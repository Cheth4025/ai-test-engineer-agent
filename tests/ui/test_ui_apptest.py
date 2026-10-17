"""Streamlit AppTest smoke tests for src/ai_test_engineer/ui.py.

Orchestration stubbed throughout -- no real Bedrock/Decider calls, matching
tests/unit/test_orchestration_stubbed.py's "no Bedrock, no GPU, no network"
principle, but at the UI-wiring level rather than re-testing orchestration's
own business logic (already covered by tests/unit and tests/ui/
test_ui_guards.py).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("streamlit.testing.v1")
from streamlit.testing.v1 import AppTest

from ai_test_engineer import orchestration
from ai_test_engineer.state import RunController


@pytest.fixture
def ui_script_path(tmp_path) -> str:
    """An AppTest-only wrapper script (never ui.py itself): clears
    get_ui_state()'s @st.cache_resource entry from INSIDE a real Streamlit
    script context before calling ui.main().

    cache_resource's storage key is tied to the decorated function object
    itself, shared process-wide regardless of which file path imports and
    calls it -- confirmed directly: pointing two separate AppTest instances
    at two DIFFERENT copies of ui.py still leaked state from one into the
    other once the first instance actually drove a click/worker thread (an
    untouched copy's default state coincidentally matches a fresh cache
    entry's default, which is why a lighter smoke test of "different path"
    alone looked isolated and was not). A bare get_ui_state.clear() call
    made from outside any Streamlit script context also does not reach
    that same keyed entry. Calling .clear() from a script that DOES have a
    real context -- this wrapper -- reaches it correctly, with no test-only
    hook needed in ui.py's own production code."""
    wrapper = tmp_path / "ui_test_wrapper.py"
    wrapper.write_text(
        "import streamlit as st\n"
        "import ai_test_engineer.ui as ui\n"
        # Only once per AppTest session (st.session_state IS correctly
        # session-scoped) -- clearing on every rerun would also wipe
        # worker_busy/current_run_id set by this same test's own earlier
        # clicks, breaking intra-test persistence.
        "if 'ui_state_reset_done' not in st.session_state:\n"
        "    ui.get_ui_state.clear()\n"
        "    st.session_state['ui_state_reset_done'] = True\n"
        "ui.main()\n"
    )
    return str(wrapper)


@pytest.fixture(autouse=True)
def _clean_active_runs():
    orchestration._ACTIVE_RUNS.clear()
    yield
    orchestration._ACTIVE_RUNS.clear()


def _app(path: str) -> AppTest:
    # default_timeout above AppTest's 3s default: a cold run (first import of
    # streamlit/orchestration/checkpoints in this process) takes ~2s on its
    # own, which is too close to the default for a reliable CI margin.
    return AppTest.from_file(path, default_timeout=15)


def _settle(at: AppTest, predicate, tries: int = 50, delay: float = 0.05) -> AppTest:
    """Re-renders (at.run(), like a Refresh Status tick) until predicate(at)
    holds. Deliberately re-enters the SAME AppTest instance's script
    context every time rather than reaching into ui.get_ui_state() from
    bare test code -- a background worker's result only becomes visible
    to the next script run, not to a get_ui_state() call made outside any
    AppTest run."""
    for _ in range(tries):
        if predicate(at):
            return at
        time.sleep(delay)
        at.run()
    raise AssertionError(f"did not settle within {tries * delay:.1f}s")


def _has_label(at: AppTest, widget: str, label: str) -> bool:
    return any(w.label == label for w in getattr(at, widget))


