"""Unit tests for ai_test_engineer.checkpoints: payment-domain Decider wiring.

All httpx.MockTransport responses here are unit-test-only -- never real
Decider output. Mirrors tests/unit/test_decider_gate.py's make_client +
_make_event pattern for anything that needs to drive DeciderGuardrail itself;
tests of resolve()/build_questions() in isolation call them directly with
hand-built Verdict fixtures, no HTTP involved.
"""

from __future__ import annotations

import json

import httpx
import pytest
from strands.hooks.events import BeforeToolCallEvent
from strands.interventions import Confirm, Deny, Proceed

from ai_test_engineer.checkpoints import (
    HaltGuard,
    StageGuard,
    apply_human_decision,
    build_all,
    build_coverage_checkpoint,
    build_generate_scenarios_checkpoint,
    full_coverage,
    grounding_items,
    remediation_feedback,
    resolve_host_label,
)
from ai_test_engineer.state import RunController
from ai_test_engineer.tools import AttemptLog
from decider_gate.client import DeciderClient
from decider_gate.gate import Verdict

CATALOG = {
    "required_scenarios": ["S01", "S02", "S03", "S04", "S05"],
    "scenarios": {
        sid: {
            "scenario_id": sid,
            "test_id": f"T{sid[1:]}",
            "rule_ids": ["BR01"],
            "required_preconditions": ["precondition"],
            "required_action_codes": ["CODE"],
            "required_assertions": [{"observation": "obs", "operator": "EQ", "expected": 1}],
        }
        for sid in ["S01", "S02", "S03", "S04", "S05"]
    },
}

ONE_RULE_INTERPRETATION = {
    "rules": [
        {"rule_id": "BR01", "source_excerpt": "excerpt", "meaning": "meaning of BR01", "expected_observations": ["obs"]}
    ],
    "ambiguities": [],
}

DECIDER_CFG = {
    "url": "http://decider.test",
    "model": "m",
    "host_label": "local-cpu",
    "choice_threshold": 0.75,
    "noul_upper": 0.80,
    "noul_lower": 0.40,
    "timeouts": {"connect_s": 5, "call_s_cpu": 30, "call_s_gpu": 10},
}


def _valid_scenario(sid: str) -> dict:
    return {
        "scenario_id": sid,
        "title": f"title {sid}",
        "rule_ids": ["BR01"],
        "test_id": f"T{sid[1:]}",
        "preconditions": ["precondition"],
        "actions": ["action"],
        "action_codes": ["CODE"],
        "expected_assertions": [{"observation": "obs", "operator": "EQ", "expected": 1}],
        "gherkin": f"Given... When... Then... ({sid})",
    }


def _choice_verdict(question: str, choice: str, confidence: float = 0.9) -> Verdict:
    return Verdict(
        question=question,
        answer=choice,
        confidence=confidence,
        route="AUTO",
        suggestion=choice,
        probabilities={choice: confidence},
        latency_ms=10,
        host_label="local-cpu",
        model="m",
    )


def _pending_verdict_fields(suggestion: str, confidence: float = 0.5, route: str = "ESCALATE") -> dict:
    """Shape on_decision stores per-question on controller.pending_decision["verdicts"]."""
    return {
        "answer": suggestion if route == "AUTO" else None,
        "suggestion": suggestion,
        "confidence": confidence,
        "probabilities": {suggestion: confidence},
        "route": route,
    }


def _noul_verdict(question: str, answer: bool, confidence: float = 0.9) -> Verdict:
    return Verdict(
        question=question,
        answer=answer,
        confidence=confidence,
        route="AUTO",
        suggestion=answer,
        probabilities={"true": confidence if answer else 1 - confidence, "false": 1 - confidence if answer else confidence},
        latency_ms=10,
        host_label="local-cpu",
        model="m",
    )


