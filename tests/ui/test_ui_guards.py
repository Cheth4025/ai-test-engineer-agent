"""Controller-level tests for the guards src/ai_test_engineer/ui.py relies on:
Start Analysis once (duplicate clicks while active do nothing), Confirm
decision only in the right stage with a pending decision, and reload being
side-effect free (never calls either orchestration entry point). Also covers
the small pure data-access helpers the tabs read from.

No Bedrock, no GPU, no network: orchestration.run_initial_pass/
resume_pending_decision are monkeypatched to fast stand-ins wherever a
worker actually needs to run, same principle as
tests/unit/test_orchestration_stubbed.py.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from ai_test_engineer import orchestration, ui
from ai_test_engineer.state import RunController


def _wait_until_not_busy(state: ui.UIState, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while state.worker_busy and time.monotonic() < deadline:
        time.sleep(0.02)


def _controller_awaiting_confirmation(tmp_path) -> RunController:
    controller = RunController("r1", tmp_path)
    controller.set_workflow_state("PREPARING", stage="AWAITING_DECISION_CONFIRMATION", busy=False)
    controller.set_pending_decision({"tool_name": "save_scenario_set", "context": {}, "verdicts": {}})
    return controller


# ---------------------------------------------------------------------------
# can_start_analysis / start_analysis
# ---------------------------------------------------------------------------


def test_can_start_analysis_true_when_no_current_run():
    state = ui.UIState()
    assert ui.can_start_analysis(state, None) is True


def test_can_start_analysis_false_when_busy():
    state = ui.UIState()
    state.worker_busy = True
    assert ui.can_start_analysis(state, None) is False


@pytest.mark.parametrize(
    "workflow_state", ["COMPLETED", "REJECTED", "BLOCKED", "STOPPED", "AWAITING_APPROVAL", "AWAITING_CLARIFICATION"]
)
def test_can_start_analysis_true_for_dead_end_or_terminal_states(tmp_path, workflow_state):
    controller = RunController("r1", tmp_path)
    controller.set_workflow_state(workflow_state, stage=workflow_state, busy=False)
    assert ui.can_start_analysis(ui.UIState(), controller) is True


def test_can_start_analysis_false_while_awaiting_decision_confirmation(tmp_path):
    """A run paused on an escalation has a working Confirm decision path --
    New Analysis must not be allowed to silently abandon it (unlike the
    genuine dead ends AWAITING_APPROVAL/AWAITING_CLARIFICATION, which have
    no path forward in this milestone)."""
    controller = _controller_awaiting_confirmation(tmp_path)
    assert ui.can_start_analysis(ui.UIState(), controller) is False


def test_can_start_analysis_false_for_preparing(tmp_path):
    controller = RunController("r1", tmp_path)  # defaults to PREPARING
    assert ui.can_start_analysis(ui.UIState(), controller) is False


def test_start_analysis_returns_false_when_already_busy():
    state = ui.UIState()
    state.worker_busy = True
    assert ui.start_analysis(state) is False


def test_start_analysis_runs_worker_and_clears_busy_on_completion(monkeypatch):
    state = ui.UIState()
    monkeypatch.setattr(
        orchestration,
        "run_initial_pass",
        lambda: {"run_id": "r1", "workflow_state": "BLOCKED", "stage": "BLOCKED"},
    )

    assert ui.start_analysis(state) is True
    _wait_until_not_busy(state)

    assert state.worker_busy is False
    assert state.current_run_id == "r1"
    assert state.last_result["workflow_state"] == "BLOCKED"


def test_start_analysis_false_while_current_run_awaits_confirmation(tmp_path):
    """New Analysis must not be able to abandon a run that still has a
    working Confirm decision path (unlike the genuine dead-end states)."""
    controller = _controller_awaiting_confirmation(tmp_path)
    orchestration._ACTIVE_RUNS["r1"] = {"controller": controller}
    try:
        state = ui.UIState()
        state.current_run_id = "r1"
        assert ui.start_analysis(state) is False
    finally:
        orchestration._ACTIVE_RUNS.pop("r1", None)


def test_start_analysis_marks_abandoned_dead_end_run_stopped(tmp_path, monkeypatch):
    controller = RunController("r1", tmp_path)
    controller.set_workflow_state("AWAITING_APPROVAL", stage="AWAITING_APPROVAL", busy=False)
    orchestration._ACTIVE_RUNS["r1"] = {"controller": controller}
    monkeypatch.setattr(
        orchestration, "run_initial_pass", lambda: {"run_id": "r2", "workflow_state": "BLOCKED", "stage": "BLOCKED"}
    )
    try:
        state = ui.UIState()
        state.current_run_id = "r1"

        assert ui.start_analysis(state) is True
        _wait_until_not_busy(state)

        assert controller.workflow_state == "STOPPED"
        assert json.loads((tmp_path / "abandoned.json").read_text())["reason"]
    finally:
        orchestration._ACTIVE_RUNS.pop("r1", None)
        orchestration._ACTIVE_RUNS.pop("r2", None)


def test_start_analysis_adopts_new_run_id_before_worker_completes(monkeypatch, tmp_path):
    """The header must show the NEW run's id while it's still in progress,
    not a stale previous run's -- state.current_run_id should be adopted
    as soon as orchestration registers the new run, not only once the
    (possibly multi-minute) call returns."""
    release = threading.Event()

    def slow_run_initial_pass() -> dict:
        run_id = "r-new"
        orchestration._ACTIVE_RUNS[run_id] = {"controller": RunController(run_id, tmp_path / run_id)}
        release.wait(timeout=2)
        return {"run_id": run_id, "workflow_state": "STOPPED", "stage": "STOPPED"}

    monkeypatch.setattr(orchestration, "run_initial_pass", slow_run_initial_pass)
    try:
        state = ui.UIState()
        assert ui.start_analysis(state) is True
        for _ in range(100):
            if state.current_run_id == "r-new":
                break
            time.sleep(0.01)
        assert state.current_run_id == "r-new"
        assert state.worker_busy is True  # still running -- adoption is independent of completion
    finally:
        release.set()
        _wait_until_not_busy(state)
        orchestration._ACTIVE_RUNS.pop("r-new", None)


def test_run_worker_exception_marks_the_new_runs_own_controller_stopped(tmp_path):
    """A failure during Start Analysis must stop the run that actually
    failed, not whichever run the UI happened to be showing before."""
    state = ui.UIState()
    new_controller = RunController("r-new", tmp_path / "r-new")
    known_before: frozenset[str] = frozenset()

    def failing_fn() -> dict:
        orchestration._ACTIVE_RUNS["r-new"] = {"controller": new_controller}
        raise RuntimeError("boom")

    try:
        ui._run_worker(state, failing_fn, known_before)
        assert new_controller.workflow_state == "STOPPED"
        assert state.last_error == "boom"
        assert state.worker_busy is False
    finally:
        orchestration._ACTIVE_RUNS.pop("r-new", None)


def test_run_worker_exception_uses_already_known_run_for_confirm_decision(tmp_path):
    """Confirm decision resumes an EXISTING run -- no new run_id is created,
    so the exception path must fall back to state.current_run_id's
    controller rather than (incorrectly) finding nothing."""
    controller = RunController("r1", tmp_path)
    orchestration._ACTIVE_RUNS["r1"] = {"controller": controller}
    state = ui.UIState()
    state.current_run_id = "r1"
    known_before = frozenset(orchestration._ACTIVE_RUNS.keys())

    def failing_fn() -> dict:
        raise RuntimeError("boom")

    try:
        ui._run_worker(state, failing_fn, known_before)
        assert controller.workflow_state == "STOPPED"
    finally:
        orchestration._ACTIVE_RUNS.pop("r1", None)


def test_run_worker_clears_busy_even_if_stop_on_exception_itself_raises(tmp_path, monkeypatch):
    """A secondary failure while handling the first exception must not
    leave the UI permanently locked (worker_busy stuck True forever)."""
    state = ui.UIState()
    state.worker_busy = True

    def failing_fn() -> dict:
        raise RuntimeError("boom")

    monkeypatch.setattr(ui, "_stop_on_exception", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secondary")))

    ui._run_worker(state, failing_fn, frozenset())

    assert state.worker_busy is False


def test_run_worker_never_touches_streamlit_apis(monkeypatch):
    """_run_worker must be pure orchestration + UIState -- no st.* API.
    Proven by replacing ui.st with an object that raises on any attribute
    access and confirming the worker still completes cleanly."""

    class _ExplodingStreamlit:
        def __getattr__(self, name):
            raise AssertionError(f"_run_worker must never touch streamlit.{name}")

    monkeypatch.setattr(ui, "st", _ExplodingStreamlit())
    state = ui.UIState()

    ui._run_worker(state, lambda: {"run_id": "r1", "workflow_state": "STOPPED", "stage": "STOPPED"}, frozenset())

    assert state.last_result["workflow_state"] == "STOPPED"


def test_start_analysis_duplicate_click_while_busy_does_not_start_second_run(monkeypatch):
    state = ui.UIState()
    calls: list[int] = []
    release = threading.Event()

    def fake_run_initial_pass():
        calls.append(1)
        release.wait(timeout=2)
        return {"run_id": "r1", "workflow_state": "BLOCKED", "stage": "BLOCKED"}

    monkeypatch.setattr(orchestration, "run_initial_pass", fake_run_initial_pass)

    assert ui.start_analysis(state) is True
    time.sleep(0.05)  # let the worker thread actually start and register its call
    assert ui.start_analysis(state) is False  # duplicate click while active does nothing

    release.set()
    _wait_until_not_busy(state)
    assert len(calls) == 1


def test_start_analysis_worker_never_calls_streamlit_apis(monkeypatch):
    """The worker thread must be pure orchestration + UIState -- no st.* API.
    Proven by running it off-thread with Streamlit's runtime unavailable
    (no ScriptRunContext) and confirming it still completes cleanly."""
    state = ui.UIState()
    monkeypatch.setattr(
        orchestration,
        "run_initial_pass",
        lambda: {"run_id": "r1", "workflow_state": "STOPPED", "stage": "STOPPED"},
    )
    assert ui.start_analysis(state) is True
    _wait_until_not_busy(state)
    assert state.last_error is None


# ---------------------------------------------------------------------------
# can_confirm_decision / confirm_decision
# ---------------------------------------------------------------------------


def test_can_confirm_decision_true_when_stage_and_pending_match(tmp_path):
    controller = _controller_awaiting_confirmation(tmp_path)
    assert ui.can_confirm_decision(ui.UIState(), controller) is True


def test_can_confirm_decision_false_when_wrong_stage(tmp_path):
    controller = _controller_awaiting_confirmation(tmp_path)
    controller.set_workflow_state("PREPARING", stage="SCORING", busy=False)
    assert ui.can_confirm_decision(ui.UIState(), controller) is False


def test_can_confirm_decision_false_when_no_pending_decision(tmp_path):
    controller = _controller_awaiting_confirmation(tmp_path)
    controller.set_pending_decision(None)
    assert ui.can_confirm_decision(ui.UIState(), controller) is False


def test_can_confirm_decision_false_when_controller_none():
    assert ui.can_confirm_decision(ui.UIState(), None) is False


def test_can_confirm_decision_false_when_worker_busy(tmp_path):
    controller = _controller_awaiting_confirmation(tmp_path)
    state = ui.UIState()
    state.worker_busy = True
    assert ui.can_confirm_decision(state, controller) is False


def test_confirm_decision_returns_false_when_guard_fails():
    state = ui.UIState()
    state.current_run_id = "nonexistent"
    assert ui.confirm_decision(state, "ACCEPT_COVERAGE") is False


def test_confirm_decision_spawns_worker_and_applies_choice(tmp_path, monkeypatch):
    controller = _controller_awaiting_confirmation(tmp_path)
    orchestration._ACTIVE_RUNS["r1"] = {"controller": controller}
    try:
        state = ui.UIState()
        state.current_run_id = "r1"
        calls: list[tuple[str, str]] = []

        def fake_resume(run_id, choice):
            calls.append((run_id, choice))
            return {"run_id": run_id, "workflow_state": "AWAITING_APPROVAL", "stage": "AWAITING_APPROVAL"}

        monkeypatch.setattr(orchestration, "resume_pending_decision", fake_resume)

        assert ui.confirm_decision(state, "ACCEPT_COVERAGE") is True
        _wait_until_not_busy(state)

        assert calls == [("r1", "ACCEPT_COVERAGE")]
        assert state.last_result["workflow_state"] == "AWAITING_APPROVAL"
    finally:
        orchestration._ACTIVE_RUNS.pop("r1", None)


# ---------------------------------------------------------------------------
# reload is side-effect free
# ---------------------------------------------------------------------------


def _forbid_orchestration_calls(monkeypatch) -> None:
    def _boom(*args, **kwargs):
        raise AssertionError("gather_display_data must never call orchestration entry points")

    monkeypatch.setattr(orchestration, "run_initial_pass", _boom)
    monkeypatch.setattr(orchestration, "resume_pending_decision", _boom)


def test_gather_display_data_with_active_run_is_side_effect_free(tmp_path, monkeypatch):
    """Populates real version files (not an empty run dir) so the
    per-version coverage/grounding loop actually executes, with a
    corrupt mid-write grounding file to prove that path tolerates it too
    -- an empty run dir would let this test pass vacuously."""
    _forbid_orchestration_calls(monkeypatch)
    controller = RunController("r1", tmp_path)
    (tmp_path / "scenarios_v1.json").write_text(json.dumps({"version": 1, "scenarios": []}))
    (tmp_path / "coverage_v1.json").write_text(
        json.dumps({"covered": 4, "required": 5, "percentage": 80.0, "rows": [], "rule_traceability": {}})
    )
    (tmp_path / "grounding_v1.json").write_text("{not valid json")  # mid-write by the worker
    (tmp_path / "decisions.json").write_text(json.dumps([{"decision_source": "DECIDER", "latency_ms": 100}]))
    orchestration._ACTIVE_RUNS["r1"] = {"controller": controller}
    try:
        state = ui.UIState()
        state.current_run_id = "r1"
        data = ui.gather_display_data(state)  # must not raise

        assert data["controller"] is controller
        assert len(data["versions"]) == 1
        assert data["versions"][0]["effective_coverage"]["covered"] == 4
        assert data["versions"][0]["unresolved_grounding"] == []
    finally:
        orchestration._ACTIVE_RUNS.pop("r1", None)


def test_gather_display_data_with_no_current_run_is_side_effect_free(monkeypatch):
    _forbid_orchestration_calls(monkeypatch)
    state = ui.UIState()
    data = ui.gather_display_data(state)
    assert data["controller"] is None


# ---------------------------------------------------------------------------
# small pure data-access helpers
# ---------------------------------------------------------------------------


def test_load_json_returns_parsed_value(tmp_path):
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"a": 1}))
    assert ui._load_json(path) == {"a": 1}


def test_load_json_treats_missing_file_as_unavailable(tmp_path):
    assert ui._load_json(tmp_path / "missing.json") is None


def test_load_json_treats_corrupt_file_as_unavailable(tmp_path):
    path = tmp_path / "decisions.json"
    path.write_text("{not valid json")
    assert ui._load_json(path) is None


def test_options_for_known_tools():
    assert ui.options_for("save_scenario_set") == ["ACCEPT_COVERAGE", "REMEDIATE_GAP"]
    assert ui.options_for("generate_scenarios") == ["GENERATE_TESTS", "REQUEST_CLARIFICATION"]


def test_options_for_unknown_tool_raises():
    with pytest.raises(ValueError):
        ui.options_for("mystery_tool")


def test_metrics_strip_computes_calls_median_and_escalated_count():
    records = [
        {"latency_ms": 100, "route": "AUTO", "decision_source": "DECIDER"},
        {"latency_ms": 300, "route": "ESCALATED", "decision_source": "DECIDER"},
        {"latency_ms": None, "route": "ESCALATED", "decision_source": "DECIDER"},
    ]
    metrics = ui.metrics_strip(records)
    assert metrics == {"decider_calls": 3, "median_latency_ms": 200, "escalated": 2}


def test_metrics_strip_counts_only_real_decider_calls():
    """REUSED rows explicitly made no new Decider call, and a
    HUMAN_ESCALATION row is a human's answer to an already-counted
    DECIDER row -- counting either would double-count one real call."""
    records = [
        {"latency_ms": 100, "route": "ESCALATED", "decision_source": "DECIDER"},
        {"latency_ms": None, "route": "ESCALATED", "decision_source": "HUMAN_ESCALATION"},
        {"latency_ms": 100, "route": "AUTO", "decision_source": "REUSED"},
    ]
    metrics = ui.metrics_strip(records)
    assert metrics == {"decider_calls": 1, "median_latency_ms": 100, "escalated": 1}


def test_metrics_strip_empty_records():
    assert ui.metrics_strip([]) == {"decider_calls": 0, "median_latency_ms": None, "escalated": 0}


def test_available_versions_finds_present_version_files(tmp_path):
    (tmp_path / "scenarios_v1.json").write_text("{}")
    (tmp_path / "scenarios_v2.json").write_text("{}")
    (tmp_path / "coverage_v1.json").write_text("{}")  # different stem, ignored

    assert ui.available_versions(tmp_path, "scenarios") == [1, 2]


def test_available_versions_empty_when_none_present(tmp_path):
    assert ui.available_versions(tmp_path, "scenarios") == []


# ---------------------------------------------------------------------------
# parse_business_rules -- real per-BR text from requirement.json, never
# hardcoded; only the short label (BR_SHORT_LABELS) is a static lookup,
# copied verbatim from docs/AI_Test_Engineer_Agent_Requirements.md.
# ---------------------------------------------------------------------------

_REAL_REQUIREMENT_TEXT = (
    "As a customer, I want to retry a payment when the payment gateway times out, "
    "without being charged twice.\n"
    "BR01: Every payment request must include a unique idempotency key for its "
    "logical payment; retries reuse that key.\n"
    "BR02: Reusing the same key with the same payload resolves the original "
    "payment and must not create another charge.\n"
    "BR03: Reusing the same key with a different payment amount is rejected; the "
    "original charge and ledger remain unchanged.\n"
    "BR04: A timeout must not automatically mark the payment as failed. Retain "
    "its unresolved outcome until status verification or a callback establishes "
    "the result.\n"
    "BR05: Verify the original payment status before initiating another charge. "
    "If already successful, return the original result. If unresolved, do not "
    "create a new charge; retain the unresolved outcome until the scripted "
    "original success is available.\n"
    "BR06: Re-delivery of the same successful callback, identified by callback "
    "ID and payment ID, must not create another financial ledger posting."
)


def test_parse_business_rules_extracts_all_six_in_order():
    rules = ui.parse_business_rules(_REAL_REQUIREMENT_TEXT)

    assert [r["rule_id"] for r in rules] == ["BR01", "BR02", "BR03", "BR04", "BR05", "BR06"]
    assert rules[0]["label"] == "Idempotency key"
    assert rules[0]["description"].startswith("Every payment request must include")
    assert rules[5]["label"] == "Callback idempotency"


def test_parse_business_rules_skips_the_non_rule_opening_line():
    rules = ui.parse_business_rules(_REAL_REQUIREMENT_TEXT)
    assert not any("As a customer" in r["description"] for r in rules)


def test_parse_business_rules_empty_or_none_text():
    assert ui.parse_business_rules("") == []
    assert ui.parse_business_rules(None) == []


# ---------------------------------------------------------------------------
# added_by_remediation -- pure scenario_id set diff, no new field needed.
# ---------------------------------------------------------------------------


def test_added_by_remediation_finds_the_new_scenario_id():
    previous = [{"scenario_id": "S01"}, {"scenario_id": "S02"}, {"scenario_id": "S03"}, {"scenario_id": "S05"}]
    current = [
        {"scenario_id": "S01"},
        {"scenario_id": "S02"},
        {"scenario_id": "S03"},
        {"scenario_id": "S04"},
        {"scenario_id": "S05"},
    ]
    assert ui.added_by_remediation(previous, current) == {"S04"}


def test_added_by_remediation_empty_when_no_previous_version():
    current = [{"scenario_id": "S01"}, {"scenario_id": "S02"}]
    assert ui.added_by_remediation(None, current) == set()


def test_added_by_remediation_empty_when_sets_are_identical():
    scenarios = [{"scenario_id": "S01"}, {"scenario_id": "S02"}]
    assert ui.added_by_remediation(scenarios, scenarios) == set()


# ---------------------------------------------------------------------------
# split_shared_assertions -- real required_assertions tuples from
# config/scenario_catalog.json (inlined here, not re-read from disk, same
# style as this file's other fixtures), the literal tuple-exact intersection
# across all five scenarios.
# ---------------------------------------------------------------------------

_SHARED_FIVE = [
    {"observation": "gateway_charge_count", "operator": "EQ", "expected": 1},
    {"observation": "ledger_success_count", "operator": "EQ", "expected": 1},
    {"observation": "payment_state", "operator": "EQ", "expected": "SUCCESS"},
    {"observation": "amount", "operator": "EQ", "expected": "100.00"},
    {"observation": "currency", "operator": "EQ", "expected": "USD"},
]

_REAL_PER_SCENARIO_ASSERTIONS = {
    "S01": list(_SHARED_FIVE),
    "S02": [
        {"observation": "timeout_not_failed", "operator": "EQ", "expected": True},
        {"observation": "status_verification_before_retry_charge", "operator": "PRECEDES", "expected": True},
        {"observation": "same_idempotency_key", "operator": "EQ", "expected": True},
        {"observation": "same_payload", "operator": "EQ", "expected": True},
        *_SHARED_FIVE,
    ],
    "S03": [
        {"observation": "conflict_rejected", "operator": "EQ", "expected": True},
        {"observation": "original_financial_records_unchanged", "operator": "EQ", "expected": True},
        *_SHARED_FIVE,
    ],
    "S04": list(_SHARED_FIVE),
    "S05": [
        {"observation": "timeout_not_failed", "operator": "EQ", "expected": True},
        {"observation": "delayed_success_resolves_original", "operator": "EQ", "expected": True},
        {"observation": "same_idempotency_key", "operator": "EQ", "expected": True},
        {"observation": "same_payload", "operator": "EQ", "expected": True},
        *_SHARED_FIVE,
    ],
}


def test_split_shared_assertions_finds_the_real_five_tuple_shared_by_all():
    shared, _ = ui.split_shared_assertions(_REAL_PER_SCENARIO_ASSERTIONS)

    assert {(a["observation"], a["operator"], a["expected"]) for a in shared} == {
        (a["observation"], a["operator"], a["expected"]) for a in _SHARED_FIVE
    }
    # ledger_success_count first -- that's the assertion T04 is about.
    assert shared[0]["observation"] == "ledger_success_count"


def test_split_shared_assertions_remainder_is_scenario_specific():
    _, remainder = ui.split_shared_assertions(_REAL_PER_SCENARIO_ASSERTIONS)

    assert {a["observation"] for a in remainder["S03"]} == {
        "conflict_rejected",
        "original_financial_records_unchanged",
    }
    # T04/S04 has no scenario-specific assertions beyond the shared five --
    # the UI renders "shared checks only" for this, not a blank cell.
    assert remainder["S04"] == []


def test_split_shared_assertions_empty_input():
    shared, remainder = ui.split_shared_assertions({})
    assert shared == []
    assert remainder == {}


# ---------------------------------------------------------------------------
# can_approve / can_reject -- enforced for real in RunController.
# record_approval; these mirror can_start_analysis/can_confirm_decision's
# existing pattern so the UI can disable buttons before a doomed click.
# ---------------------------------------------------------------------------


def _awaiting_approval_controller(tmp_path) -> RunController:
    controller = RunController("r1", tmp_path)
    controller.set_workflow_state("AWAITING_APPROVAL", stage="AWAITING_APPROVAL", busy=False)
    rows = [
        {"scenario_id": sid, "test_id": f"T{sid[1:]}", "rule_ids": ["BR01"], "status": "VALID", "findings": []}
        for sid in ("S01", "S02", "S03", "S04", "S05")
    ]
    (tmp_path / "coverage_v1.json").write_text(
        json.dumps({"covered": 5, "required": 5, "percentage": 100.0, "rows": rows, "rule_traceability": {}})
    )
    return controller


def test_can_approve_true_when_ready(tmp_path):
    controller = _awaiting_approval_controller(tmp_path)
    assert ui.can_approve(ui.UIState(), controller, "Chethan", 1) is True


def test_can_approve_false_when_reviewer_blank(tmp_path):
    controller = _awaiting_approval_controller(tmp_path)
    assert ui.can_approve(ui.UIState(), controller, "   ", 1) is False


def test_can_approve_false_when_not_awaiting_approval(tmp_path):
    controller = RunController("r1", tmp_path)  # PREPARING
    assert ui.can_approve(ui.UIState(), controller, "Chethan", 1) is False


def test_can_approve_false_when_blockers_open(tmp_path):
    controller = RunController("r1", tmp_path)
    controller.set_workflow_state("AWAITING_APPROVAL", stage="AWAITING_APPROVAL", busy=False)
    rows = [{"scenario_id": "S01", "test_id": "T01", "rule_ids": ["BR01"], "status": "MISSING", "findings": []}]
    (tmp_path / "coverage_v1.json").write_text(
        json.dumps({"covered": 0, "required": 1, "percentage": 0.0, "rows": rows, "rule_traceability": {}})
    )
    assert ui.can_approve(ui.UIState(), controller, "Chethan", 1) is False


def test_can_approve_false_when_already_recorded(tmp_path):
    controller = _awaiting_approval_controller(tmp_path)
    controller.record_approval("APPROVE", "Chethan", 1)
    assert ui.can_approve(ui.UIState(), controller, "Someone Else", 1) is False


def test_can_reject_true_despite_open_blockers(tmp_path):
    controller = RunController("r1", tmp_path)
    controller.set_workflow_state("AWAITING_APPROVAL", stage="AWAITING_APPROVAL", busy=False)
    rows = [{"scenario_id": "S01", "test_id": "T01", "rule_ids": ["BR01"], "status": "MISSING", "findings": []}]
    (tmp_path / "coverage_v1.json").write_text(
        json.dumps({"covered": 0, "required": 1, "percentage": 0.0, "rows": rows, "rule_traceability": {}})
    )
    assert ui.can_reject(ui.UIState(), controller, "Chethan") is True


def test_can_reject_false_when_reviewer_blank(tmp_path):
    controller = _awaiting_approval_controller(tmp_path)
    assert ui.can_reject(ui.UIState(), controller, "") is False


def test_can_reject_false_when_not_awaiting_approval(tmp_path):
    controller = RunController("r1", tmp_path)  # PREPARING
    assert ui.can_reject(ui.UIState(), controller, "Chethan") is False


def test_can_approve_and_can_reject_false_when_controller_none():
    assert ui.can_approve(ui.UIState(), None, "Chethan", 1) is False
    assert ui.can_reject(ui.UIState(), None, "Chethan") is False


def test_can_approve_and_can_reject_false_when_worker_busy(tmp_path):
    controller = _awaiting_approval_controller(tmp_path)
    state = ui.UIState()
    state.worker_busy = True
    assert ui.can_approve(state, controller, "Chethan", 1) is False
    assert ui.can_reject(state, controller, "Chethan") is False


# ---------------------------------------------------------------------------
# format_assertion / decider_decision_value
# ---------------------------------------------------------------------------


def test_format_assertion_eq_operator():
    assert ui.format_assertion({"observation": "ledger_success_count", "operator": "EQ", "expected": 1}) == (
        "ledger_success_count == 1"
    )


def test_format_assertion_precedes_operator():
    assertion = {"observation": "status_verification_before_retry_charge", "operator": "PRECEDES", "expected": True}
    assert ui.format_assertion(assertion) == "status_verification_before_retry_charge precedes True"


def test_decider_decision_value_human_escalation_uses_selected_action_directly():
    record = {"decision_source": "HUMAN_ESCALATION", "selected_action": "REMEDIATE_GAP", "per_question": []}
    assert ui.decider_decision_value(record) == "REMEDIATE_GAP (human)"


def test_decider_decision_value_decider_reads_coverage_action_answer_not_selected_action():
    """selected_action for a DECIDER record is "Proceed"/a Deny-reason
    string, never the bounded choice -- the real choice lives in
    per_question's coverage_action entry."""
    record = {
        "decision_source": "DECIDER",
        "selected_action": "Proceed",
        "per_question": [
            {"question": "grounding_S01", "answer": True},
            {"question": "coverage_action", "answer": "ACCEPT_COVERAGE"},
        ],
    }
    assert ui.decider_decision_value(record) == "ACCEPT_COVERAGE"


