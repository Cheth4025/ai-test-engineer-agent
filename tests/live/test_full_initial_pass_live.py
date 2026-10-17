"""Real-Bedrock + real-Decider-server end-to-end smoke test for Milestone 4's
checkpoints. Deselected by default (addopts = -m "not live").

Run manually once a Decider server is up:
    .venv/Scripts/python.exe -m pytest tests/live/test_full_initial_pass_live.py -v -m live -s

This test must never auto-answer an escalation. If run_initial_pass() pauses
at AWAITING_DECISION_CONFIRMATION, it prints the pending decision and stops
there -- it does not call resume_pending_decision(). It reports back which
of the three legitimate outcomes the live Decider actually took: a clean
finish (4/5 -> REMEDIATE_GAP -> 5/5, no escalation), an AWAITING_CLARIFICATION
stop, or a paused AWAITING_DECISION_CONFIRMATION escalation.
"""

from __future__ import annotations

import json

import pytest

from ai_test_engineer import orchestration

pytestmark = pytest.mark.live


def _print_decision(i: int, record: dict) -> None:
    print(
        f"\n[{i}] checkpoint={record['checkpoint']} route={record['route']} "
        f"selected_action={record['selected_action']!r} "
        f"decider_suggestion={record['decider_suggestion']!r} "
        f"confidence={record['confidence']} latency_ms={record['latency_ms']} "
        f"host_label={record['host_label']} model={record['model_revision']} "
        f"decision_source={record['decision_source']} decision_status={record['decision_status']}"
    )


def test_live_full_initial_pass():
    result = orchestration.run_initial_pass()
    run_id = result["run_id"]
    controller = orchestration._ACTIVE_RUNS[run_id]["controller"]
    run_dir = controller.run_dir

    print(f"\nrun_id: {run_id}")
    print(f"workflow_state: {result.get('workflow_state')}  stage: {result.get('stage')}")
    print(f"\n--- decisions ({len(controller.decisions)}) ---")
    for i, record in enumerate(controller.decisions, start=1):
        _print_decision(i, record)

    before_path = run_dir / "coverage_v1.json"
    if before_path.exists():
        before = json.loads(before_path.read_text())
        print(
            f"\ncoverage BEFORE remediation: {before['covered']}/{before['required']} "
            f"({before['percentage']}%)"
        )
    else:
        print("\nno coverage_v1.json -- remediation was never scheduled on this run")

    if result.get("stage") == "AWAITING_DECISION_CONFIRMATION":
        pending = controller.pending_decision
        print("\n=== PATH TAKEN: paused for human escalation (AWAITING_DECISION_CONFIRMATION) ===")
        print(json.dumps(pending, indent=2, default=str))
        assert pending is not None
        assert pending["tool_name"] in {"generate_scenarios", "save_scenario_set"}
        return

    if result.get("workflow_state") == "AWAITING_CLARIFICATION":
        print("\n=== PATH TAKEN: stopped for clarification (AWAITING_CLARIFICATION) ===")
        return

    if result.get("workflow_state") == "BLOCKED":
        print("\n=== PATH TAKEN: BLOCKED (second coverage gap) ===")
        return

    print("\n=== PATH TAKEN: finished without escalation (clean pass or AUTO remediation) ===")
    coverage = result["coverage"]
    print(f"\ncoverage AFTER: {coverage['covered']}/{coverage['required']} ({coverage['percentage']}%)")
    for row in coverage["rows"]:
        print(f"  {row['scenario_id']}: {row['status']}")

    assert coverage["covered"] == 5
    assert coverage["required"] == 5
    assert (run_dir / "decisions.json").exists()
    assert list(run_dir.glob("grounding_v*.json"))