def _setup_coverage_checkpoint(tmp_path, present_ids: list[str]):
    attempt_log = AttemptLog()
    attempt_log.record_success("interpret_requirement", 1, ONE_RULE_INTERPRETATION)
    scenario_set = {
        "version": 1,
        "scenarios": [_valid_scenario(sid) for sid in present_ids],
        "omission_event": None,
    }
    attempt_log.record_success("generate_scenarios", 1, scenario_set)
    controller = RunController("test-run", tmp_path)
    holder: dict = {}
    checkpoint = build_coverage_checkpoint(attempt_log, CATALOG, controller, holder)
    checkpoint.build_questions(None)
    return checkpoint, controller, holder


def make_client(handler, timeout_s=30, connect_timeout_s=5):
    transport = httpx.MockTransport(handler)
    return DeciderClient(url="http://decider.test", timeout_s=timeout_s, connect_timeout_s=connect_timeout_s, transport=transport)


def _make_event(tool_name: str, tool_input: dict | None = None) -> BeforeToolCallEvent:
    return BeforeToolCallEvent(
        agent=None,
        selected_tool=None,
        tool_use={"name": tool_name, "input": tool_input or {}, "toolUseId": "test-id"},
        invocation_state={},
    )


def _auto_handler(request):
    """Every question gets a confidently-AUTO answer: noul -> False, choice -> its first criterion."""
    body = json.loads(request.content)
    answers = {}
    for name, q in body["questions"].items():
        if q["type"] == "noul":
            answers[name] = {"type": "noul", "noul": 0.1}
        else:
            first_key = next(iter(q["criteria"]))
            answers[name] = {"type": "choice", "choice": first_key, "confidence": 0.95, "probabilities": {first_key: 0.95}}
    return httpx.Response(200, json={"model": "m", "answers": answers, "usage": {}})


def _escalate_handler(request):
    """Every question gets a confidently-ambiguous answer, forcing ESCALATE."""
    body = json.loads(request.content)
    answers = {}
    for name, q in body["questions"].items():
        if q["type"] == "noul":
            answers[name] = {"type": "noul", "noul": 0.55}
        else:
            first_key = next(iter(q["criteria"]))
            answers[name] = {"type": "choice", "choice": first_key, "confidence": 0.5, "probabilities": {first_key: 0.5}}
    return httpx.Response(200, json={"model": "m", "answers": answers, "usage": {}})


# -- COVERAGE checkpoint resolve() -------------------------------------------


def test_coverage_resolve_denies_accept_at_4_of_5(tmp_path):
    checkpoint, controller, holder = _setup_coverage_checkpoint(tmp_path, ["S01", "S02", "S03", "S05"])
    verdicts = {"coverage_action": _choice_verdict("coverage_action", "ACCEPT_COVERAGE")}

    action = checkpoint.resolve(verdicts)

    assert isinstance(action, Deny)


def test_coverage_resolve_first_remediate_sets_holder_flag_and_denies(tmp_path):
    checkpoint, controller, holder = _setup_coverage_checkpoint(tmp_path, ["S01", "S02", "S03", "S05"])
    verdicts = {"coverage_action": _choice_verdict("coverage_action", "REMEDIATE_GAP")}

    action = checkpoint.resolve(verdicts)

    assert isinstance(action, Deny)
    assert holder["remediation_feedback"] == (
        "S04 / BR06 missing: add a duplicate-callback scenario asserting "
        "exactly one ledger posting; keep S01-S03, S05 unchanged"
    )
    assert controller.remediation_used is True
    assert controller.halted is True  # denies any further tool call this turn, before _drive_recheck


def test_coverage_resolve_second_remediate_denies_and_blocks(tmp_path):
    checkpoint, controller, holder = _setup_coverage_checkpoint(tmp_path, ["S01", "S02", "S03", "S05"])
    verdicts = {"coverage_action": _choice_verdict("coverage_action", "REMEDIATE_GAP")}
    checkpoint.resolve(verdicts)  # first call: succeeds, sets remediation_used

    checkpoint.build_questions(None)
    action = checkpoint.resolve(verdicts)

    assert isinstance(action, Deny)
    assert controller.workflow_state == "BLOCKED"
    assert controller.halted is True
    assert holder["remediation_feedback"] is None  # reset at top of resolve(), never left stale


