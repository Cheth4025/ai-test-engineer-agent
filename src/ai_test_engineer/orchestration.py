"""Strands orchestration for F02 (interpretation) and F03 (initial scenario set).

One Strands Agent per run, backed by Bedrock, with interpret_requirement and
generate_scenarios as its only tools. No interventions yet -- Milestone 4
wires the Decider confidence-gated checkpoints (decider_gate) around these
same tool names; this module must stay importable without decider_gate.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from strands import Agent

from ai_test_engineer.bedrock import get_bedrock_model
from ai_test_engineer.coverage import compute_coverage
from ai_test_engineer.tools import (
    AttemptLog,
    build_generate_scenarios_tool,
    build_interpret_requirement_tool,
)

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
INITIAL_TARGET_IDS = ["S01", "S02", "S03", "S05"]


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


def run_initial_pass() -> dict:
    run_id = _new_run_id()
    attempt_log = AttemptLog()
    run_dir = _artifact_root() / run_id

    requirement = json.loads((CONFIG_DIR / "requirement.json").read_text())
    requirement_text = requirement["text"]
    catalog = json.loads((CONFIG_DIR / "scenario_catalog.json").read_text())

    interpret_requirement = build_interpret_requirement_tool(attempt_log, requirement_text)
    generate_scenarios = build_generate_scenarios_tool(attempt_log, INITIAL_TARGET_IDS)
    agent = Agent(
        model=get_bedrock_model(),
        tools=[interpret_requirement, generate_scenarios],
        # Strands' default callback handler prints raw model/tool-call deltas
        # to stdout and can crash on non-UTF-8 consoles (observed: a '→'
        # the model emitted raised UnicodeEncodeError on cp1252 Windows).
        # Orchestration returns structured results; callers print what they need.
        callback_handler=None,
    )

    try:
        agent(_interpretation_prompt(requirement_text))
        interpretation = attempt_log.get_result("interpret_requirement")
        if interpretation is None:
            raise RuntimeError(
                "interpret_requirement did not produce a valid result after "
                f"{attempt_log.count('interpret_requirement')} attempt(s); stopping run."
            )

        agent(_generation_prompt(INITIAL_TARGET_IDS, catalog))
        scenario_set = attempt_log.get_result("generate_scenarios")
        if scenario_set is None:
            raise RuntimeError(
                "generate_scenarios did not produce a valid result after "
                f"{attempt_log.count('generate_scenarios')} attempt(s); stopping run."
            )

        coverage = compute_coverage(
            scenario_set["scenarios"], catalog, scenario_set_version=scenario_set["version"]
        )

        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "interpretation.json").write_text(json.dumps(interpretation, indent=2))
        (run_dir / "scenarios_initial.json").write_text(json.dumps(scenario_set, indent=2))
        (run_dir / "coverage_v1.json").write_text(json.dumps(coverage, indent=2))

        return {
            "run_id": run_id,
            "interpretation": interpretation,
            "scenario_set": scenario_set,
            "coverage": coverage,
            "tool_attempts": attempt_log.summary(),
        }
    finally:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "tool_attempts.json").write_text(json.dumps(attempt_log.as_dict(), indent=2))
