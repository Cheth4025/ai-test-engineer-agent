"""Strands orchestration for F02 (interpretation), F03 (scenario generation),
and F04 (coverage scoring) with Milestone 4's Decider confidence-gated
checkpoints wired in.

One Strands Agent per run, backed by Bedrock, reused for every turn in that
run including across a pause/resume (Agent._interrupt_state lives on the
instance). All three tools are registered on the Agent up front, so
checkpoints.StageGuard (see STAGE_TOOLS below) is what actually keeps the
model from calling e.g. generate_scenarios during the INTERPRETING stage --
without it, a model that calls a tool early would reach
decider_gate.DeciderGuardrail's checkpoints (built in checkpoints.py) and
could escalate out of turn. generate_scenarios and save_scenario_set are
gated by DeciderGuardrail; interpret_requirement and
generate_missing_scenarios stay ungated (but still stage-limited).
Remediation adds a differently-named tool (generate_missing_scenarios)
rather than replacing generate_scenarios, so it never re-triggers the
AMBIGUITY+STRATEGY checkpoint.

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
from ai_test_engineer.checkpoints import (
    HaltGuard,
    StageGuard,
    apply_human_decision,
    build_all,
    decision_record_reused,
)
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

# docs/03 Section 3: "Tools available to the agent are limited per stage by
# the controller." Enforced by checkpoints.StageGuard via
# RunController.allowed_tools -- set here, consumed there.
STAGE_TOOLS: dict[str, frozenset[str]] = {
    "INTERPRETING": frozenset({"interpret_requirement"}),
    "GENERATING": frozenset({"generate_scenarios"}),
    "SCORING": frozenset({"save_scenario_set"}),
    "REMEDIATING": frozenset({"generate_missing_scenarios"}),
    "RESCORING": frozenset({"save_scenario_set"}),
}

# run_id -> {agent, controller, attempt_log, catalog, holder, last_result}
# In-memory only -- see module docstring.
_ACTIVE_RUNS: dict[str, dict] = {}


def _artifact_root() -> Path:
    return Path(os.environ.get("AITE_ARTIFACT_ROOT", "./runs"))


def _new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}_{uuid.uuid4().hex[:8]}"


def _scenario_summaries(catalog: dict) -> str:
    """One line per S01-S05: title + the concrete outcome its assertions
    check -- so the interpretation prompt can judge `critical` against the
    real scenarios instead of a vague description. Built from
    required_assertions' own observation/operator/expected, same fields
    checkpoints.py's grounding question embeds."""
    lines = []
    for scenario_id in sorted(catalog["scenarios"]):
        scenario = catalog["scenarios"][scenario_id]
        assertions = "; ".join(
            f"{a['observation']} {a['operator']} {a['expected']!r}" for a in scenario["required_assertions"]
        )
        lines.append(f"{scenario_id} ({scenario['test_id']}) '{scenario['title']}': asserts {assertions}")
    return "\n".join(lines)