def test_coverage_resolve_accepts_at_5_of_5(tmp_path):
    checkpoint, controller, holder = _setup_coverage_checkpoint(tmp_path, ["S01", "S02", "S03", "S04", "S05"])
    verdicts = {"coverage_action": _choice_verdict("coverage_action", "ACCEPT_COVERAGE")}

    action = checkpoint.resolve(verdicts)

    assert isinstance(action, Proceed)
    assert (controller.run_dir / "scenarios_accepted.json").exists()


# -- grounding nouls are self-contained; shared state is validator-matrix-only -


def test_grounding_question_embeds_its_own_gherkin_and_oracle(tmp_path):
    checkpoint, controller, holder = _setup_coverage_checkpoint(tmp_path, ["S01", "S02", "S03", "S05"])

    questions = checkpoint.build_questions(None)
    state = checkpoint.build_state(None)

    instructions = questions["grounding_S01"]["instructions"]
    assert _valid_scenario("S01")["gherkin"] in instructions
    assert "obs EQ 1" in instructions
    assert "meaning of BR01" in instructions
    # The shared state no longer duplicates any scenario's Gherkin/oracle --
    # only the validator matrix (status per scenario, covered/required, missing).
    assert "scenarios" not in state
    assert state["validator_matrix"]["rows"] == [
        {"scenario_id": "S01", "status": "VALID"},
        {"scenario_id": "S02", "status": "VALID"},
        {"scenario_id": "S03", "status": "VALID"},
        {"scenario_id": "S04", "status": "MISSING"},
        {"scenario_id": "S05", "status": "VALID"},
    ]


# -- grounding never promotes an invalid row ---------------------------------


def test_grounding_never_promotes_invalid_row():
    coverage = {
        "scenario_set_version": 1,
        "covered": 0,
        "required": 1,
        "rows": [{"scenario_id": "S01", "status": "INVALID", "rule_ids": ["BR01"], "findings": ["bad"]}],
    }
    verdicts = {"grounding_S01": _noul_verdict("grounding_S01", True)}

    items = grounding_items(coverage, verdicts)

    assert items[0]["verdict"] == "GROUNDED"  # grounding annotates the row...
    assert full_coverage(coverage) is False  # ...but never feeds into the coverage guard itself


# -- HaltGuard: deny every tool call while halted, never reaching Decider ----


def test_halt_guard_proceeds_when_not_halted(tmp_path):
    controller = RunController("test-run", tmp_path)
    guard = HaltGuard(controller)

    action = guard.before_tool_call(_make_event("save_scenario_set", {}))

    assert isinstance(action, Proceed)


def test_halt_guard_denies_with_halt_reason_once_halted(tmp_path):
    controller = RunController("test-run", tmp_path)
    controller.set_halt("REMEDIATION_SCHEDULED: do not call further tools")

    action = HaltGuard(controller).before_tool_call(_make_event("save_scenario_set", {}))

    assert isinstance(action, Deny)
    assert action.reason == "REMEDIATION_SCHEDULED: do not call further tools"


def test_halt_guard_clears_and_proceeds_again(tmp_path):
    controller = RunController("test-run", tmp_path)
    controller.set_halt("blocked for now")
    controller.clear_halt()

    action = HaltGuard(controller).before_tool_call(_make_event("save_scenario_set", {}))

    assert isinstance(action, Proceed)


# -- StageGuard ---------------------------------------------------------------


def test_stage_guard_proceeds_when_no_stage_set(tmp_path):
    controller = RunController("test-run", tmp_path)

    action = StageGuard(controller).before_tool_call(_make_event("generate_scenarios", {}))

    assert isinstance(action, Proceed)


def test_stage_guard_proceeds_for_a_tool_allowed_at_the_current_stage(tmp_path):
    controller = RunController("test-run", tmp_path)
    controller.set_stage("GENERATING", {"generate_scenarios"})

    action = StageGuard(controller).before_tool_call(_make_event("generate_scenarios", {}))

    assert isinstance(action, Proceed)


