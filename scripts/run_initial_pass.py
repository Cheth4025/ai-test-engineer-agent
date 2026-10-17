"""Interactive, human-driven run of orchestration.run_initial_pass() in one process.

Runs the full initial pass against real Bedrock and a real Decider server.
Whenever the run pauses at AWAITING_DECISION_CONFIRMATION, this script prints
the checkpoint, the options, Decider's suggestion, confidence, and
probabilities, then asks a human on stdin which option to pick -- there is no
default and no auto-answer. The typed choice is applied via
resume_pending_decision(), through the same deterministic guards as the AUTO
path, and the loop repeats until the run finishes or stops.

Must run in the SAME process as the pause and the resume: orchestration's
_ACTIVE_RUNS (and the live Strands Agent/_interrupt_state it holds) is
in-process only. Running this script, then separately trying to resume a
run from a different process or a later invocation will not work -- see
ai_test_engineer.state's module docstring for why.

Usage:
    .venv/Scripts/python.exe scripts/run_initial_pass.py
"""

from __future__ import annotations

import json

from ai_test_engineer import orchestration


def _print_decisions(controller) -> None:
    print(f"\n--- decisions ({len(controller.decisions)}) ---")
    for i, record in enumerate(controller.decisions, start=1):
        print(
            f"[{i}] checkpoint={record['checkpoint']} route={record['route']} "
            f"selected_action={record['selected_action']!r} "
            f"decider_suggestion={record['decider_suggestion']!r} "
            f"confidence={record['confidence']} latency_ms={record['latency_ms']} "
            f"host_label={record['host_label']} model={record['model_revision']} "
            f"decision_source={record['decision_source']} decision_status={record['decision_status']}"
        )
        for q in record.get("per_question", []):
            print(
                f"      - {q['question']}: answer={q['answer']!r} suggestion={q['suggestion']!r} "
                f"confidence={q['confidence']} route={q['route']} probabilities={q['probabilities']!r}"
            )


def _options_for(tool_name: str) -> list[str]:
    if tool_name == "save_scenario_set":
        return ["ACCEPT_COVERAGE", "REMEDIATE_GAP"]
    if tool_name == "generate_scenarios":
        return ["GENERATE_TESTS", "REQUEST_CLARIFICATION"]
    raise ValueError(f"unrecognized pending tool {tool_name!r}")


def _ask_human(pending: dict) -> str:
    print("\n=== AWAITING_DECISION_CONFIRMATION ===")
    print(f"checkpoint tool: {pending['tool_name']}")
    print("Decider's per-question suggestions:")
    for name, fields in pending["verdicts"].items():
        print(
            f"  {name}: answer={fields['answer']!r} suggestion={fields['suggestion']!r} "
            f"confidence={fields['confidence']} route={fields['route']} "
            f"probabilities={fields['probabilities']!r}"
        )
    context = pending.get("context") or {}
    if "coverage" in context:
        coverage = context["coverage"]
        print(f"coverage: {coverage['covered']}/{coverage['required']} ({coverage['percentage']}%)")
        for row in coverage["rows"]:
            print(f"  {row['scenario_id']}: {row['status']}")

    options = _options_for(pending["tool_name"])
    print(f"options: {options}")
    while True:
        choice = input("Your choice: ").strip()
        if choice in options:
            return choice
        print(f"'{choice}' is not one of {options}; try again.")


def _stop_on_exception(exc: Exception, run_id: str | None = None) -> None:
    """Never let an exception reach the top as a raw traceback: mark the run
    STOPPED with a readable reason (docs/06 Section 4: "readable failure
    reason ... never a fabricated success") and print that reason instead."""
    controller = None
    if run_id is not None:
        active = orchestration._ACTIVE_RUNS.get(run_id)
        controller = active["controller"] if active else None
    elif orchestration._ACTIVE_RUNS:
        controller = list(orchestration._ACTIVE_RUNS.values())[-1]["controller"]
    if controller is not None:
        controller.set_workflow_state("STOPPED", stage="STOPPED", busy=False)
        controller.save_evidence(
            "exception_stop.json", {"error_type": type(exc).__name__, "message": str(exc)}
        )
    print(f"\nRun stopped: {type(exc).__name__}: {exc}")


def main() -> None:
    try:
        result = orchestration.run_initial_pass()
    except Exception as exc:
        _stop_on_exception(exc)
        return

    run_id = result["run_id"]
    controller = orchestration._ACTIVE_RUNS[run_id]["controller"]
    run_dir = controller.run_dir

    # controller.pending_decision, not result.get("pending_decision"): a
    # REJECTED_RETRY result deliberately omits that key (apply_human_decision
    # never touches pending_decision on a rejection -- see its docstring) but
    # the decision is still open. The controller is the authoritative source.
    while result.get("stage") == "AWAITING_DECISION_CONFIRMATION" and controller.pending_decision is not None:
        if "rejected_choice" in result:
            print(
                f"\n'{result['rejected_choice']}' was rejected by the deterministic guard "
                "(see the decision just recorded) -- pick again."
            )
        pending = controller.pending_decision
        choice = _ask_human(pending)
        try:
            result = orchestration.resume_pending_decision(run_id, choice)
        except Exception as exc:
            _stop_on_exception(exc, run_id)
            return

    print(f"\nrun_id: {run_id}")
    print(f"final workflow_state: {result.get('workflow_state')}  stage: {result.get('stage')}")

    if not controller.remediation_used:
        print("coverage BEFORE remediation: n/a (remediation was never scheduled on this run)")
    else:
        before_path = run_dir / "coverage_v1.json"
        if before_path.exists():
            before = json.loads(before_path.read_text())
            print(
                f"coverage BEFORE remediation: {before['covered']}/{before['required']} "
                f"({before['percentage']}%)"
            )
        else:
            print("coverage BEFORE remediation: remediation was scheduled but coverage_v1.json is missing")

    if "coverage" in result:
        coverage = result["coverage"]
        print(f"coverage AFTER: {coverage['covered']}/{coverage['required']} ({coverage['percentage']}%)")
        for row in coverage["rows"]:
            print(f"  {row['scenario_id']}: {row['status']}")

    _print_decisions(controller)


if __name__ == "__main__":
    main()
