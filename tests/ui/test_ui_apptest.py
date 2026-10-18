"""Streamlit AppTest smoke tests for src/ai_test_engineer/ui.py.

Navigation is a sidebar icon+label st.button per item (docs/mockups/
README.md row 1, owner-approved layout change) -- unlike the former
st.tabs, only the SELECTED page's content and controls exist in the
rendered tree, so most tests here navigate to the right page with
_navigate() before looking for a control it owns.

No live runs: every fixture here is a hand-written run directory (JSON
files under a temp AITE_ARTIFACT_ROOT), matching the real shapes
RunController/orchestration produce. Nothing here calls
scripts/run_initial_pass.py, a real Decider server, or Bedrock --
orchestration.run_initial_pass/resume_pending_decision are monkeypatched
to fast stand-ins wherever a worker actually needs to run (same principle
as tests/unit/test_orchestration_stubbed.py).
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
    other once the first instance actually drove a click/worker thread. A
    bare get_ui_state.clear() call made from outside any Streamlit script
    context also does not reach that same keyed entry. Calling .clear()
    from a script that DOES have a real context -- this wrapper -- reaches
    it correctly, with no test-only hook needed in ui.py's own production
    code."""
    wrapper = tmp_path / "ui_test_wrapper.py"
    wrapper.write_text(
        "import streamlit as st\n"
        "import ai_test_engineer.ui as ui\n"
        # Only once per AppTest session (st.session_state IS correctly
        # session-scoped) -- clearing on every rerun would also wipe
        # worker_busy/current_run_id/nav_id set by this same test's own
        # earlier clicks, breaking intra-test persistence.
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
    """Re-renders (at.run(), like a Refresh status tick) until predicate(at)
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


def _navigate(at: AppTest, nav_id: str) -> AppTest:
    """Clicks a sidebar nav button -- the same action a user clicking it
    performs. Never starts work by itself (proven by
    test_navigating_never_calls_orchestration_entry_points)."""
    next(b for b in at.sidebar.button if b.key == f"nav_{nav_id}").click().run()
    return at


# ---------------------------------------------------------------------------
# Nav / layout
# ---------------------------------------------------------------------------


def test_app_loads_on_overview_with_nav_and_start_button(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    at = _app(ui_script_path)
    at.run()

    assert not at.exception
    nav_labels = {b.key: b.label for b in at.sidebar.button if b.key and b.key.startswith("nav_")}
    assert nav_labels == {
        "nav_requirements": "Overview",
        "nav_scenarios": "Scenarios",
        "nav_execution": "Execution",
        "nav_results": "Results",
    }
    assert at.title[0].value == "Overview"
    assert any("Payment Retry & Duplicate Charge Prevention" in c.value for c in at.caption)
    assert any(b.label == "Start analysis" for b in at.button)


def test_sidebar_navigation_reaches_all_four_pages_with_right_titles(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    at = _app(ui_script_path)
    at.run()

    expected_titles = {
        "requirements": "Overview",
        "scenarios": "Scenarios",
        "execution": "Execution",
        "results": "Results",
    }
    for nav_id, title in expected_titles.items():
        at = _navigate(at, nav_id)
        assert not at.exception
        assert at.title[0].value == title


def test_navigating_never_calls_orchestration_entry_points(monkeypatch, tmp_path, ui_script_path):
    """Selecting a sidebar page is read-only, exactly like the former
    st.tabs -- it must never call either orchestration entry point."""

    def _boom(*args, **kwargs):
        raise AssertionError("navigating pages must never call this")

    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr(orchestration, "run_initial_pass", _boom)
    monkeypatch.setattr(orchestration, "resume_pending_decision", _boom)

    at = _app(ui_script_path)
    at.run()
    for nav_id in ("scenarios", "execution", "results", "requirements"):
        at = _navigate(at, nav_id)
        assert not at.exception


def test_plain_reload_never_calls_orchestration_entry_points(monkeypatch, tmp_path, ui_script_path):
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


# ---------------------------------------------------------------------------
# Overview page content
# ---------------------------------------------------------------------------


def test_overview_shows_use_case_copy_and_real_br_table(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    at = _app(ui_script_path)
    at.run()

    assert not at.exception
    markdown_text = " ".join(m.value for m in at.markdown)
    # \$ (escaped) -- a bare "$100" is parsed as inline LaTeX math by
    # st.markdown (caught visually; see ui.py's _USE_CASE_PROBLEM comment).
    assert "A customer pays \\$100 once" in markdown_text  # PROBLEM copy
    assert "Inflated revenue" in markdown_text  # IMPACT copy
    assert "Test T04 catches the duplicate" in markdown_text  # MITIGATION copy

    br_table = next(t.value for t in at.table if "ID" in list(t.value.columns))
    assert list(br_table["ID"]) == ["BR01", "BR02", "BR03", "BR04", "BR05", "BR06"]
    assert br_table[br_table["ID"] == "BR01"]["Rule"].iloc[0] == "Idempotency key"


def test_overview_shows_acceptance_criteria_and_agent_steps(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    at = _app(ui_script_path)
    at.run()

    markdown_text = " ".join(m.value for m in at.markdown)
    assert "Use idempotency to prevent duplicate charges" in markdown_text
    assert "Waits for human approval, runs T01" in markdown_text


def test_approval_and_results_placeholders_before_any_run(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    at = _app(ui_script_path)
    at.run()

    at = _navigate(at, "scenarios")
    assert any("Start analysis to generate scenarios." in i.value for i in at.info)

    at = _navigate(at, "execution")
    assert any("Results appear here after execution (Milestone 6)." in i.value for i in at.info)

    at = _navigate(at, "results")
    assert any("Results appear after the tests run (Milestone 6)." in i.value for i in at.info)


# ---------------------------------------------------------------------------
# Confirm decision / pending-decision sidebar notice (unchanged guarantees,
# adapted to the new nav)
# ---------------------------------------------------------------------------


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
    if tool_name == "save_scenario_set":
        # A real save_scenario_set checkpoint always has scenarios_v1.json/
        # coverage_v1.json on disk by the time it can escalate (both are
        # written by build_questions' _compute(), before Decider is ever
        # asked) -- without them here, the Scenarios page's "no versions
        # yet" placeholder would suppress the Confirm decision control,
        # which cannot happen in a real run.
        controller.save_evidence(
            "scenarios_v1.json",
            {"version": 1, "scenarios": [{"scenario_id": "S01", "title": "Initial success", "rule_ids": ["BR01"]}]},
        )
        controller.save_evidence(
            "coverage_v1.json",
            {
                "covered": 1,
                "required": 5,
                "percentage": 20.0,
                "rows": [{"scenario_id": "S01", "test_id": "T01", "rule_ids": ["BR01"], "status": "VALID", "findings": []}],
                "rule_traceability": {},
            },
        )
    orchestration._ACTIVE_RUNS[run_id] = {"controller": controller}
    return {
        "run_id": run_id,
        "stage": "AWAITING_DECISION_CONFIRMATION",
        "workflow_state": "PREPARING",
        "pending_decision": pending,
    }


def test_sidebar_shows_notice_pointing_to_page_with_pending_decision(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    paused_result = _register_paused_run(tmp_path, "run-1", "save_scenario_set")
    monkeypatch.setattr(orchestration, "run_initial_pass", lambda: paused_result)

    at = _app(ui_script_path)
    at.run()
    next(b for b in at.button if b.label == "Start analysis").click().run()
    at = _settle(at, lambda a: any("Scenarios" in w.value for w in a.sidebar.warning))

    assert not at.exception
    assert any("Scenarios" in w.value for w in at.sidebar.warning)


def test_confirm_decision_preselects_suggestion_with_realistic_multi_question_verdicts(
    monkeypatch, tmp_path, ui_script_path
):
    """Reproduces the real checkpoint shape, where per-scenario grounding
    yes/no questions are inserted into pending["verdicts"] BEFORE the
    actual coverage_action choice question -- picking "the first verdict"
    would preselect based on a grounding answer, not the Decider's real
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
    next(b for b in at.button if b.label == "Start analysis").click().run()
    at = _navigate(at, "scenarios")
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
        controller.set_pending_decision(None)
        controller.set_workflow_state("AWAITING_APPROVAL", stage="AWAITING_APPROVAL", busy=False)
        return {"run_id": run_id, "workflow_state": "AWAITING_APPROVAL", "stage": "AWAITING_APPROVAL"}

    monkeypatch.setattr(orchestration, "resume_pending_decision", fake_resume)

    at = _app(ui_script_path)
    at.run()
    next(b for b in at.button if b.label == "Start analysis").click().run()
    at = _navigate(at, "scenarios")
    at = _settle(at, lambda a: _has_label(a, "button", "Confirm decision"))

    next(b for b in at.button if b.label == "Confirm decision").click().run()
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
    next(b for b in at.button if b.label == "Start analysis").click().run()
    second_start_button = next(b for b in at.button if b.label == "Start analysis")
    assert second_start_button.disabled is True  # disabled buttons can't be re-clicked

    release.set()
    time.sleep(0.1)
    at.run()

    assert len(calls) == 1


def test_new_analysis_disabled_while_awaiting_decision_confirmation(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    _register_paused_run(tmp_path, "run-1", "save_scenario_set")
    monkeypatch.setattr(
        orchestration,
        "run_initial_pass",
        lambda: {"run_id": "run-1", "stage": "AWAITING_DECISION_CONFIRMATION", "workflow_state": "PREPARING"},
    )

    at = _app(ui_script_path)
    at.run()
    next(b for b in at.button if b.label == "Start analysis").click().run()
    at = _settle(at, lambda a: _has_label(a, "button", "New Analysis"))

    assert next(b for b in at.button if b.label == "New Analysis").disabled is True


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
    next(b for b in at.button if b.label == "Start analysis").click().run()
    at = _settle(at, lambda a: _has_label(a, "button", "Submit clarification"))

    assert next(b for b in at.button if b.label == "Submit clarification").disabled is True


# ---------------------------------------------------------------------------
# Scenarios page: a real v1 (4/5) -> v2 (5/5, S04 added) remediation fixture
# ---------------------------------------------------------------------------


def _write_remediation_fixture(run_dir: Path, confidence: float = 0.62) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)

    def _scenario(sid: str, title: str, rule_ids: list[str]) -> dict:
        return {
            "scenario_id": sid,
            "title": title,
            "rule_ids": rule_ids,
            "test_id": f"T{sid[1:]}",
            "gherkin": f"Scenario: {title}\n  Given ...\n  When ...\n  Then ...",
        }

    v1_scenarios = [
        _scenario("S01", "Initial success", ["BR01"]),
        _scenario("S02", "Timeout and identical retry", ["BR01", "BR02", "BR04", "BR05"]),
        _scenario("S03", "Same key with conflicting amount", ["BR01", "BR03"]),
        _scenario("S05", "Delayed success after timeout", ["BR01", "BR02", "BR04", "BR05"]),
    ]
    v2_scenarios = v1_scenarios + [_scenario("S04", "Duplicate successful callback", ["BR06"])]

    (run_dir / "scenarios_v1.json").write_text(json.dumps({"version": 1, "scenarios": v1_scenarios}))
    (run_dir / "scenarios_v2.json").write_text(json.dumps({"version": 2, "scenarios": v2_scenarios}))

    def _row(sid: str, status: str) -> dict:
        return {"scenario_id": sid, "test_id": f"T{sid[1:]}", "rule_ids": ["BR01"], "status": status, "findings": []}

    coverage_v1 = {
        "covered": 4,
        "required": 5,
        "percentage": 80.0,
        "rows": [_row(s, "VALID") for s in ("S01", "S02", "S03", "S05")] + [_row("S04", "MISSING")],
        "rule_traceability": {},
    }
    coverage_v2 = {
        "covered": 5,
        "required": 5,
        "percentage": 100.0,
        "rows": [_row(s, "VALID") for s in ("S01", "S02", "S03", "S04", "S05")],
        "rule_traceability": {},
    }
    (run_dir / "coverage_v1.json").write_text(json.dumps(coverage_v1))
    (run_dir / "coverage_v2.json").write_text(json.dumps(coverage_v2))

    grounding_v1 = {
        "scenario_set_version": 1,
        "items": [
            {"scenario_id": s, "verdict": "GROUNDED", "confidence": 0.95, "human_resolution": None}
            for s in ("S01", "S02", "S03", "S05")
        ],
    }
    grounding_v2 = {
        "scenario_set_version": 2,
        "items": grounding_v1["items"]
        + [{"scenario_id": "S04", "verdict": "GROUNDED", "confidence": 0.93, "human_resolution": None}],
    }
    (run_dir / "grounding_v1.json").write_text(json.dumps(grounding_v1))
    (run_dir / "grounding_v2.json").write_text(json.dumps(grounding_v2))

    decisions = [
        {
            "checkpoint": "COVERAGE",
            "workflow_state": "PREPARING",
            "selected_action": "Confirm",
            "decider_suggestion": "REMEDIATE_GAP",
            "confidence": confidence,
            "route": "ESCALATED",
            "decision_source": "DECIDER",
            "threshold": 0.75,
            "latency_ms": 120,
            "host_label": "local-cpu",
            "per_question": [{"question": "coverage_action", "answer": None, "suggestion": "REMEDIATE_GAP"}],
        },
        {
            "checkpoint": "COVERAGE",
            "workflow_state": "PREPARING",
            "selected_action": "REMEDIATE_GAP",
            "decider_suggestion": {"coverage_action": "REMEDIATE_GAP"},
            "confidence": None,
            "route": "ESCALATED",
            "decision_source": "HUMAN_ESCALATION",
            "threshold": None,
            "latency_ms": None,
            "host_label": None,
            "per_question": [{"question": "coverage_action", "answer": "REMEDIATE_GAP", "suggestion": "REMEDIATE_GAP"}],
        },
        {
            "checkpoint": "COVERAGE",
            "workflow_state": "AWAITING_APPROVAL",
            "selected_action": "Proceed",
            "decider_suggestion": "ACCEPT_COVERAGE",
            "confidence": 0.97,
            "route": "AUTO",
            "decision_source": "DECIDER",
            "threshold": 0.75,
            "latency_ms": 95,
            "host_label": "local-cpu",
            "per_question": [{"question": "coverage_action", "answer": "ACCEPT_COVERAGE", "suggestion": "ACCEPT_COVERAGE"}],
        },
    ]
    (run_dir / "decisions.json").write_text(json.dumps(decisions))


def _register_remediated_run(tmp_path, confidence: float = 0.62) -> RunController:
    run_dir = tmp_path / "run-1"
    _write_remediation_fixture(run_dir, confidence=confidence)
    controller = RunController("run-1", run_dir)
    controller.set_workflow_state("AWAITING_APPROVAL", stage="AWAITING_APPROVAL", busy=False)
    orchestration._ACTIVE_RUNS["run-1"] = {"controller": controller}
    return controller


def _start_into_fixture(at: AppTest, monkeypatch, controller: RunController) -> AppTest:
    monkeypatch.setattr(
        orchestration,
        "run_initial_pass",
        lambda: {"run_id": controller.run_id, "workflow_state": "AWAITING_APPROVAL", "stage": "AWAITING_APPROVAL"},
    )
    next(b for b in at.button if b.label == "Start analysis").click().run()
    return at


def test_scenarios_page_shows_v2_with_s04_added_by_remediation_badge(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    controller = _register_remediated_run(tmp_path)

    at = _app(ui_script_path)
    at.run()
    at = _start_into_fixture(at, monkeypatch, controller)
    at = _navigate(at, "scenarios")
    at = _settle(at, lambda a: any("Scenario" in getattr(t.value, "columns", []) for t in a.table))

    assert not at.exception
    scenarios_table = next(t.value for t in at.table if "Scenario" in getattr(t.value, "columns", []))
    s04_row = scenarios_table[scenarios_table["ID"] == "S04"]
    assert "Added by remediation" in s04_row["Scenario"].iloc[0]
    # v1 (the default-latest segmented_control value is v2, but v1's own
    # table must never show the badge for S04 -- it's not on this page's
    # default render) is covered by the diff-based helper's own unit tests;
    # here we confirm the real v2 render wires it through end to end.


def test_scenarios_page_shows_coverage_donuts_before_and_after(monkeypatch, tmp_path, ui_script_path):
    """st.altair_chart elements aren't a trackable AppTest node type (always
    empty via at.get("altair_chart")), so this checks the real caption each
    donut renders right after itself instead -- proves both actually
    rendered with the correct before/after coverage, not just that no
    exception occurred."""
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    controller = _register_remediated_run(tmp_path)

    at = _app(ui_script_path)
    at.run()
    at = _start_into_fixture(at, monkeypatch, controller)
    at = _navigate(at, "scenarios")
    at = _settle(at, lambda a: any("Before" in c.value for c in a.caption))

    assert not at.exception
    captions = [c.value for c in at.caption]
    assert any("Before · coverage_v1 · 4 of 5" in c for c in captions)
    assert any("After · coverage_v2 · 5 of 5" in c for c in captions)


def test_scenarios_page_decider_decision_block_shows_fixture_confidence(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    controller = _register_remediated_run(tmp_path, confidence=0.4242)

    at = _app(ui_script_path)
    at.run()
    at = _start_into_fixture(at, monkeypatch, controller)
    at = _navigate(at, "scenarios")
    at = _settle(at, lambda a: any("0.4242" in m.value for m in a.markdown))

    assert not at.exception
    markdown_text = " ".join(m.value for m in at.markdown)
    assert "0.4242" in markdown_text
    assert "REMEDIATE_GAP" in markdown_text  # the real bounded choice, not "Confirm"/"Proceed"


def test_scenarios_page_gherkin_tabs_one_per_scenario(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    controller = _register_remediated_run(tmp_path)

    at = _app(ui_script_path)
    at.run()
    at = _start_into_fixture(at, monkeypatch, controller)
    at = _navigate(at, "scenarios")
    at = _settle(at, lambda a: len(a.tabs) >= 5)

    assert not at.exception
    assert any("S04" in tab.label for tab in at.tabs)


# ---------------------------------------------------------------------------
# Execution page: approval flow
# ---------------------------------------------------------------------------


def test_execution_inputs_locked_when_not_awaiting_approval(monkeypatch, tmp_path, ui_script_path):
    """Reviewer name/Comment must be locked the same way the buttons
    already are -- previously only the buttons checked workflow_state ==
    AWAITING_APPROVAL (via can_approve/can_reject); the inputs stayed
    editable even before a run reached that state."""
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    controller = _register_remediated_run(tmp_path)
    controller.set_workflow_state("PREPARING", stage="PREPARING", busy=False)

    at = _app(ui_script_path)
    at.run()
    at = _start_into_fixture(at, monkeypatch, controller)
    at = _navigate(at, "execution")
    at = _settle(at, lambda a: _has_label(a, "text_input", "Reviewer name"))

    assert not at.exception
    reviewer_input = next(t for t in at.text_input if t.label == "Reviewer name")
    comment_input = next(t for t in at.text_input if t.label == "Comment (optional)")
    assert reviewer_input.disabled is True
    assert comment_input.disabled is True
    assert any("Locked until the run reaches AWAITING_APPROVAL" in w.value for w in at.warning)


def test_execution_approve_requires_reviewer_name(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    controller = _register_remediated_run(tmp_path)

    at = _app(ui_script_path)
    at.run()
    at = _start_into_fixture(at, monkeypatch, controller)
    at = _navigate(at, "execution")
    at = _settle(at, lambda a: _has_label(a, "button", "Approve"))

    assert next(b for b in at.button if b.label == "Approve").disabled is True  # reviewer name still blank

    reviewer_input = next(t for t in at.text_input if t.label == "Reviewer name")
    reviewer_input.set_value("Chethan").run()

    assert next(b for b in at.button if b.label == "Approve").disabled is False


def test_execution_approve_writes_approval_json_once_and_locks_both_buttons(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    controller = _register_remediated_run(tmp_path)

    at = _app(ui_script_path)
    at.run()
    at = _start_into_fixture(at, monkeypatch, controller)
    at = _navigate(at, "execution")
    at = _settle(at, lambda a: _has_label(a, "button", "Approve"))

    next(t for t in at.text_input if t.label == "Reviewer name").set_value("Chethan").run()
    next(b for b in at.button if b.label == "Approve").click().run()

    assert not at.exception
    approval = json.loads((controller.run_dir / "approval.json").read_text())
    assert approval == {
        "run_id": "run-1",
        "decision": "APPROVE",
        "reviewer": "Chethan",
        "comment": None,
        "timestamp": approval["timestamp"],
    }

    # Both buttons lock once recorded -- no inputs, no second write possible.
    assert next(b for b in at.button if b.label == "Reject").disabled is True
    assert next(b for b in at.button if b.label == "Approve").disabled is True
    assert not any(t.label == "Reviewer name" for t in at.text_input)


def test_execution_execute_always_disabled_even_after_approval(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    controller = _register_remediated_run(tmp_path)

    at = _app(ui_script_path)
    at.run()
    at = _start_into_fixture(at, monkeypatch, controller)
    at = _navigate(at, "execution")
    at = _settle(at, lambda a: _has_label(a, "button", "Execute"))
    assert next(b for b in at.button if b.label == "Execute").disabled is True

    next(t for t in at.text_input if t.label == "Reviewer name").set_value("Chethan").run()
    next(b for b in at.button if b.label == "Approve").click().run()

    assert next(b for b in at.button if b.label == "Execute").disabled is True
    assert any("Milestone 6" in c.value for c in at.caption)


def test_execution_shows_fixed_manifest_with_real_expected_assertions(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    controller = _register_remediated_run(tmp_path)

    at = _app(ui_script_path)
    at.run()
    at = _start_into_fixture(at, monkeypatch, controller)
    at = _navigate(at, "execution")
    at = _settle(at, lambda a: any("ID" in getattr(t.value, "columns", []) for t in a.table))

    manifest_table = next(t.value for t in at.table if "Expected" in getattr(t.value, "columns", []))
    assert list(manifest_table["ID"]) == ["T01", "T02", "T03", "T04", "T05"]
    t04_row = manifest_table[manifest_table["ID"] == "T04"]
    assert t04_row["Expected"].iloc[0] == "shared checks only"
    t03_row = manifest_table[manifest_table["ID"] == "T03"]
    assert "conflict_rejected" in t03_row["Expected"].iloc[0]
    assert any("ledger_success_count == 1" in c.value for c in at.caption)