def test_stage_guard_denies_without_decider_and_records_stage_violation_not_a_decision(tmp_path):
    controller = RunController("test-run", tmp_path)
    controller.set_stage("INTERPRETING", {"interpret_requirement"})

    action = StageGuard(controller).before_tool_call(_make_event("generate_scenarios", {}))

    assert isinstance(action, Deny)
    assert "generate_scenarios" in action.reason
    assert "INTERPRETING" in action.reason
    assert controller.stage_violations == [
        {
            "stage": "INTERPRETING",
            "tool_name": "generate_scenarios",
            "reason": action.reason,
        }
    ]
    assert controller.decisions == []  # never reached Decider -- no decision record
    assert json.loads((tmp_path / "stage_violations.json").read_text()) == controller.stage_violations


# -- generate_scenarios checkpoint resolve() ----------------------------------


def test_generate_scenarios_resolve_sets_awaiting_clarification_on_flagged_ambiguity(tmp_path):
    attempt_log = AttemptLog()
    attempt_log.record_success("interpret_requirement", 1, ONE_RULE_INTERPRETATION)
    controller = RunController("test-run", tmp_path)
    checkpoint = build_generate_scenarios_checkpoint(attempt_log, controller)
    verdicts = {
        "ambiguity_BR01": _noul_verdict("ambiguity_BR01", True),
        "strategy_action": _choice_verdict("strategy_action", "GENERATE_TESTS"),
    }

    action = checkpoint.resolve(verdicts)

    assert isinstance(action, Deny)
    assert controller.workflow_state == "AWAITING_CLARIFICATION"
    assert controller.halted is True


def test_generate_scenarios_resolve_proceeds_when_clear(tmp_path):
    attempt_log = AttemptLog()
    attempt_log.record_success("interpret_requirement", 1, ONE_RULE_INTERPRETATION)
    controller = RunController("test-run", tmp_path)
    checkpoint = build_generate_scenarios_checkpoint(attempt_log, controller)
    verdicts = {
        "ambiguity_BR01": _noul_verdict("ambiguity_BR01", False),
        "strategy_action": _choice_verdict("strategy_action", "GENERATE_TESTS"),
    }

    action = checkpoint.resolve(verdicts)

    assert isinstance(action, Proceed)
    assert controller.workflow_state == "PREPARING"
    assert controller.halted is False


# -- escalation / on_decision / on_error, via the real DeciderGuardrail ------


def test_escalate_sets_pending_decision_and_awaiting_stage(tmp_path):
    attempt_log = AttemptLog()
    attempt_log.record_success("interpret_requirement", 1, ONE_RULE_INTERPRETATION)
    controller = RunController("test-run", tmp_path)
    holder: dict = {}
    _, gate, checkpoints, on_decision, on_error, _host_label = build_all(attempt_log, CATALOG, controller, DECIDER_CFG, holder)
    client = make_client(_escalate_handler)

    from decider_gate.intervention import DeciderGuardrail

    guardrail = DeciderGuardrail(
        client=client, gate=gate, checkpoints=checkpoints, host_label="local-cpu",
        on_decision=on_decision, on_error=on_error,
    )
    action = guardrail.before_tool_call(_make_event("generate_scenarios", {}))

    assert isinstance(action, Confirm)
    assert controller.pending_decision is not None
    assert controller.pending_decision["tool_name"] == "generate_scenarios"
    assert controller.stage == "AWAITING_DECISION_CONFIRMATION"
    # on_decision keeps every question's full verdict, not just its suggestion,
    # so a later human pick can produce a complete per_question audit record.
    stored = controller.pending_decision["verdicts"]
    for fields in stored.values():
        assert set(fields) == {"answer", "suggestion", "confidence", "probabilities", "route"}
    assert controller.decisions[-1]["per_question"]
    assert {q["question"] for q in controller.decisions[-1]["per_question"]} == set(stored)