def test_app_loads_with_four_tabs_and_start_analysis_button(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    at = _app(ui_script_path)
    at.run()

    assert not at.exception
    assert [tab.label for tab in at.tabs] == [
        "Overview & Requirements",
        "Scenarios & Coverage",
        "Approval & Execution",
        "Results & Report",
    ]
    assert any(b.label == "Start Analysis" for b in at.button)


def test_plain_reload_never_calls_orchestration_entry_points(monkeypatch, tmp_path, ui_script_path):
    """A browser reload (no click at all) must never start work -- not even
    a second reload after the first has rendered."""

    def _boom(*args, **kwargs):
        raise AssertionError("a plain page load must never call this")

    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr(orchestration, "run_initial_pass", _boom)
    monkeypatch.setattr(orchestration, "resume_pending_decision", _boom)

    at = _app(ui_script_path)
    at.run()
    assert not at.exception
    at.run()  # a second "reload"
    assert not at.exception


def test_approval_and_results_tabs_show_disabled_not_run_placeholders(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    at = _app(ui_script_path)
    at.run()

    assert not at.exception
    approve = next(b for b in at.button if b.label == "Approve")
    reject = next(b for b in at.button if b.label == "Reject")
    execute = next(b for b in at.button if b.label == "Execute")
    assert approve.disabled and reject.disabled and execute.disabled


def _register_paused_run(tmp_path, run_id: str, tool_name: str, verdicts: dict | None = None) -> dict:
    if verdicts is None:
        verdicts = (
            {
                "coverage_action": {
                    "answer": None,
                    "suggestion": "REMEDIATE_GAP",
                    "confidence": 0.55,
                    "probabilities": {"ACCEPT_COVERAGE": 0.45, "REMEDIATE_GAP": 0.55},
                    "route": "ESCALATE",
                }
            }
            if tool_name == "save_scenario_set"
            else {
                "strategy_action": {
                    "answer": None,
                    "suggestion": "REQUEST_CLARIFICATION",
                    "confidence": 0.55,
                    "probabilities": {"GENERATE_TESTS": 0.45, "REQUEST_CLARIFICATION": 0.55},
                    "route": "ESCALATE",
                }
            }
        )
    pending = {"tool_name": tool_name, "context": {}, "verdicts": verdicts}
    controller = RunController(run_id, tmp_path / run_id)
    controller.set_workflow_state("PREPARING", stage="AWAITING_DECISION_CONFIRMATION", busy=False)
    controller.set_pending_decision(pending)
    orchestration._ACTIVE_RUNS[run_id] = {"controller": controller}
    return {
        "run_id": run_id,
        "stage": "AWAITING_DECISION_CONFIRMATION",
        "workflow_state": "PREPARING",
        "pending_decision": pending,
    }


def test_start_analysis_settles_into_confirm_decision_control_with_suggested_option(
    monkeypatch, tmp_path, ui_script_path
):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    paused_result = _register_paused_run(tmp_path, "run-1", "save_scenario_set")
    monkeypatch.setattr(orchestration, "run_initial_pass", lambda: paused_result)

    at = _app(ui_script_path)
    at.run()
    next(b for b in at.button if b.label == "Start Analysis").click().run()
    at = _settle(at, lambda a: _has_label(a, "radio", "Your choice"))

    assert not at.exception
    radios = [r for r in at.radio if r.label == "Your choice"]
    assert len(radios) == 1
    assert radios[0].value == "REMEDIATE_GAP"  # Decider's suggestion preselected


def test_confirm_decision_preselects_suggestion_with_realistic_multi_question_verdicts(
    monkeypatch, tmp_path, ui_script_path
):
    """Reproduces the real checkpoint shape, where per-scenario grounding
    yes/no questions are inserted into pending["verdicts"] BEFORE the
    actual coverage_action choice question (see checkpoints.py's
    build_coverage_checkpoint) -- picking "the first verdict" would
    preselect based on a grounding answer, not the Decider's real
    suggestion for the choice itself."""
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    verdicts = {
        "grounding_S03": {
            "answer": True,
            "suggestion": "true",
            "confidence": 0.9,
            "probabilities": {"true": 0.9, "false": 0.1},
            "route": "AUTO",
        },
        "coverage_action": {
            "answer": None,
            "suggestion": "ACCEPT_COVERAGE",
            "confidence": 0.55,
            "probabilities": {"ACCEPT_COVERAGE": 0.55, "REMEDIATE_GAP": 0.45},
            "route": "ESCALATE",
        },
    }
    paused_result = _register_paused_run(tmp_path, "run-1", "save_scenario_set", verdicts=verdicts)
    monkeypatch.setattr(orchestration, "run_initial_pass", lambda: paused_result)

    at = _app(ui_script_path)
    at.run()
    next(b for b in at.button if b.label == "Start Analysis").click().run()
    at = _settle(at, lambda a: _has_label(a, "radio", "Your choice"))

    radios = [r for r in at.radio if r.label == "Your choice"]
    assert radios[0].value == "ACCEPT_COVERAGE"


def test_confirm_decision_applies_choice_through_resume_pending_decision(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    paused_result = _register_paused_run(tmp_path, "run-1", "save_scenario_set")
    monkeypatch.setattr(orchestration, "run_initial_pass", lambda: paused_result)

    applied: list[tuple[str, str]] = []
    controller = orchestration._ACTIVE_RUNS["run-1"]["controller"]

    def fake_resume(run_id: str, choice: str) -> dict:
        applied.append((run_id, choice))
        # Mirrors what the real orchestration.resume_pending_decision does to
        # the controller on a non-REJECTED_RETRY outcome (clears the pending
        # decision, advances state) -- our fake only replaces the Strands
        # Agent/Decider call itself, not RunController's own contract.
        controller.set_pending_decision(None)
        controller.set_workflow_state("AWAITING_APPROVAL", stage="AWAITING_APPROVAL", busy=False)
        return {"run_id": run_id, "workflow_state": "AWAITING_APPROVAL", "stage": "AWAITING_APPROVAL"}

    monkeypatch.setattr(orchestration, "resume_pending_decision", fake_resume)

    at = _app(ui_script_path)
    at.run()
    next(b for b in at.button if b.label == "Start Analysis").click().run()
    at = _settle(at, lambda a: _has_label(a, "button", "Confirm decision"))

    next(b for b in at.button if b.label == "Confirm decision").click().run()
    # Settles once the choice has been applied and the run has moved past
    # AWAITING_DECISION_CONFIRMATION -- the Confirm decision control (and
    # its "Your choice" radio) disappears from the rendered tree.
    at = _settle(at, lambda a: not _has_label(a, "button", "Confirm decision"))

    assert not at.exception
    assert applied == [("run-1", "REMEDIATE_GAP")]


def test_duplicate_start_analysis_click_while_busy_does_not_start_second_run(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    calls: list[int] = []
    release = threading.Event()

    def slow_run_initial_pass() -> dict:
        calls.append(1)
        release.wait(timeout=2)
        return {"run_id": "run-1", "workflow_state": "STOPPED", "stage": "STOPPED"}

    monkeypatch.setattr(orchestration, "run_initial_pass", slow_run_initial_pass)

    at = _app(ui_script_path)
    at.run()
    next(b for b in at.button if b.label == "Start Analysis").click().run()
    # start_analysis() sets worker_busy under the lock synchronously before
    # returning, so this second render already reflects it deterministically
    # -- no sleep/race needed. A disabled button can't be clicked by "a
    # browser user" (AppTest enforces this itself), which is exactly the
    # guard doing its job: assert the disablement rather than forcing a
    # second click past it.
    second_start_button = next(b for b in at.button if b.label == "Start Analysis")
    assert second_start_button.disabled is True

    release.set()
    time.sleep(0.1)  # let the now-released worker thread finish cleanly before the test ends
    at.run()

    assert len(calls) == 1


def test_new_analysis_disabled_while_awaiting_decision_confirmation(monkeypatch, tmp_path, ui_script_path):
    """A run with a working Confirm decision path must not be abandonable
    via New Analysis (unlike the genuine dead-end states)."""
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    _register_paused_run(tmp_path, "run-1", "save_scenario_set")
    monkeypatch.setattr(
        orchestration,
        "run_initial_pass",
        lambda: {"run_id": "run-1", "stage": "AWAITING_DECISION_CONFIRMATION", "workflow_state": "PREPARING"},
    )

    at = _app(ui_script_path)
    at.run()
    next(b for b in at.button if b.label == "Start Analysis").click().run()
    at = _settle(at, lambda a: _has_label(a, "button", "New Analysis"))

    new_analysis_button = next(b for b in at.button if b.label == "New Analysis")
    assert new_analysis_button.disabled is True


def test_submit_clarification_disabled_when_awaiting_clarification(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr(
        orchestration,
        "run_initial_pass",
        lambda: {"run_id": "run-1", "stage": "AWAITING_CLARIFICATION", "workflow_state": "AWAITING_CLARIFICATION"},
    )
    controller = RunController("run-1", tmp_path / "run-1")
    controller.set_workflow_state("AWAITING_CLARIFICATION", stage="AWAITING_CLARIFICATION", busy=False)
    orchestration._ACTIVE_RUNS["run-1"] = {"controller": controller}

    at = _app(ui_script_path)
    at.run()
    next(b for b in at.button if b.label == "Start Analysis").click().run()
    at = _settle(at, lambda a: _has_label(a, "button", "Submit clarification"))

    submit_button = next(b for b in at.button if b.label == "Submit clarification")
    assert submit_button.disabled is True


def test_resolve_grounding_control_updates_effective_coverage_display(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    run_dir = tmp_path / "run-1"
    controller = RunController("run-1", run_dir)
    controller.set_workflow_state("AWAITING_APPROVAL", stage="AWAITING_APPROVAL", busy=False)
    rows = [
        {"scenario_id": sid, "test_id": f"T{sid[1:]}", "rule_ids": ["BR01"], "status": "VALID", "findings": []}
        for sid in ("S01", "S02", "S03", "S04", "S05")
    ]
    (run_dir / "scenarios_v1.json").write_text(json.dumps({"version": 1, "scenarios": []}))
    (run_dir / "coverage_v1.json").write_text(
        json.dumps(
            {
                "covered": 5,
                "required": 5,
                "percentage": 100.0,
                "rows": rows,
                "rule_traceability": {"BR01": ["S01", "S02", "S03", "S04", "S05"]},
            }
        )
    )
    (run_dir / "grounding_v1.json").write_text(
        json.dumps(
            {
                "scenario_set_version": 1,
                "items": [{"scenario_id": "S04", "verdict": "NEEDS_REVIEW", "confidence": 0.5, "human_resolution": None}],
            }
        )
    )
    orchestration._ACTIVE_RUNS["run-1"] = {"controller": controller}
    monkeypatch.setattr(
        orchestration,
        "run_initial_pass",
        lambda: {"run_id": "run-1", "workflow_state": "AWAITING_APPROVAL", "stage": "AWAITING_APPROVAL"},
    )

    at = _app(ui_script_path)
    at.run()
    next(b for b in at.button if b.label == "Start Analysis").click().run()
    at = _settle(at, lambda a: _has_label(a, "button", "Resolve"))

    assert not at.exception
    resolve_buttons = [b for b in at.button if b.label == "Resolve"]
    assert len(resolve_buttons) == 1

    radios = [r for r in at.radio if r.label == "Resolution"]
    radios[0].set_value("CONFIRMED_NOT_GROUNDED")
    resolve_buttons[0].click().run()

    assert not at.exception
    assert controller.unresolved_grounding_flags(1) == []
    effective = controller.effective_coverage(1)
    assert effective["covered"] == 4
    # Evidence immutability: the recorded coverage file is untouched.
    recorded = json.loads((run_dir / "coverage_v1.json").read_text())
    assert recorded["covered"] == 5


def test_tab4_audit_table_shows_real_decisions_with_suggestion_column(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    run_dir = tmp_path / "run-1"
    controller = RunController("run-1", run_dir)
    controller.record_decision(
        {
            "checkpoint": "COVERAGE",
            "decider_suggestion": "ACCEPT_COVERAGE",
            "selected_action": "Proceed",
            "confidence": 0.9,
            "route": "AUTO",
            "decision_source": "DECIDER",
            "latency_ms": 42,
            "host_label": "local-cpu",
        }
    )
    orchestration._ACTIVE_RUNS["run-1"] = {"controller": controller}
    monkeypatch.setattr(
        orchestration,
        "run_initial_pass",
        lambda: {"run_id": "run-1", "workflow_state": "AWAITING_APPROVAL", "stage": "AWAITING_APPROVAL"},
    )

    at = _app(ui_script_path)
    at.run()
    next(b for b in at.button if b.label == "Start Analysis").click().run()
    at = _settle(at, lambda a: any("suggestion" in getattr(t.value, "columns", []) for t in a.table))

    assert not at.exception
    audit_table = next(t.value for t in at.table if "suggestion" in getattr(t.value, "columns", []))
    assert audit_table["suggestion"].iloc[0] == "ACCEPT_COVERAGE"
