"""Strands orchestration for F02 (interpretation), F03 (scenario generation),
and F04 (coverage scoring) with Milestone 4's Decider confidence-gated
checkpoints wired in.

One Strands Agent per run, backed by Bedrock, reused for every turn in that
run including across a pause/resume (Agent._interrupt_state lives on the
instance). generate_scenarios and save_scenario_set are gated by
decider_gate.DeciderGuardrail via checkpoints built in checkpoints.py;
interpret_requirement stays ungated. Remediation adds a differently-named
tool (generate_missing_scenarios) rather than replacing generate_scenarios,
so it never re-triggers the AMBIGUITY+STRATEGY checkpoint.

_ACTIVE_RUNS is in-process only: it holds the live Agent object (and thus the
only copy of its paused _interrupt_state) for any run that might later be
resumed via resume_pending_decision(). A process restart between a pause and
a resume loses that run's ability to resume -- see state.py's docstring.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from decider_gate.intervention import DeciderGuardrail
from strands import Agent

from ai_test_engineer.bedrock import get_bedrock_model
from ai_test_engineer.checkpoints import apply_human_decision, build_all
from ai_test_engineer.coverage import compute_coverage
from ai_test_engineer.state import RunController
from ai_test_engineer.tools import (
    AttemptLog,
    build_generate_missing_scenarios_tool,
    build_generate_scenarios_tool,
    build_interpret_requirement_tool,
    build_save_scenario_set_tool,
)

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
INITIAL_TARGET_IDS = ["S01", "S02", "S03", "S05"]

# run_id -> {agent, controller, attempt_log, catalog, holder, last_result}
# In-memory only -- see module docstring.
_ACTIVE_RUNS: dict[str, dict] = {}


def _artifact_root() -> Path:
    return Path(os.environ.get("AITE_ARTIFACT_ROOT", "./runs"))


def _new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}_{uuid.uuid4().hex[:8]}"


def _interpretation_prompt(requirement_text: str) -> str:
    return (
        "Here is the bundled payment-retry requirement text, verbatim:\n\n"
        f"{requirement_text}\n\n"
        "Extract exactly six business rules, BR01 through BR06, one per rule "
        "stated in the text above. For each rule, supply rule_id, a "
        "source_excerpt that is an exact, literal substring of the text "
        "above, a meaning, and expected_observations. Record any ambiguities "
        "you find (there may be none). Call interpret_requirement exactly "
        "once with all six rules together."
    )


def _generation_prompt(target_ids: list[str], catalog: dict) -> str:
    grounding = {scenario_id: catalog["scenarios"][scenario_id] for scenario_id in target_ids}
    return (
        "Write the initial scenario set with Gherkin for exactly these "
        f"scenario IDs: {target_ids}. Do not include S04 -- it is "
        "deliberately omitted from this pass.\n\n"
        "For each scenario, preconditions must match required_preconditions, "
        "action_codes must match required_action_codes exactly, and "
        "expected_assertions must match required_assertions exactly (same "
        "observation/operator/expected values), all taken from this fixed "
        f"catalog grounding:\n\n{json.dumps(grounding, indent=2)}\n\n"
        "Write a readable title, ordered human-readable actions, and "
        "Given/When/Then Gherkin for each -- these are review-only and are "
        "never executed. Call generate_scenarios exactly once with "
        f"target_ids={target_ids} and all {len(target_ids)} scenarios together."
    )


def _paused(run_id: str, controller: RunController) -> dict:
    controller.set_workflow_state(
        controller.workflow_state, stage="AWAITING_DECISION_CONFIRMATION", busy=False
    )
    return {
        "run_id": run_id,
        "stage": "AWAITING_DECISION_CONFIRMATION",
        "workflow_state": controller.workflow_state,
        "pending_decision": controller.pending_decision,
    }


def _finish_run(run_id: str) -> dict:
    run = _ACTIVE_RUNS[run_id]
    controller: RunController = run["controller"]
    scenario_set = run["attempt_log"].get_result("generate_scenarios")
    coverage = compute_coverage(
        scenario_set["scenarios"], run["catalog"], scenario_set_version=scenario_set["version"]
    )
    controller.run_dir.mkdir(parents=True, exist_ok=True)
    (controller.run_dir / "scenarios_final.json").write_text(json.dumps(scenario_set, indent=2))
    (controller.run_dir / "coverage_final.json").write_text(json.dumps(coverage, indent=2))
    (controller.run_dir / "tool_attempts.json").write_text(json.dumps(run["attempt_log"].as_dict(), indent=2))
    return {
        "run_id": run_id,
        "workflow_state": controller.workflow_state,
        "stage": controller.stage,
        "scenario_set": scenario_set,
        "coverage": coverage,
        "tool_attempts": run["attempt_log"].summary(),
    }


def _drive_recheck(run_id: str, feedback: str) -> dict:
    """Regenerate S04 only, merge it into the original set, recheck coverage.

    Called from both the AUTO-Guide-equivalent path (run_initial_pass, right
    after a save_scenario_set turn that scheduled remediation) and from
    resume_pending_decision's REMEDIATION_SCHEDULED branch -- one shared,
    tested implementation for both.
    """
    run = _ACTIVE_RUNS[run_id]
    attempt_log: AttemptLog = run["attempt_log"]
    v1 = attempt_log.get_result("generate_scenarios")

    remediation_tool = build_generate_missing_scenarios_tool(attempt_log, ["S04"])
    run["agent"].tool_registry.process_tools([remediation_tool])  # new name -- no collision, no checkpoint

    result = run["agent"](f"{feedback}\n\nCall generate_missing_scenarios with target_ids=['S04'] only.")
    run["last_result"] = result
    if result.stop_reason == "interrupt":
        # generate_missing_scenarios is ungated (not a checkpoints key) so this
        # cannot fire from this call; kept for defense-in-depth / API symmetry.
        return _paused(run_id, run["controller"])

    s04_only = attempt_log.get_result("generate_missing_scenarios")
    if s04_only is None:
        raise RuntimeError(
            "generate_missing_scenarios did not produce a valid result after "
            f"{attempt_log.count('generate_missing_scenarios')} attempt(s); stopping run."
        )
    merged = {
        "version": v1["version"] + 1,
        "scenarios": [s for s in v1["scenarios"] if s["scenario_id"] != "S04"] + s04_only["scenarios"],
        "omission_event": None,
    }
    attempt_log.overwrite_result("generate_scenarios", merged)

    result = run["agent"]("Call save_scenario_set again to recheck coverage after remediation.")
    run["last_result"] = result
    controller: RunController = run["controller"]
    if result.stop_reason == "interrupt":
        return _paused(run_id, controller)
    if controller.workflow_state == "BLOCKED":  # second gap
        return {"run_id": run_id, "workflow_state": "BLOCKED", "stage": "BLOCKED"}
    return _finish_run(run_id)


def run_initial_pass() -> dict:
    run_id = _new_run_id()
    run_dir = _artifact_root() / run_id
    controller = RunController(run_id, run_dir)
    attempt_log = AttemptLog()
    holder: dict = {}

    requirement = json.loads((CONFIG_DIR / "requirement.json").read_text())
    requirement_text = requirement["text"]
    catalog = json.loads((CONFIG_DIR / "scenario_catalog.json").read_text())
    decider_cfg = json.loads((CONFIG_DIR / "decider.json").read_text())

    interpret_requirement = build_interpret_requirement_tool(attempt_log, requirement_text)
    generate_scenarios = build_generate_scenarios_tool(attempt_log, INITIAL_TARGET_IDS)
    save_scenario_set = build_save_scenario_set_tool(attempt_log, catalog)
    client, gate, checkpoints, on_decision, on_error = build_all(
        attempt_log, catalog, controller, decider_cfg, holder
    )
    guardrail = DeciderGuardrail(
        client=client,
        gate=gate,
        checkpoints=checkpoints,
        host_label=decider_cfg["host_label"],
        on_decision=on_decision,
        on_error=on_error,
    )

    agent = Agent(
        model=get_bedrock_model(),
        tools=[interpret_requirement, generate_scenarios, save_scenario_set],
        interventions=[guardrail],
        # Strands' default callback handler prints raw model/tool-call deltas
        # to stdout and can crash on non-UTF-8 consoles (observed: a '→'
        # the model emitted raised UnicodeEncodeError on cp1252 Windows).
        # Orchestration returns structured results; callers print what they need.
        callback_handler=None,
    )
    _ACTIVE_RUNS[run_id] = {
        "agent": agent,
        "controller": controller,
        "attempt_log": attempt_log,
        "catalog": catalog,
        "holder": holder,
        "last_result": None,
    }

    try:
        result = agent(_interpretation_prompt(requirement_text))  # ungated
        _ACTIVE_RUNS[run_id]["last_result"] = result
        interpretation = attempt_log.get_result("interpret_requirement")
        if interpretation is None:
            raise RuntimeError(
                "interpret_requirement did not produce a valid result after "
                f"{attempt_log.count('interpret_requirement')} attempt(s); stopping run."
            )
        controller.run_dir.mkdir(parents=True, exist_ok=True)
        (controller.run_dir / "interpretation.json").write_text(json.dumps(interpretation, indent=2))

        result = agent(_generation_prompt(INITIAL_TARGET_IDS, catalog))  # gated: AMBIGUITY+STRATEGY
        _ACTIVE_RUNS[run_id]["last_result"] = result
        if result.stop_reason == "interrupt":
            return _paused(run_id, controller)
        if controller.workflow_state == "AWAITING_CLARIFICATION":
            return {
                "run_id": run_id,
                "workflow_state": "AWAITING_CLARIFICATION",
                "stage": "AWAITING_CLARIFICATION",
            }
        scenario_set = attempt_log.get_result("generate_scenarios")
        if scenario_set is None:
            raise RuntimeError(
                "generate_scenarios did not produce a valid result after "
                f"{attempt_log.count('generate_scenarios')} attempt(s); stopping run."
            )
        controller.run_dir.mkdir(parents=True, exist_ok=True)
        (controller.run_dir / "scenarios_initial.json").write_text(json.dumps(scenario_set, indent=2))

        result = agent(
            "Call save_scenario_set now to score the scenario set you just produced."
        )  # gated: COVERAGE
        _ACTIVE_RUNS[run_id]["last_result"] = result
        if result.stop_reason == "interrupt":
            return _paused(run_id, controller)
        if controller.workflow_state == "BLOCKED":
            return {"run_id": run_id, "workflow_state": "BLOCKED", "stage": "BLOCKED"}
        feedback = holder.pop("remediation_feedback", None)
        if feedback is not None:
            return _drive_recheck(run_id, feedback)

        return _finish_run(run_id)
    finally:
        controller.run_dir.mkdir(parents=True, exist_ok=True)
        (controller.run_dir / "tool_attempts.json").write_text(json.dumps(attempt_log.as_dict(), indent=2))


def resume_pending_decision(run_id: str, human_choice: str) -> dict:
    """Applies a human's pick for a paused run's pending escalation.

    A REJECTED_RETRY outcome (e.g. ACCEPT_COVERAGE picked below 5/5) leaves
    both the Strands interrupt and controller.pending_decision untouched --
    the caller can call this again with a different human_choice. Every
    other outcome resumes the paused interrupt and clears pending_decision.
    """
    run = _ACTIVE_RUNS[run_id]
    agent = run["agent"]
    controller: RunController = run["controller"]
    pending = controller.pending_decision
    if pending is None:
        raise RuntimeError(f"run {run_id} has no pending decision")

    outcome, feedback = apply_human_decision(pending, human_choice, controller)

    if outcome == "REJECTED_RETRY":
        return {
            "run_id": run_id,
            "stage": "AWAITING_DECISION_CONFIRMATION",
            "workflow_state": controller.workflow_state,
            "rejected_choice": human_choice,
        }

    controller.set_pending_decision(None)
    interrupt = next(i for i in run["last_result"].interrupts if i.name == "decider-gate")
    result = agent(
        [
            {
                "interruptResponse": {
                    "interruptId": interrupt.id,
                    "response": "yes" if outcome == "PROCEED" else "no",
                }
            }
        ]
    )
    run["last_result"] = result

    if outcome == "REMEDIATION_SCHEDULED":
        return _drive_recheck(run_id, feedback)
    if outcome == "BLOCKED":
        return {"run_id": run_id, "workflow_state": "BLOCKED", "stage": "BLOCKED"}
    if outcome == "AWAITING_CLARIFICATION":
        return {
            "run_id": run_id,
            "workflow_state": "AWAITING_CLARIFICATION",
            "stage": "AWAITING_CLARIFICATION",
        }
    return _finish_run(run_id)  # PROCEED