def test_decisions_json_records_every_call(tmp_path):
    attempt_log = AttemptLog()
    attempt_log.record_success("interpret_requirement", 1, ONE_RULE_INTERPRETATION)
    controller = RunController("test-run", tmp_path)
    holder: dict = {}
    _, gate, checkpoints, on_decision, on_error, _host_label = build_all(attempt_log, CATALOG, controller, DECIDER_CFG, holder)

    from decider_gate.intervention import DeciderGuardrail

    auto_guardrail = DeciderGuardrail(
        client=make_client(_auto_handler), gate=gate, checkpoints=checkpoints, host_label="local-cpu",
        on_decision=on_decision, on_error=on_error,
    )
    auto_guardrail.before_tool_call(_make_event("generate_scenarios", {}))
    assert len(controller.decisions) == 1

    escalate_guardrail = DeciderGuardrail(
        client=make_client(_escalate_handler), gate=gate, checkpoints=checkpoints, host_label="local-cpu",
        on_decision=on_decision, on_error=on_error,
    )
    escalate_guardrail.before_tool_call(_make_event("generate_scenarios", {}))
    assert len(controller.decisions) == 2
    assert (controller.run_dir / "decisions.json").exists()


def test_decider_error_sets_stopped(tmp_path):
    attempt_log = AttemptLog()
    attempt_log.record_success("interpret_requirement", 1, ONE_RULE_INTERPRETATION)
    controller = RunController("test-run", tmp_path)
    holder: dict = {}
    _, gate, checkpoints, on_decision, on_error, _host_label = build_all(attempt_log, CATALOG, controller, DECIDER_CFG, holder)

    def handler(request):
        raise httpx.ConnectError("refused")

    from decider_gate.intervention import DeciderGuardrail

    guardrail = DeciderGuardrail(
        client=make_client(handler), gate=gate, checkpoints=checkpoints, host_label="local-cpu",
        on_decision=on_decision, on_error=on_error,
    )
    action = guardrail.before_tool_call(_make_event("generate_scenarios", {}))

    assert isinstance(action, Deny)
    assert controller.workflow_state == "STOPPED"
    assert (controller.run_dir / "decider_error.json").exists()


# -- human escalation path: apply_human_decision ------------------------------


def test_human_rejected_pick_keeps_decision_open(tmp_path):
    checkpoint, controller, holder = _setup_coverage_checkpoint(tmp_path, ["S01", "S02", "S03", "S05"])  # 4/5
    pending = {
        "tool_name": "save_scenario_set",
        "context": {"coverage": holder["coverage"], "scenario_set": holder["scenario_set"]},
        "verdicts": {"coverage_action": _pending_verdict_fields("ACCEPT_COVERAGE")},
    }
    controller.set_pending_decision(pending)

    outcome, feedback = apply_human_decision(pending, "ACCEPT_COVERAGE", controller)

    assert outcome == "REJECTED_RETRY"
    assert feedback is None
    assert controller.decisions[-1]["decision_status"] == "REJECTED"
    assert controller.decisions[-1]["per_question"] == [
        {"question": "coverage_action", **_pending_verdict_fields("ACCEPT_COVERAGE")}
    ]
    # apply_human_decision never touches pending_decision itself -- that is
    # orchestration.resume_pending_decision's job, specifically so a rejected
    # pick never strands the run.
    assert controller.pending_decision is pending

    # A second, valid pick on the SAME still-open pending decision succeeds.
    outcome2, feedback2 = apply_human_decision(pending, "REMEDIATE_GAP", controller)
    assert outcome2 == "REMEDIATION_SCHEDULED"
    assert "S04" in feedback2
    assert controller.remediation_used is True
    assert controller.halted is True
    assert (controller.run_dir / "coverage_v1.json").exists()  # parity with the AUTO resolve() path


def test_human_generate_tests_pick_proceeds(tmp_path):
    attempt_log = AttemptLog()
    attempt_log.record_success("interpret_requirement", 1, ONE_RULE_INTERPRETATION)
    controller = RunController("test-run", tmp_path)
    pending = {
        "tool_name": "generate_scenarios",
        "context": {},
        "verdicts": {"strategy_action": _pending_verdict_fields("GENERATE_TESTS")},
    }
    controller.set_pending_decision(pending)

    outcome, feedback = apply_human_decision(pending, "GENERATE_TESTS", controller)

    assert outcome == "PROCEED"
    assert controller.halted is False