def _interpretation_prompt(requirement_text: str, catalog: dict) -> str:
    return (
        "Here is the bundled payment-retry requirement text, verbatim:\n\n"
        f"{requirement_text}\n\n"
        "Extract exactly six business rules, BR01 through BR06, one per rule "
        "stated in the text above. For each rule, supply rule_id, a "
        "source_excerpt that is an exact, literal substring of the text "
        "above, a meaning, and expected_observations. Record every "
        "ambiguity you find (there may be none) -- never suppress one "
        "because it looks out of scope; scope only controls its `critical` "
        "flag, decided as follows.\n\n"
        "Scope for `critical`: this is a synthetic test specification "
        "covering exactly five fixed scenarios, S01 through S05 (tests T01 "
        "through T05), each run against deterministic fixtures -- "
        "\"scripted\" means the fixture's deterministic script, nothing "
        "live or production. Here is exactly what each of those five "
        "scenarios asserts, so judge `critical` against these real "
        f"scenarios, not a guess at what they might cover:\n\n{_scenario_summaries(catalog)}\n\n"
        "Mark an ambiguity critical ONLY if resolving it would change what "
        "one of S01-S05/T01-T05 must assert per the list above -- e.g. "
        "which exact condition, value, branch, or outcome one of those five "
        "scenarios needs to check. An ambiguity about a topic that cannot "
        "change any of S01-S05's assertions is NOT critical, even if it is "
        "a real open question -- e.g. key generation ownership, TTLs, and "
        "non-amount payload fields are out of scope for this fixed scenario "
        "set, so record them as non-critical ambiguities rather than "
        "omitting them. Every ambiguity must carry exactly one rule_id: one "
        "of BR01-BR06, or GENERAL only if it genuinely spans multiple rules "
        "-- never leave rule_id unset. Call interpret_requirement exactly "
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
    _write_tool_attempts(run_id)
    return {
        "run_id": run_id,
        "stage": "AWAITING_DECISION_CONFIRMATION",
        "workflow_state": controller.workflow_state,
        "pending_decision": controller.pending_decision,
    }


def _write_tool_attempts(run_id: str) -> None:
    """Best-effort: a no-op if run_id was never registered in _ACTIVE_RUNS
    (e.g. a unit test driving a helper directly against a bare controller),
    never a KeyError on an exit path that is trying to leave evidence behind."""
    run = _ACTIVE_RUNS.get(run_id)
    if run is None:
        return
    controller: RunController = run["controller"]
    attempt_log: AttemptLog = run["attempt_log"]
    controller.run_dir.mkdir(parents=True, exist_ok=True)
    (controller.run_dir / "tool_attempts.json").write_text(json.dumps(attempt_log.as_dict(), indent=2))


def _unexpected_interrupt_stop(run_id: str, controller: RunController, result, context: str) -> dict:
    """An agent() call that was not expected to pause (interpret_requirement's
    ungated turn, or a just-resumed turn) instead raised an interrupt -- e.g.
    the model called a gated tool (generate_scenarios/save_scenario_set)
    before orchestration asked it to, and that call escalated. Strands'
    Agent._interrupt_state stays activated=True until that interrupt is
    answered; the NEXT agent() call in this run is always a plain string
    prompt, and _InterruptState.resume() raises TypeError on a string while
    activated (see strands/interrupt.py) -- so this must never be allowed to
    fall through silently. Stop the run here instead, with evidence of
    exactly which interrupt(s) were pending and why.
    """
    interrupts = [{"id": i.id, "name": i.name, "reason": i.reason} for i in (result.interrupts or [])]
    controller.save_evidence(
        "unexpected_interrupt.json", {"context": context, "interrupts": interrupts}
    )
    controller.set_workflow_state("STOPPED", stage="STOPPED", busy=False)
    _write_tool_attempts(run_id)
    return {
        "run_id": run_id,
        "workflow_state": "STOPPED",
        "stage": "STOPPED",
        "error": f"unexpected interrupt during {context}",
        "interrupts": interrupts,
    }


def _finish_run(run_id: str) -> dict:
    """Only ever reached after the COVERAGE checkpoint accepted a full 5/5
    set (every other outcome -- BLOCKED, AWAITING_CLARIFICATION, a pause --
    returns earlier). docs/05 Section 15 (REMEDIATING: "AWAITING_APPROVAL
    only at valid 5/5") + docs/06 Section 3 (Approve requires "AWAITING_
    APPROVAL; accepted 5/5"): accepted 5/5 moves the run to AWAITING_APPROVAL,
    not whatever stage/workflow_state happened to be left over from the last
    pause. Previously this was never set, so state.json kept reporting
    stage=AWAITING_DECISION_CONFIRMATION with pending_decision=None after the
    run actually finished."""
    run = _ACTIVE_RUNS[run_id]
    controller: RunController = run["controller"]
    scenario_set = run["attempt_log"].get_result("generate_scenarios")
    coverage = compute_coverage(
        scenario_set["scenarios"], run["catalog"], scenario_set_version=scenario_set["version"]
    )
    controller.set_workflow_state("AWAITING_APPROVAL", stage="AWAITING_APPROVAL", busy=False)
    controller.run_dir.mkdir(parents=True, exist_ok=True)
    (controller.run_dir / "scenarios_final.json").write_text(json.dumps(scenario_set, indent=2))
    (controller.run_dir / "coverage_final.json").write_text(json.dumps(coverage, indent=2))
    _write_tool_attempts(run_id)
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
    controller: RunController = run["controller"]
    controller.clear_halt()  # fresh turn: lift the halt the triggering decision set
    attempt_log: AttemptLog = run["attempt_log"]
    v1 = attempt_log.get_result("generate_scenarios")

    remediation_tool = build_generate_missing_scenarios_tool(attempt_log, ["S04"])
    run["agent"].tool_registry.process_tools([remediation_tool])  # new name -- no collision, no checkpoint

    controller.set_stage("REMEDIATING", STAGE_TOOLS["REMEDIATING"])
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

    controller.set_stage("RESCORING", STAGE_TOOLS["RESCORING"])
    result = run["agent"]("Call save_scenario_set again to recheck coverage after remediation.")
    run["last_result"] = result
    if result.stop_reason == "interrupt":
        return _paused(run_id, controller)
    if controller.workflow_state == "BLOCKED":  # second gap
        _write_tool_attempts(run_id)
        return {"run_id": run_id, "workflow_state": "BLOCKED", "stage": "BLOCKED"}
    return _finish_run(run_id)


def _continue_to_scoring(run_id: str) -> dict:
    """Drives the SCORING stage for a scenario set that generate_scenarios
    just produced, whichever path got it there: run_initial_pass's own
    (possibly AUTO-resolved) agent() call, or resume_pending_decision after a
    human resumed the AMBIGUITY+STRATEGY checkpoint's escalation with
    GENERATE_TESTS. Both must reach the exact same next step -- grab the
    scenario set, write scenarios_initial.json, gate into SCORING, and ask
    the model to call save_scenario_set -- so this is the one shared
    implementation, never duplicated between the two callers."""
    run = _ACTIVE_RUNS[run_id]
    controller: RunController = run["controller"]
    attempt_log: AttemptLog = run["attempt_log"]
    holder: dict = run["holder"]

    scenario_set = attempt_log.get_result("generate_scenarios")
    if scenario_set is None:
        raise RuntimeError(
            "generate_scenarios did not produce a valid result after "
            f"{attempt_log.count('generate_scenarios')} attempt(s); stopping run."
        )
    controller.run_dir.mkdir(parents=True, exist_ok=True)
    (controller.run_dir / "scenarios_initial.json").write_text(json.dumps(scenario_set, indent=2))

    controller.set_stage("SCORING", STAGE_TOOLS["SCORING"])
    result = run["agent"](
        "Call save_scenario_set now to score the scenario set you just produced."
    )  # gated: COVERAGE
    run["last_result"] = result
    if result.stop_reason == "interrupt":
        return _paused(run_id, controller)
    if controller.workflow_state == "BLOCKED":
        _write_tool_attempts(run_id)
        return {"run_id": run_id, "workflow_state": "BLOCKED", "stage": "BLOCKED"}
    feedback = holder.pop("remediation_feedback", None)
    if feedback is not None:
        return _drive_recheck(run_id, feedback)

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
    client, gate, checkpoints, on_decision, on_error, host_label = build_all(
        attempt_log, catalog, controller, decider_cfg, holder
    )

    def on_reuse(tool_name: str, verdicts: dict, action, origin_tool_use_id: str | None) -> None:
        controller.record_decision(
            decision_record_reused(tool_name, verdicts, action, origin_tool_use_id, controller, decider_cfg, host_label)
        )

    def on_call(tool_name: str, tool_use_id: str | None, action) -> None:
        controller.record_tool_call(tool_name, tool_use_id, controller.tool_stage, "decider-gate", type(action).__name__)

    guardrail = DeciderGuardrail(
        client=client,
        gate=gate,
        checkpoints=checkpoints,
        host_label=host_label,
        on_decision=on_decision,
        on_error=on_error,
        on_reuse=on_reuse,
        on_call=on_call,
    )

    agent = Agent(
        model=get_bedrock_model(),
        tools=[interpret_requirement, generate_scenarios, save_scenario_set],
        # Registration order is dispatch order, and a Deny short-circuits the
        # rest: StageGuard (denies a tool the current stage doesn't allow,
        # before Decider is ever consulted) ahead of HaltGuard (denies
        # everything once a decision has halted the run) ahead of guardrail.
        interventions=[StageGuard(controller), HaltGuard(controller), guardrail],
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
        controller.set_stage("INTERPRETING", STAGE_TOOLS["INTERPRETING"])
        result = agent(_interpretation_prompt(requirement_text, catalog))  # ungated
        _ACTIVE_RUNS[run_id]["last_result"] = result
        if result.stop_reason == "interrupt":
            # interpret_requirement itself never gates; a pending interrupt here
            # means the model called a gated tool early in this same turn and it
            # escalated. Never fall through to the next (string-prompt) agent()
            # call -- see _unexpected_interrupt_stop's docstring.
            return _unexpected_interrupt_stop(run_id, controller, result, "interpret_requirement turn")
        interpretation = attempt_log.get_result("interpret_requirement")
        if interpretation is None:
            raise RuntimeError(
                "interpret_requirement did not produce a valid result after "
                f"{attempt_log.count('interpret_requirement')} attempt(s); stopping run."
            )
        controller.run_dir.mkdir(parents=True, exist_ok=True)
        (controller.run_dir / "interpretation.json").write_text(json.dumps(interpretation, indent=2))

        controller.set_stage("GENERATING", STAGE_TOOLS["GENERATING"])
        result = agent(_generation_prompt(INITIAL_TARGET_IDS, catalog))  # gated: AMBIGUITY+STRATEGY
        _ACTIVE_RUNS[run_id]["last_result"] = result
        if result.stop_reason == "interrupt":
            return _paused(run_id, controller)
        if controller.workflow_state == "AWAITING_CLARIFICATION":
            _write_tool_attempts(run_id)
            return {
                "run_id": run_id,
                "workflow_state": "AWAITING_CLARIFICATION",
                "stage": "AWAITING_CLARIFICATION",
            }
        return _continue_to_scoring(run_id)
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
    if result.stop_reason == "interrupt":
        # A second, different escalation surfaced in the same resumed turn
        # (e.g. the model called another gated tool right after this one was
        # answered). resume_pending_decision only knows how to apply ONE
        # human answer per call; never fall through to _drive_recheck's
        # string-prompt agent() calls with this interrupt still pending.
        return _unexpected_interrupt_stop(run_id, controller, result, "resume_pending_decision")

    if outcome == "REMEDIATION_SCHEDULED":
        return _drive_recheck(run_id, feedback)
    if outcome == "BLOCKED":
        _write_tool_attempts(run_id)
        return {"run_id": run_id, "workflow_state": "BLOCKED", "stage": "BLOCKED"}
    if outcome == "AWAITING_CLARIFICATION":
        _write_tool_attempts(run_id)
        return {
            "run_id": run_id,
            "workflow_state": "AWAITING_CLARIFICATION",
            "stage": "AWAITING_CLARIFICATION",
        }
    if outcome == "PROCEED" and pending["tool_name"] == "generate_scenarios":
        # The AMBIGUITY+STRATEGY checkpoint's own escalation, now answered by
        # a human: continue exactly where run_initial_pass's AUTO path would
        # have -- into SCORING -- rather than finishing the run on a
        # scenario set that was never actually scored. See
        # _continue_to_scoring's docstring.
        return _continue_to_scoring(run_id)
    return _finish_run(run_id)  # PROCEED (save_scenario_set's ACCEPT_COVERAGE)
