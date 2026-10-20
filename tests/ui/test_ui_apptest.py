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


def test_app_loads_on_overview_with_nav_and_no_start_button(monkeypatch, tmp_path, ui_script_path):
    """Overview is static reference content only (polish-1): Start Analysis
    moved to the new Analysis nav item, which is now the 2nd of 5 pages."""
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    at = _app(ui_script_path)
    at.run()

    assert not at.exception
    nav_labels = {b.key: b.label for b in at.sidebar.button if b.key and b.key.startswith("nav_")}
    assert nav_labels == {
        "nav_requirements": "Overview",
        "nav_analysis": "Analysis",
        "nav_scenarios": "Scenarios",
        "nav_execution": "Execution",
        "nav_results": "Results",
    }
    assert at.title[0].value == "Overview"
    assert any("Payment Retry & Duplicate Charge Prevention" in c.value for c in at.caption)
    assert not any(b.label == "Start analysis" for b in at.button)


def test_analysis_page_has_the_start_button(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    at = _app(ui_script_path)
    at.run()
    at = _navigate(at, "analysis")

    assert not at.exception
    assert at.title[0].value == "Analysis"
    assert any(b.label == "Start analysis" for b in at.button)


def test_sidebar_navigation_reaches_all_five_pages_with_right_titles(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    at = _app(ui_script_path)
    at.run()

    expected_titles = {
        "requirements": "Overview",
        "analysis": "Analysis",
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
    for nav_id in ("analysis", "scenarios", "execution", "results", "requirements"):
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
    # The use-case card is now rendered as one aae-card HTML block (plan
    # section C "use st.table/.../st.info for styled sections" is replaced
    # by ui_components.card()/labeled_section()) -- content lives inside
    # at.markdown, not a separate widget type.
    assert "aae-label-problem" in markdown_text
    assert "aae-label-impact" in markdown_text
    assert "aae-label-mitigation" in markdown_text
    # \$ (escaped) -- a bare "$100" is parsed as inline LaTeX math by
    # st.markdown (caught visually; see ui.py's _USE_CASE_PROBLEM comment).
    # labeled_section() escapes internally -- a literal backslash does not
    # need html-escaping, so it still appears verbatim.
    assert "A customer pays \\$100 once" in markdown_text  # PROBLEM copy
    assert "Inflated revenue" in markdown_text  # IMPACT copy
    assert "Test T04 catches the duplicate" in markdown_text  # MITIGATION copy

    assert "aae-table" in markdown_text
    assert "BR01" in markdown_text and "BR06" in markdown_text
    assert "Idempotency key" in markdown_text
    assert "Loaded from the requirements catalog" in markdown_text


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
    # Now inside the Test run card's HTML body (ui_components.card()), not
    # a native st.info.
    assert any("Results appear here after execution." in m.value for m in at.markdown)

    at = _navigate(at, "results")
    assert any("Results appear after the tests run." in i.value for i in at.info)


# ---------------------------------------------------------------------------
# Confirm decision / pending-decision sidebar notice (unchanged guarantees,
# adapted to the new nav)
# ---------------------------------------------------------------------------


def _register_paused_run(
    tmp_path, run_id: str, tool_name: str, verdicts: dict | None = None, escalation_seq: int = 1
) -> dict:
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
    pending = {"tool_name": tool_name, "context": {}, "verdicts": verdicts, "escalation_seq": escalation_seq}
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
    at = _navigate(at, "analysis")
    next(b for b in at.button if b.label == "Start analysis").click().run()
    at = _settle(at, lambda a: any("Analysis" in w.value for w in a.sidebar.warning))

    assert not at.exception
    assert any(
        w.value == "A decision needs your confirmation — see 'Analysis'." for w in at.sidebar.warning
    )


def test_sidebar_shows_approval_notice_when_awaiting_approval(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    controller = _register_remediated_run(tmp_path)

    at = _app(ui_script_path)
    at.run()
    at = _start_into_fixture(at, monkeypatch, controller)
    at = _settle(at, lambda a: any("Approval needed" in w.value for w in a.sidebar.warning))

    assert not at.exception
    assert any(w.value == "Approval needed — see 'Execution'." for w in at.sidebar.warning)
    # Exactly one notice -- never both the decision and approval wording at once.
    assert not any("A decision needs your confirmation" in w.value for w in at.sidebar.warning)


# ---------------------------------------------------------------------------
# Action banners -- a top-of-page "Action needed" notice, present only
# while a human action is actually pending, on the page that hosts the
# control (same-page "Jump to it" link) and on Overview (a "Go to <page>"
# button, since Overview itself doesn't host anything).
# ---------------------------------------------------------------------------


def test_no_action_banner_anywhere_before_any_run(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    at = _app(ui_script_path)
    at.run()

    for nav_id in ("requirements", "scenarios", "execution", "results"):
        at = _navigate(at, nav_id)
        assert not any("Action needed" in m.value for m in at.markdown)


def test_action_banner_on_hosting_page_and_overview_for_pending_decision(monkeypatch, tmp_path, ui_script_path):
    """The action banner is now an aae-banner HTML block (ui_components.
    banner()), not a native st.warning -- the same-page link is a real
    <a href="#aae-action-anchor"> anchor embedded in the banner; the
    cross-page case is the banner plus a real st.button beside it. A
    pending decision is answered only on Analysis (polish-1): Scenarios
    now gets the cross-page "Go to Analysis" case too, same as Overview."""
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    paused_result = _register_paused_run(tmp_path, "run-1", "save_scenario_set")
    monkeypatch.setattr(orchestration, "run_initial_pass", lambda: paused_result)

    at = _app(ui_script_path)
    at.run()
    at = _navigate(at, "analysis")
    next(b for b in at.button if b.label == "Start analysis").click().run()
    at = _settle(at, lambda a: any("Action needed below" in m.value for m in a.markdown))

    markdown_text = " ".join(m.value for m in at.markdown)
    assert "confirm the Decider decision" in markdown_text
    assert '<a href="#aae-action-anchor">Jump to it</a>' in markdown_text

    at = _navigate(at, "scenarios")
    markdown_text = " ".join(m.value for m in at.markdown)
    assert "Action needed" in markdown_text and "see 'Analysis'" in markdown_text
    assert any(b.label == "Go to Analysis" for b in at.button)

    at = _navigate(at, "requirements")
    markdown_text = " ".join(m.value for m in at.markdown)
    assert "Action needed" in markdown_text and "see 'Analysis'" in markdown_text
    assert any(b.label == "Go to Analysis" for b in at.button)


def test_action_banner_on_overview_for_awaiting_approval(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    controller = _register_remediated_run(tmp_path)

    at = _app(ui_script_path)
    at.run()
    at = _start_into_fixture(at, monkeypatch, controller)
    at = _navigate(at, "requirements")
    at = _settle(at, lambda a: any("Action needed" in m.value for m in a.markdown))

    markdown_text = " ".join(m.value for m in at.markdown)
    assert "record the approval decision" in markdown_text and "see 'Execution'" in markdown_text
    assert any(b.label == "Go to Execution" for b in at.button)

    at = _navigate(at, "execution")
    markdown_text = " ".join(m.value for m in at.markdown)
    assert "Action needed below" in markdown_text and "record the approval decision" in markdown_text


def test_confirm_decision_radio_has_no_preselection_and_confirm_disabled_until_chosen(
    monkeypatch, tmp_path, ui_script_path
):
    """The live BLOCKED-run bug (20261018T191604Z_0ad068bc) traced back to
    this radio preselecting the Decider's suggestion by default -- a human
    pick REMEDIATE_GAP got silently reverted to that default and the
    reverted value (ACCEPT_COVERAGE) was submitted instead, which the 5/5
    guard then rejected. Fix: no preselection at all (index=None), and
    Confirm stays disabled until the human has actually picked something.
    Also reproduces the real checkpoint shape, where per-scenario grounding
    yes/no questions are inserted into pending["verdicts"] BEFORE the
    actual coverage_action choice question."""
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
    at = _navigate(at, "analysis")
    next(b for b in at.button if b.label == "Start analysis").click().run()
    at = _settle(at, lambda a: _has_label(a, "radio", "Your choice"))

    radio = next(r for r in at.radio if r.label == "Your choice")
    assert radio.value is None  # never preselects the Decider's suggestion
    assert next(b for b in at.button if b.label == "Confirm decision").disabled is True

    radio.set_value("ACCEPT_COVERAGE").run()
    assert next(b for b in at.button if b.label == "Confirm decision").disabled is False


def test_confirm_decision_choice_survives_fragment_reruns_and_submits_exactly_that_choice(
    monkeypatch, tmp_path, ui_script_path
):
    """Regression test for the live BLOCKED-run bug: picks the option that
    does NOT match the Decider's own suggestion (REMEDIATE_GAP's suggestion
    here is implicitly ACCEPT_COVERAGE-adjacent via default verdicts, see
    _register_paused_run) so a silent revert-to-default would be caught,
    re-renders several times with no further interaction -- the same thing
    a st.fragment(run_every=2) auto-refresh does -- and confirms the choice
    never reverts and the exact chosen value is what gets submitted."""
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
    at = _navigate(at, "analysis")
    next(b for b in at.button if b.label == "Start analysis").click().run()
    at = _settle(at, lambda a: _has_label(a, "radio", "Your choice"))

    # The default verdicts' suggestion is REMEDIATE_GAP -- deliberately pick
    # the OTHER option so a silent revert to the suggestion would be caught.
    next(r for r in at.radio if r.label == "Your choice").set_value("ACCEPT_COVERAGE").run()

    # Several re-renders with no further interaction -- what a fragment's
    # periodic auto-refresh does.
    for _ in range(5):
        at.run()
        assert next(r for r in at.radio if r.label == "Your choice").value == "ACCEPT_COVERAGE"

    next(b for b in at.button if b.label == "Confirm decision").click().run()
    at = _settle(at, lambda a: not _has_label(a, "button", "Confirm decision"))

    assert not at.exception
    assert applied == [("run-1", "ACCEPT_COVERAGE")]


def test_second_escalation_in_same_run_gets_a_fresh_unanswered_radio(monkeypatch, tmp_path, ui_script_path):
    """Reproduces the second half of the live BLOCKED-run bug: after a
    REMEDIATE_GAP, the RESCORING-stage COVERAGE escalation that follows is
    a SEPARATE escalation in the same run and must not inherit whatever the
    first escalation's radio was left holding -- it needs its own fresh,
    unanswered widget (checkpoints.py's escalation_seq, which changes
    between the two)."""
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    paused_result = _register_paused_run(tmp_path, "run-1", "save_scenario_set", escalation_seq=1)
    monkeypatch.setattr(orchestration, "run_initial_pass", lambda: paused_result)
    controller = orchestration._ACTIVE_RUNS["run-1"]["controller"]

    at = _app(ui_script_path)
    at.run()
    at = _navigate(at, "analysis")
    next(b for b in at.button if b.label == "Start analysis").click().run()
    at = _settle(at, lambda a: _has_label(a, "radio", "Your choice"))

    next(r for r in at.radio if r.label == "Your choice").set_value("REMEDIATE_GAP").run()
    assert next(r for r in at.radio if r.label == "Your choice").value == "REMEDIATE_GAP"

    # A later, second escalation in the SAME run -- a new pending_decision
    # with a different escalation_seq, same shape checkpoints.py produces
    # after a remediation pass re-escalates at RESCORING.
    controller.set_pending_decision(
        {
            "tool_name": "save_scenario_set",
            "context": {},
            "verdicts": {
                "coverage_action": {
                    "answer": None,
                    "suggestion": "ACCEPT_COVERAGE",
                    "confidence": 0.87,
                    "probabilities": {"ACCEPT_COVERAGE": 0.87, "REMEDIATE_GAP": 0.13},
                    "route": "ESCALATE",
                }
            },
            "escalation_seq": 2,
        }
    )
    at.run()

    new_radio = next(r for r in at.radio if r.label == "Your choice")
    assert new_radio.value is None
    assert next(b for b in at.button if b.label == "Confirm decision").disabled is True


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
    at = _navigate(at, "analysis")
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
    at = _navigate(at, "analysis")
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
    at = _navigate(at, "analysis")
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
            "per_question": [
                {"question": "coverage_action", "answer": None, "suggestion": "REMEDIATE_GAP", "confidence": confidence}
            ],
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
            "per_question": [
                {"question": "coverage_action", "answer": "ACCEPT_COVERAGE", "suggestion": "ACCEPT_COVERAGE", "confidence": 0.97}
            ],
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
    """Start analysis lives only on the Analysis page (polish-1) -- navigate
    there first so every caller can click it regardless of which page the
    AppTest instance currently has selected."""
    monkeypatch.setattr(
        orchestration,
        "run_initial_pass",
        lambda: {"run_id": controller.run_id, "workflow_state": "AWAITING_APPROVAL", "stage": "AWAITING_APPROVAL"},
    )
    at = _navigate(at, "analysis")
    next(b for b in at.button if b.label == "Start analysis").click().run()
    return at


def test_scenarios_page_shows_v2_with_s04_added_by_remediation_badge(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    controller = _register_remediated_run(tmp_path)

    at = _app(ui_script_path)
    at.run()
    at = _start_into_fixture(at, monkeypatch, controller)
    at = _navigate(at, "scenarios")
    at = _settle(at, lambda a: any("aae-table" in m.value for m in a.markdown))

    assert not at.exception
    # The scenario table is now one aae-table HTML block (ui_components.
    # table()); S04's "Added by remediation" badge is its own Rule-match
    # pill (not appended to the Scenario title text) and its row carries
    # the aae-row--added highlight class.
    markdown_text = " ".join(m.value for m in at.markdown)
    assert "S04" in markdown_text
    assert "Duplicate successful callback" in markdown_text
    assert 'aae-pill--added-remediation">Added by remediation</span>' in markdown_text
    assert "aae-row--added" in markdown_text
    # v1 (the default-latest segmented_control value is v2, but v1's own
    # table must never show the badge for S04 -- it's not on this page's
    # default render) is covered by the diff-based helper's own unit tests;
    # here we confirm the real v2 render wires it through end to end.


def test_scenarios_page_shows_coverage_donuts_before_and_after(monkeypatch, tmp_path, ui_script_path):
    """st.altair_chart elements aren't a trackable AppTest node type (always
    empty via at.get("altair_chart")), so this checks the real caption each
    donut renders right after itself instead -- proves both actually
    rendered with the correct before/after coverage, not just that no
    exception occurred. The caption is now an aae-caption HTML block
    (ui_components.small_caption()) in at.markdown, not a native st.caption."""
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    controller = _register_remediated_run(tmp_path)

    at = _app(ui_script_path)
    at.run()
    at = _start_into_fixture(at, monkeypatch, controller)
    at = _navigate(at, "scenarios")
    at = _settle(at, lambda a: any("Before" in m.value for m in a.markdown))

    assert not at.exception
    markdown_text = " ".join(m.value for m in at.markdown)
    assert "Before · coverage_v1 · 4 of 5" in markdown_text
    assert "After · coverage_v2 · 5 of 5" in markdown_text


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


def _register_run_with_unresolved_rule_match_blocker(tmp_path) -> RunController:
    """AWAITING_APPROVAL with coverage 5/5 (not the blocker under test) but
    one grounding item still NEEDS_REVIEW/unresolved -- isolates the
    approval_blockers() "grounding flag not resolved" wording specifically."""
    run_dir = tmp_path / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "scenarios_v1.json").write_text(
        json.dumps({"version": 1, "scenarios": [{"scenario_id": "S03", "title": "x", "rule_ids": ["BR03"]}]})
    )
    (run_dir / "coverage_v1.json").write_text(
        json.dumps({"covered": 5, "required": 5, "percentage": 100.0, "rows": [], "rule_traceability": {}})
    )
    (run_dir / "grounding_v1.json").write_text(
        json.dumps(
            {
                "scenario_set_version": 1,
                "items": [{"scenario_id": "S03", "verdict": "NEEDS_REVIEW", "confidence": 0.5, "human_resolution": None}],
            }
        )
    )
    controller = RunController("run-1", run_dir)
    controller.set_workflow_state("AWAITING_APPROVAL", stage="AWAITING_APPROVAL", busy=False)
    orchestration._ACTIVE_RUNS["run-1"] = {"controller": controller}
    return controller


def test_execution_blocker_text_says_rule_match_check_not_grounding_flag(monkeypatch, tmp_path, ui_script_path):
    """Presentation-only wording (plan C6-8): approval_blockers()'s real
    internal string still says "grounding flag" (state.py, unchanged) --
    the UI translates it to "rule-match check" at the render boundary."""
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    controller = _register_run_with_unresolved_rule_match_blocker(tmp_path)

    at = _app(ui_script_path)
    at.run()
    at = _start_into_fixture(at, monkeypatch, controller)
    at = _navigate(at, "execution")
    at = _settle(at, lambda a: any("S03" in m.value for m in a.markdown))

    markdown_text = " ".join(m.value for m in at.markdown)
    assert "rule-match check not resolved" in markdown_text
    assert "grounding flag" not in markdown_text


def test_execution_ready_banner_says_rule_match_checks_not_grounding_flags(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    controller = _register_remediated_run(tmp_path)  # no blockers -- the "Ready for approval" banner

    at = _app(ui_script_path)
    at.run()
    at = _start_into_fixture(at, monkeypatch, controller)
    at = _navigate(at, "execution")
    at = _settle(at, lambda a: any("Ready for approval" in m.value for m in a.markdown))

    markdown_text = " ".join(m.value for m in at.markdown)
    assert "rule-match checks" in markdown_text
    assert "grounding flags" not in markdown_text


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
    # The blocker banner is now an aae-banner HTML block (ui_components.
    # banner()), not a native st.warning.
    assert any("Locked until the run reaches AWAITING_APPROVAL" in m.value for m in at.markdown)


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


def test_execution_execute_unlocks_after_approval(monkeypatch, tmp_path, ui_script_path):
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

    assert next(b for b in at.button if b.label == "Execute").disabled is False
    # The reason caption is now an aae-caption HTML block (ui_components.
    # small_caption()), not a native st.caption.
    assert any("Ready to run the five fixed tests." in m.value for m in at.markdown)


def test_execution_shows_fixed_manifest_with_real_expected_assertions(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    controller = _register_remediated_run(tmp_path)

    at = _app(ui_script_path)
    at.run()
    at = _start_into_fixture(at, monkeypatch, controller)
    at = _navigate(at, "execution")
    at = _settle(at, lambda a: any("aae-table" in m.value for m in a.markdown))

    # The manifest is now one aae-table HTML block (ui_components.table()).
    markdown_text = " ".join(m.value for m in at.markdown)
    assert "T01" in markdown_text and "T05" in markdown_text
    assert "shared checks only" in markdown_text  # T04's Expected cell
    assert "conflict_rejected" in markdown_text  # T03's Expected cell
    assert "ledger_success_count == 1" in markdown_text


# ---------------------------------------------------------------------------
# Milestone 6: Execute unlock, the Test run card, and the Results page.
# Fixtures only -- real T04-evidence shape, no live execute_approved_run.
# ---------------------------------------------------------------------------


def _write_completed_execution_fixture(run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "coverage_final.json").write_text(
        json.dumps({"covered": 5, "required": 5, "percentage": 100.0, "rows": [], "rule_traceability": {}})
    )
    (run_dir / "approval.json").write_text(
        json.dumps({"decision": "APPROVE", "reviewer": "Chethan", "comment": None, "timestamp": "2026-10-18T19:00:00Z"})
    )
    results = {
        "execution_id": "run-1",
        "return_code": 1,
        "duration_ms": 412,
        "tests": [
            {"test_id": "T01", "scenario_id": "S01", "rule_ids": ["BR01"], "status": "PASS", "duration_ms": 3, "expected": "1 charge", "actual": "1 charge", "failure_message": None, "evidence_reference": "execution/T01_evidence.json"},
            {"test_id": "T02", "scenario_id": "S02", "rule_ids": ["BR01"], "status": "PASS", "duration_ms": 4, "expected": "1 charge", "actual": "1 charge", "failure_message": None, "evidence_reference": "execution/T02_evidence.json"},
            {"test_id": "T03", "scenario_id": "S03", "rule_ids": ["BR01", "BR03"], "status": "PASS", "duration_ms": 2, "expected": "Conflict", "actual": "Conflict", "failure_message": None, "evidence_reference": "execution/T03_evidence.json"},
            {"test_id": "T04", "scenario_id": "S04", "rule_ids": ["BR06"], "status": "FAIL", "duration_ms": 5, "expected": "1 ledger entry", "actual": "2 ledger entries", "failure_message": "assert 2 == 1", "evidence_reference": "execution/T04_evidence.json"},
            {"test_id": "T05", "scenario_id": "S05", "rule_ids": ["BR01"], "status": "PASS", "duration_ms": 6, "expected": "1 charge", "actual": "1 charge", "failure_message": None, "evidence_reference": "execution/T05_evidence.json"},
        ],
        "junit_reference": "execution/junit.xml",
        "log_reference": "execution/pytest_output.log",
    }
    (run_dir / "results.json").write_text(json.dumps(results))

    execution_dir = run_dir / "execution"
    execution_dir.mkdir(parents=True, exist_ok=True)
    (execution_dir / "pytest_output.log").write_text("tests/payment/test_payment.py::test_t04_duplicate_callback FAILED")
    (execution_dir / "T04_evidence.json").write_text(
        json.dumps(
            {
                "test_id": "T04",
                "ledger": [
                    {"entry_id": "ledger-1", "payment_id": "P-001", "amount": "100.00", "currency": "USD", "callback_id": "C-001"},
                    {"entry_id": "ledger-2", "payment_id": "P-001", "amount": "100.00", "currency": "USD", "callback_id": "C-001"},
                ],
                "events": [
                    {"sequence": 1, "action": "callback_delivered", "payment_id": "P-001", "callback_id": "C-001", "details": {"delivery_number": 1}},
                    {"sequence": 2, "action": "callback_delivered", "payment_id": "P-001", "callback_id": "C-001", "details": {"delivery_number": 2}},
                ],
            }
        )
    )

    analysis = {
        "has_failure": True,
        "test_id": "T04",
        "rule_ids": ["BR06"],
        "classification": "BUSINESS_RULE_VIOLATION",
        "expected_behavior": "BR06: Re-delivery of the same successful callback, identified by callback ID and payment ID, must not create another financial ledger posting.",
        "actual_behavior": "Observed 2 ledger entries instead of 1 ledger entry.",
        "impact": ["Books overstate the payment.", "Reconciliation against the gateway disagrees."],
        "evidence_references": ["execution/T04_evidence.json"],
        "handling_action": "REPORT_FAILURE",
        "investigated": False,
    }
    (run_dir / "analysis.json").write_text(json.dumps(analysis))

    decisions = [
        {
            "checkpoint": "COVERAGE",
            "decision_source": "DECIDER",
            "decision_status": "VALIDATED",
            "confidence": 0.97,
            "threshold": 0.75,
            "route": "AUTO",
            "per_question": [{"question": "coverage_action", "answer": "ACCEPT_COVERAGE", "confidence": 0.97}],
        },
        {
            "checkpoint": "FAILURE",
            "decision_source": "DECIDER",
            "decision_status": "VALIDATED",
            "confidence": 0.95,
            "threshold": 0.75,
            "route": "AUTO",
            "per_question": [
                {"question": "failure_class", "answer": "BUSINESS_RULE_VIOLATION", "confidence": 0.95},
                {"question": "failure_action", "answer": "REPORT_FAILURE", "confidence": 0.9},
            ],
        },
        {
            "checkpoint": "COMPLETION",
            "decision_source": "DECIDER",
            "decision_status": "VALIDATED",
            "confidence": 0.95,
            "threshold": 0.75,
            "route": "AUTO",
            "per_question": [{"question": "completion_action", "answer": "FINALIZE_RESULTS", "confidence": 0.95}],
        },
    ]
    (run_dir / "decisions.json").write_text(json.dumps(decisions))

    report_text = (
        "# AI Test Engineer Agent -- Report\n\n"
        "**Agent workflow:** COMPLETED  \n**Payment verification:** FAILED\n"
    )
    (run_dir / "report.md").write_text(report_text, encoding="utf-8")
    (run_dir / "report_metadata.json").write_text(
        json.dumps({"agent_workflow_status": "COMPLETED", "payment_verification_status": "FAILED"})
    )


def _register_completed_run(tmp_path) -> RunController:
    run_dir = tmp_path / "run-1"
    _write_completed_execution_fixture(run_dir)
    controller = RunController("run-1", run_dir)
    controller.set_workflow_state("COMPLETED", stage="COMPLETED", busy=False)
    orchestration._ACTIVE_RUNS["run-1"] = {"controller": controller}
    return controller


def _register_approved_run(tmp_path) -> RunController:
    run_dir = tmp_path / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "coverage_final.json").write_text(
        json.dumps({"covered": 5, "required": 5, "percentage": 100.0, "rows": [], "rule_traceability": {}})
    )
    (run_dir / "approval.json").write_text(
        json.dumps({"decision": "APPROVE", "reviewer": "Chethan", "comment": None, "timestamp": "2026-10-18T19:00:00Z"})
    )
    controller = RunController("run-1", run_dir)
    controller.set_workflow_state("APPROVED", stage="APPROVED", busy=False)
    orchestration._ACTIVE_RUNS["run-1"] = {"controller": controller}
    return controller


def test_execute_button_unlocks_when_approved(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    controller = _register_approved_run(tmp_path)

    at = _app(ui_script_path)
    at.run()
    at = _start_into_fixture(at, monkeypatch, controller)
    at = _navigate(at, "execution")
    at = _settle(at, lambda a: _has_label(a, "button", "Execute"))

    assert next(b for b in at.button if b.label == "Execute").disabled is False
    assert any("Ready to execute" in w.value for w in at.sidebar.warning)


def test_execution_test_run_summary_matches_the_mock_wording(monkeypatch, tmp_path, ui_script_path):
    """Plan C6-8's execution summary format: "5/5 run · 4 passed · 1
    failed · 1.0s" -- was "{passed}/{total} completed · {duration}s"."""
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    controller = _register_completed_run(tmp_path)
    monkeypatch.setattr(
        orchestration, "run_initial_pass", lambda: {"run_id": "run-1", "workflow_state": "COMPLETED", "stage": "COMPLETED"}
    )

    at = _app(ui_script_path)
    at.run()
    at = _navigate(at, "analysis")
    next(b for b in at.button if b.label == "Start analysis").click().run()
    at = _navigate(at, "execution")
    at = _settle(at, lambda a: any("aae-table" in m.value for m in a.markdown))

    markdown_text = " ".join(m.value for m in at.markdown)
    assert "5/5 run · 4 passed · 1 failed · 0.4s" in markdown_text


def test_results_page_shows_banners_kpis_and_t04_row(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    controller = _register_completed_run(tmp_path)
    monkeypatch.setattr(
        orchestration, "run_initial_pass", lambda: {"run_id": "run-1", "workflow_state": "COMPLETED", "stage": "COMPLETED"}
    )

    at = _app(ui_script_path)
    at.run()
    at = _navigate(at, "analysis")
    next(b for b in at.button if b.label == "Start analysis").click().run()
    at = _navigate(at, "results")
    at = _settle(at, lambda a: any("Agent workflow" in m.value for m in a.markdown))

    assert not at.exception
    # Verdict banners and the Test results table are now ui_components.
    # verdict()/table() HTML (point A.6: no st.success/st.error/st.table
    # for these styled sections), not native success/error/table elements.
    markdown_text = " ".join(m.value for m in at.markdown)
    assert "Agent workflow: COMPLETED" in markdown_text
    assert "aae-banner--workflow-ok" in markdown_text
    assert "Payment verification: FAILED" in markdown_text
    assert "aae-banner--verification-fail" in markdown_text

    # KPI tiles (ui_components.kpi_row()) -- never previously tested.
    assert "aae-kpi-row" in markdown_text
    assert "Total tests" in markdown_text and ">5<" in markdown_text
    assert "aae-kpi-value--passed" in markdown_text
    assert "aae-kpi-value--failed" in markdown_text

    assert "T01" in markdown_text and "T05" in markdown_text
    assert "1 ledger entry" in markdown_text  # T04 Expected
    assert "2 ledger entries" in markdown_text  # T04 Actual
    assert 'aae-pill--fail">FAIL</span>' in markdown_text
    assert "aae-row--fail" in markdown_text

    assert "Observed 2 ledger entries instead of 1 ledger entry." in markdown_text
    assert "BR06: Re-delivery" in markdown_text
    assert "Developer fix. The agent reports defects; it does not change code." in markdown_text
    assert "root cause" not in markdown_text.lower()


def test_results_page_decider_trace_spans_every_checkpoint(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    controller = _register_completed_run(tmp_path)
    monkeypatch.setattr(
        orchestration, "run_initial_pass", lambda: {"run_id": "run-1", "workflow_state": "COMPLETED", "stage": "COMPLETED"}
    )

    at = _app(ui_script_path)
    at.run()
    at = _navigate(at, "analysis")
    next(b for b in at.button if b.label == "Start analysis").click().run()
    at = _navigate(at, "results")
    at = _settle(at, lambda a: any("COVERAGE" in m.value for m in a.markdown))

    markdown_text = " ".join(m.value for m in at.markdown)
    assert "COVERAGE" in markdown_text and "ACCEPT_COVERAGE" in markdown_text
    assert "FAILURE" in markdown_text and "REPORT_FAILURE" in markdown_text
    assert "COMPLETION" in markdown_text and "FINALIZE_RESULTS" in markdown_text


def test_results_page_decision_trace_matches_report_md_for_the_same_run(monkeypatch, tmp_path, ui_script_path):
    """Item 5's literal cross-surface check: the Results page (real
    Streamlit rendering, via AppTest) and reporting.render_report (real
    report.md generation) read the exact same decisions.json/decider.json
    for run-1 and must show the identical decision text + confidence/τ for
    every row -- both go through decision_trace.build_decision_trace, the
    one shared formatter."""
    from ai_test_engineer import decision_trace, reporting

    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    controller = _register_completed_run(tmp_path)
    monkeypatch.setattr(
        orchestration, "run_initial_pass", lambda: {"run_id": "run-1", "workflow_state": "COMPLETED", "stage": "COMPLETED"}
    )

    at = _app(ui_script_path)
    at.run()
    at = _navigate(at, "analysis")
    next(b for b in at.button if b.label == "Start analysis").click().run()
    at = _navigate(at, "results")
    at = _settle(at, lambda a: any("COVERAGE" in m.value for m in a.markdown))
    ui_markdown_text = " ".join(m.value for m in at.markdown)

    decisions = json.loads((controller.run_dir / "decisions.json").read_text())
    decider_cfg = json.loads((Path(__file__).resolve().parents[2] / "config" / "decider.json").read_text())
    expected_rows = decision_trace.build_decision_trace(decisions, decider_cfg)
    assert len(expected_rows) >= 3  # COVERAGE, FAILURE, COMPLETION -- the pairing collapsed nothing unexpected

    report_text = reporting.render_report(controller)["report_text"]

    for row in expected_rows:
        assert f"{row['icon']} {row['decision_text']}" in ui_markdown_text or row["decision_text"] in ui_markdown_text
        assert f"{row['icon']} {row['decision_text']}" in report_text
        if row["confidence"] is not None:
            assert str(row["confidence"]) in ui_markdown_text
            assert f"{row['confidence']} vs {row['threshold']}" in report_text


def test_results_page_evidence_tabs_and_downloads(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    controller = _register_completed_run(tmp_path)
    monkeypatch.setattr(
        orchestration, "run_initial_pass", lambda: {"run_id": "run-1", "workflow_state": "COMPLETED", "stage": "COMPLETED"}
    )

    at = _app(ui_script_path)
    at.run()
    at = _navigate(at, "analysis")
    next(b for b in at.button if b.label == "Start analysis").click().run()
    at = _navigate(at, "results")
    at = _settle(at, lambda a: len(a.tabs) >= 3)

    tab_labels = [tab.label for tab in at.tabs]
    assert tab_labels == ["Callbacks", "Ledger entries", "Test log"]

    download_labels = {b.label for b in at.download_button}
    assert download_labels == {"Download report (.md)", "Download evidence (.zip)"}
    assert not any("Download" in (b.label or "") for b in at.button)  # not the locked st.button fallback


def test_results_page_placeholder_when_no_results_yet(monkeypatch, tmp_path, ui_script_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    controller = _register_remediated_run(tmp_path)

    at = _app(ui_script_path)
    at.run()
    at = _start_into_fixture(at, monkeypatch, controller)
    at = _navigate(at, "results")

    assert any("Results appear after the tests run." in i.value for i in at.info)