def test_human_request_clarification_pick_awaits_clarification(tmp_path):
    controller = RunController("test-run", tmp_path)
    pending = {
        "tool_name": "generate_scenarios",
        "context": {},
        "verdicts": {"strategy_action": _pending_verdict_fields("REQUEST_CLARIFICATION")},
    }
    controller.set_pending_decision(pending)

    outcome, feedback = apply_human_decision(pending, "REQUEST_CLARIFICATION", controller)

    assert outcome == "AWAITING_CLARIFICATION"
    assert controller.halted is True
    assert controller.workflow_state == "AWAITING_CLARIFICATION"


# -- STRATEGY state includes interpretation's ambiguities --------------------


def test_generate_scenarios_build_state_includes_ambiguities_summary(tmp_path):
    attempt_log = AttemptLog()
    attempt_log.record_success(
        "interpret_requirement",
        1,
        {
            "rules": ONE_RULE_INTERPRETATION["rules"],
            "ambiguities": [
                {"id": "A1", "rule_ids": ["BR01"], "question": "which gateway?", "critical": True, "status": "OPEN"}
            ],
        },
    )
    controller = RunController("test-run", tmp_path)
    checkpoint = build_generate_scenarios_checkpoint(attempt_log, controller)

    state = checkpoint.build_state(None)

    assert state["ambiguities"][0]["question"] == "which gateway?"


def test_generate_scenarios_build_state_says_none_identified_when_no_ambiguities(tmp_path):
    attempt_log = AttemptLog()
    attempt_log.record_success("interpret_requirement", 1, ONE_RULE_INTERPRETATION)
    controller = RunController("test-run", tmp_path)
    checkpoint = build_generate_scenarios_checkpoint(attempt_log, controller)

    state = checkpoint.build_state(None)

    assert state["ambiguities"] == "none identified"


def test_generate_scenarios_strategy_question_references_ambiguity_evidence(tmp_path):
    attempt_log = AttemptLog()
    attempt_log.record_success(
        "interpret_requirement",
        1,
        {
            "rules": ONE_RULE_INTERPRETATION["rules"],
            "ambiguities": [
                {"id": "A1", "rule_ids": ["BR01"], "question": "which gateway?", "critical": True, "status": "OPEN"}
            ],
        },
    )
    controller = RunController("test-run", tmp_path)
    checkpoint = build_generate_scenarios_checkpoint(attempt_log, controller)

    questions = checkpoint.build_questions(None)

    assert "which gateway?" in questions["strategy_action"]["instructions"]


# -- host label resolution -----------------------------------------------------


def test_resolve_host_label_env_override(monkeypatch):
    monkeypatch.setenv("AITE_DECIDER_HOST_LABEL", "ec2-g4dn-t4")

    assert resolve_host_label({"host_label": "local-cpu"}) == "ec2-g4dn-t4"


def test_resolve_host_label_falls_back_to_config_when_env_unset(monkeypatch):
    monkeypatch.delenv("AITE_DECIDER_HOST_LABEL", raising=False)

    assert resolve_host_label({"host_label": "local-cpu"}) == "local-cpu"


def test_resolve_host_label_falls_back_to_config_when_env_empty(monkeypatch):
    monkeypatch.setenv("AITE_DECIDER_HOST_LABEL", "")

    assert resolve_host_label({"host_label": "local-cpu"}) == "local-cpu"


def test_build_all_uses_resolved_host_label_in_decisions(tmp_path, monkeypatch):
    monkeypatch.setenv("AITE_DECIDER_HOST_LABEL", "ec2-g4dn-t4")
    attempt_log = AttemptLog()
    attempt_log.record_success("interpret_requirement", 1, ONE_RULE_INTERPRETATION)
    controller = RunController("test-run", tmp_path)
    holder: dict = {}

    _, gate, checkpoints, on_decision, on_error, host_label = build_all(
        attempt_log, CATALOG, controller, DECIDER_CFG, holder
    )

    from decider_gate.intervention import DeciderGuardrail

    assert host_label == "ec2-g4dn-t4"
    guardrail = DeciderGuardrail(
        client=make_client(_auto_handler), gate=gate, checkpoints=checkpoints, host_label=host_label,
        on_decision=on_decision, on_error=on_error,
    )
    guardrail.before_tool_call(_make_event("generate_scenarios", {}))

    assert controller.decisions[-1]["host_label"] == "ec2-g4dn-t4"
