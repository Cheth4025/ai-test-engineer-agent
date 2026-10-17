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
    apply_human_decision,
    build_all,
    build_coverage_checkpoint,
    build_generate_scenarios_checkpoint,
    full_coverage,
    grounding_items,
    remediation_feedback,
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


def test_coverage_resolve_second_remediate_denies_and_blocks(tmp_path):
    checkpoint, controller, holder = _setup_coverage_checkpoint(tmp_path, ["S01", "S02", "S03", "S05"])
    verdicts = {"coverage_action": _choice_verdict("coverage_action", "REMEDIATE_GAP")}
    checkpoint.resolve(verdicts)  # first call: succeeds, sets remediation_used

    checkpoint.build_questions(None)
    action = checkpoint.resolve(verdicts)

    assert isinstance(action, Deny)
    assert controller.workflow_state == "BLOCKED"
    assert holder["remediation_feedback"] is None  # reset at top of resolve(), never left stale


def test_coverage_resolve_accepts_at_5_of_5(tmp_path):
    checkpoint, controller, holder = _setup_coverage_checkpoint(tmp_path, ["S01", "S02", "S03", "S04", "S05"])
    verdicts = {"coverage_action": _choice_verdict("coverage_action", "ACCEPT_COVERAGE")}

    action = checkpoint.resolve(verdicts)

    assert isinstance(action, Proceed)
    assert (controller.run_dir / "scenarios_accepted.json").exists()


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


# -- escalation / on_decision / on_error, via the real DeciderGuardrail ------


def test_escalate_sets_pending_decision_and_awaiting_stage(tmp_path):
    attempt_log = AttemptLog()
    attempt_log.record_success("interpret_requirement", 1, ONE_RULE_INTERPRETATION)
    controller = RunController("test-run", tmp_path)
    holder: dict = {}
    _, gate, checkpoints, on_decision, on_error = build_all(attempt_log, CATALOG, controller, DECIDER_CFG, holder)
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


def test_decisions_json_records_every_call(tmp_path):
    attempt_log = AttemptLog()
    attempt_log.record_success("interpret_requirement", 1, ONE_RULE_INTERPRETATION)
    controller = RunController("test-run", tmp_path)
    holder: dict = {}
    _, gate, checkpoints, on_decision, on_error = build_all(attempt_log, CATALOG, controller, DECIDER_CFG, holder)

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
    _, gate, checkpoints, on_decision, on_error = build_all(attempt_log, CATALOG, controller, DECIDER_CFG, holder)

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
        "verdicts": {"coverage_action": "ACCEPT_COVERAGE"},
    }
    controller.set_pending_decision(pending)

    outcome, feedback = apply_human_decision(pending, "ACCEPT_COVERAGE", controller)

    assert outcome == "REJECTED_RETRY"
    assert feedback is None
    assert controller.decisions[-1]["decision_status"] == "REJECTED"
    # apply_human_decision never touches pending_decision itself -- that is
    # orchestration.resume_pending_decision's job, specifically so a rejected
    # pick never strands the run.
    assert controller.pending_decision is pending

    # A second, valid pick on the SAME still-open pending decision succeeds.
    outcome2, feedback2 = apply_human_decision(pending, "REMEDIATE_GAP", controller)
    assert outcome2 == "REMEDIATION_SCHEDULED"
    assert "S04" in feedback2
    assert controller.remediation_used is True


def test_human_generate_tests_pick_proceeds(tmp_path):
    attempt_log = AttemptLog()
    attempt_log.record_success("interpret_requirement", 1, ONE_RULE_INTERPRETATION)
    controller = RunController("test-run", tmp_path)
    pending = {"tool_name": "generate_scenarios", "context": {}, "verdicts": {"strategy_action": "GENERATE_TESTS"}}
    controller.set_pending_decision(pending)

    outcome, feedback = apply_human_decision(pending, "GENERATE_TESTS", controller)

    assert outcome == "PROCEED"


def test_human_request_clarification_pick_awaits_clarification(tmp_path):
    controller = RunController("test-run", tmp_path)
    pending = {"tool_name": "generate_scenarios", "context": {}, "verdicts": {"strategy_action": "REQUEST_CLARIFICATION"}}
    controller.set_pending_decision(pending)

    outcome, feedback = apply_human_decision(pending, "REQUEST_CLARIFICATION", controller)

    assert outcome == "AWAITING_CLARIFICATION"