def test_decider_decision_value_awaiting_human_when_coverage_action_answer_missing():
    """The escalation-moment record (action is Confirm, selected_action is
    just the gate's internal action name) has coverage_action's answer as
    None since no one has picked yet -- shows "Awaiting human", never the
    internal "Confirm" action name as if it were a real decision (live-run
    bug: the UI showed "Decision: Confirm" for a still-pending escalation)."""
    record = {
        "decision_source": "DECIDER",
        "selected_action": "Confirm",
        "per_question": [{"question": "coverage_action", "answer": None}],
    }
    assert ui.decider_decision_value(record) == "Awaiting human"


def test_decider_decision_value_rejected_by_guard_overrides_everything_else():
    """A human pick a deterministic guard rejected (live-run evidence:
    "ACCEPT_COVERAGE" picked while coverage was 4/5, rejected with
    rejection_reason "5/5 required") must never render as if it were a
    normal, accepted decision."""
    record = {
        "decision_source": "HUMAN_ESCALATION",
        "selected_action": "ACCEPT_COVERAGE",
        "decision_status": "REJECTED",
        "rejection_reason": "5/5 required",
        "per_question": [{"question": "coverage_action", "answer": None}],
    }
    assert ui.decider_decision_value(record) == "Rejected by guard: 5/5 required"


def test_coverage_action_confidence_reads_the_question_not_the_batch_level_field():
    """The coverage_action question's own confidence, distinct from the
    record's top-level "confidence" (the batch's lowest-scoring verdict,
    compared against a different threshold -- see coverage_action_
    confidence's docstring)."""
    record = {
        "confidence": 0.5812,  # batch-level -- must be ignored here
        "per_question": [
            {"question": "grounding_S01", "confidence": 0.5812},
            {"question": "coverage_action", "confidence": 0.8711},
        ],
    }
    assert ui.coverage_action_confidence(record) == 0.8711


def test_coverage_action_confidence_none_when_question_absent():
    assert ui.coverage_action_confidence({"per_question": []}) is None


def test_round4_rounds_floats_and_passes_through_none():
    assert ui.round4(0.41879999999999995) == 0.4188
    assert ui.round4(None) is None


def test_round4_probabilities_rounds_each_value():
    assert ui.round4_probabilities({"true": 0.41879999999999995, "false": 0.5812}) == {
        "true": 0.4188,
        "false": 0.5812,
    }
