"""Unit tests for decider_gate (DeciderClient, ConfidenceGate, DeciderGuardrail).

All httpx.MockTransport responses here are unit-test-only -- never used by the
app, never labelled as real Decider output.
"""

from __future__ import annotations

import json

import httpx
import pytest
from strands.hooks.events import BeforeToolCallEvent
from strands.interventions import Confirm, Deny, Guide, Proceed

from strands.interrupt import Interrupt, _InterruptState

from decider_gate.client import DeciderClient, DeciderError
from decider_gate.gate import ConfidenceGate
from decider_gate.intervention import CheckpointSpec, DeciderGuardrail


def make_client(handler, timeout_s=30, connect_timeout_s=5):
    transport = httpx.MockTransport(handler)
    return DeciderClient(
        url="http://decider.test",
        timeout_s=timeout_s,
        connect_timeout_s=connect_timeout_s,
        transport=transport,
    )


# -- DeciderClient ----------------------------------------------------------


def test_client_sends_expected_request_body_shape():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "StrandsAgents/strands-decider-2B-hobson-v19",
                "answers": {"q1": {"type": "noul", "noul": 0.9}},
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
        )

    client = make_client(handler)
    state = {"some": "state"}
    questions = {"q1": {"type": "noul", "instructions": "x", "criteria": {"true": "a", "false": "b"}}}

    result = client.ask(state=state, questions=questions)

    assert captured["url"] == "http://decider.test/v1/systemone"
    assert captured["body"] == {"state": state, "questions": questions}
    assert result["answers"]["q1"]["noul"] == 0.9
    assert result["model"] == "StrandsAgents/strands-decider-2B-hobson-v19"
    assert isinstance(result["latency_ms"], int)
    assert result["latency_ms"] >= 0


def test_client_timeout_raises_decider_error_with_no_answer():
    def handler(request):
        raise httpx.ReadTimeout("simulated timeout")

    client = make_client(handler)
    with pytest.raises(DeciderError):
        client.ask(state="s", questions={"q1": {"type": "noul"}})


def test_client_connection_error_raises_decider_error():
    def handler(request):
        raise httpx.ConnectError("simulated connection refused")

    client = make_client(handler)
    with pytest.raises(DeciderError):
        client.ask(state="s", questions={"q1": {"type": "noul"}})


def test_client_malformed_response_missing_answers_key_raises():
    def handler(request):
        return httpx.Response(200, json={"model": "m", "usage": {}})

    client = make_client(handler)
    with pytest.raises(DeciderError):
        client.ask(state="s", questions={"q1": {"type": "noul"}})


def test_client_response_missing_requested_answer_raises():
    def handler(request):
        return httpx.Response(200, json={"model": "m", "answers": {}, "usage": {}})

    client = make_client(handler)
    with pytest.raises(DeciderError):
        client.ask(state="s", questions={"q1": {"type": "noul"}})


def test_client_non_200_status_raises():
    def handler(request):
        return httpx.Response(500, text="server error")

    client = make_client(handler)
    with pytest.raises(DeciderError):
        client.ask(state="s", questions={"q1": {"type": "noul"}})


# -- ConfidenceGate -----------------------------------------------------------


def test_choice_auto_at_0_81():
    gate = ConfidenceGate(choice_threshold=0.75, noul_upper=0.80, noul_lower=0.40)
    verdict = gate.evaluate(
        "coverage_action",
        {"type": "choice", "choice": "REMEDIATE_GAP", "probabilities": {"ACCEPT_COVERAGE": 0.19, "REMEDIATE_GAP": 0.81}, "confidence": 0.81},
        latency_ms=100,
        host_label="local-cpu",
        model="m",
    )
    assert verdict.route == "AUTO"
    assert verdict.answer == "REMEDIATE_GAP"
    assert verdict.suggestion == "REMEDIATE_GAP"


def test_choice_escalate_at_0_60():
    gate = ConfidenceGate(choice_threshold=0.75)
    verdict = gate.evaluate(
        "coverage_action",
        {"type": "choice", "choice": "ACCEPT_COVERAGE", "probabilities": {"ACCEPT_COVERAGE": 0.60, "REMEDIATE_GAP": 0.40}, "confidence": 0.60},
        latency_ms=100,
        host_label="local-cpu",
        model="m",
    )
    assert verdict.route == "ESCALATE"
    assert verdict.answer is None
    assert verdict.suggestion == "ACCEPT_COVERAGE"


def test_choice_threshold_boundary_exactly_0_75_is_auto():
    gate = ConfidenceGate(choice_threshold=0.75)
    verdict = gate.evaluate(
        "coverage_action",
        {"type": "choice", "choice": "ACCEPT_COVERAGE", "probabilities": {}, "confidence": 0.75},
        latency_ms=100,
        host_label="local-cpu",
        model="m",
    )
    assert verdict.route == "AUTO"


def test_noul_0_85_is_true_auto():
    gate = ConfidenceGate(noul_upper=0.80, noul_lower=0.40)
    verdict = gate.evaluate(
        "grounding_s04", {"type": "noul", "noul": 0.85}, latency_ms=100, host_label="local-cpu", model="m"
    )
    assert verdict.route == "AUTO"
    assert verdict.answer is True


def test_noul_0_30_is_false_auto():
    gate = ConfidenceGate(noul_upper=0.80, noul_lower=0.40)
    verdict = gate.evaluate(
        "grounding_s04", {"type": "noul", "noul": 0.30}, latency_ms=100, host_label="local-cpu", model="m"
    )
    assert verdict.route == "AUTO"
    assert verdict.answer is False


def test_noul_0_55_escalates():
    gate = ConfidenceGate(noul_upper=0.80, noul_lower=0.40)
    verdict = gate.evaluate(
        "grounding_s04", {"type": "noul", "noul": 0.55}, latency_ms=100, host_label="local-cpu", model="m"
    )
    assert verdict.route == "ESCALATE"
    assert verdict.answer is None
    assert verdict.suggestion is True  # 0.55 >= 0.5


def test_unsupported_question_type_raises_value_error():
    gate = ConfidenceGate()
    with pytest.raises(ValueError):
        gate.evaluate("sev", {"type": "score", "score": 2}, latency_ms=1, host_label="h", model="m")


# -- DeciderGuardrail ---------------------------------------------------------


def _make_event(tool_name: str, tool_input: dict | None = None) -> BeforeToolCallEvent:
    return BeforeToolCallEvent(
        agent=None,
        selected_tool=None,
        tool_use={"name": tool_name, "input": tool_input or {}, "toolUseId": "test-id"},
        invocation_state={},
    )


def _noul_response_handler(noul_value: float):
    def handler(request):
        return httpx.Response(
            200,
            json={"model": "m", "answers": {"q1": {"type": "noul", "noul": noul_value}}, "usage": {}},
        )

    return handler


def test_guardrail_proceeds_on_unguarded_tool():
    client = make_client(lambda r: httpx.Response(200, json={"model": "m", "answers": {}, "usage": {}}))
    guardrail = DeciderGuardrail(
        client=client, gate=ConfidenceGate(), checkpoints={}, host_label="local-cpu"
    )
    action = guardrail.before_tool_call(_make_event("unguarded_tool"))
    assert isinstance(action, Proceed)


def test_build_state_overrides_state_sent_to_decider():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "m", "answers": {"q1": {"type": "noul", "noul": 0.9}}, "usage": {}})

    client = make_client(handler)
    spec = CheckpointSpec(
        build_questions=lambda state: {"q1": {"type": "noul", "instructions": "x"}},
        resolve=lambda verdicts: Proceed(),
        build_state=lambda state: {"richer": "state", "raw_input": state},
    )
    guardrail = DeciderGuardrail(
        client=client, gate=ConfidenceGate(), checkpoints={"guarded_tool": spec}, host_label="local-cpu"
    )
    guardrail.before_tool_call(_make_event("guarded_tool", {"some": "input"}))

    assert captured["body"]["state"] == {"richer": "state", "raw_input": {"some": "input"}}


def test_build_state_defaults_to_raw_tool_input_when_absent():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "m", "answers": {"q1": {"type": "noul", "noul": 0.9}}, "usage": {}})

    client = make_client(handler)
    spec = CheckpointSpec(
        build_questions=lambda state: {"q1": {"type": "noul", "instructions": "x"}},
        resolve=lambda verdicts: Proceed(),
    )
    guardrail = DeciderGuardrail(
        client=client, gate=ConfidenceGate(), checkpoints={"guarded_tool": spec}, host_label="local-cpu"
    )
    guardrail.before_tool_call(_make_event("guarded_tool", {"some": "input"}))

    assert captured["body"]["state"] == {"some": "input"}


def test_guardrail_auto_true_resolves_via_checkpoint_spec():
    client = make_client(_noul_response_handler(0.9))
    spec = CheckpointSpec(
        build_questions=lambda state: {"q1": {"type": "noul", "instructions": "x"}},
        resolve=lambda verdicts: Proceed() if verdicts["q1"].answer else Deny(reason="no"),
    )
    guardrail = DeciderGuardrail(
        client=client, gate=ConfidenceGate(), checkpoints={"guarded_tool": spec}, host_label="local-cpu"
    )
    action = guardrail.before_tool_call(_make_event("guarded_tool"))
    assert isinstance(action, Proceed)


def test_guardrail_auto_false_resolves_to_guide_when_spec_says_so():
    client = make_client(_noul_response_handler(0.1))
    spec = CheckpointSpec(
        build_questions=lambda state: {"q1": {"type": "noul", "instructions": "x"}},
        resolve=lambda verdicts: Guide(feedback="try again") if not verdicts["q1"].answer else Proceed(),
    )
    guardrail = DeciderGuardrail(
        client=client, gate=ConfidenceGate(), checkpoints={"guarded_tool": spec}, host_label="local-cpu"
    )
    action = guardrail.before_tool_call(_make_event("guarded_tool"))
    assert isinstance(action, Guide)
    assert action.feedback == "try again"


def test_guardrail_escalate_returns_confirm_generically():
    client = make_client(_noul_response_handler(0.55))  # between bands -> ESCALATE
    spec = CheckpointSpec(
        build_questions=lambda state: {"q1": {"type": "noul", "instructions": "x"}},
        resolve=lambda verdicts: pytest.fail("resolve must not be called on ESCALATE"),
    )
    guardrail = DeciderGuardrail(
        client=client, gate=ConfidenceGate(), checkpoints={"guarded_tool": spec}, host_label="local-cpu"
    )
    action = guardrail.before_tool_call(_make_event("guarded_tool"))
    assert isinstance(action, Confirm)


def test_guardrail_decider_error_returns_deny_never_proceed():
    def handler(request):
        raise httpx.ConnectError("refused")

    client = make_client(handler)
    spec = CheckpointSpec(
        build_questions=lambda state: {"q1": {"type": "noul", "instructions": "x"}},
        resolve=lambda verdicts: pytest.fail("resolve must not be called on error"),
    )
    guardrail = DeciderGuardrail(
        client=client, gate=ConfidenceGate(), checkpoints={"guarded_tool": spec}, host_label="local-cpu"
    )
    action = guardrail.before_tool_call(_make_event("guarded_tool"))
    assert isinstance(action, Deny)


def test_guardrail_on_decision_fires_for_auto_route():
    client = make_client(_noul_response_handler(0.9))
    spec = CheckpointSpec(
        build_questions=lambda state: {"q1": {"type": "noul", "instructions": "x"}},
        resolve=lambda verdicts: Proceed(),
    )
    calls = []
    guardrail = DeciderGuardrail(
        client=client,
        gate=ConfidenceGate(),
        checkpoints={"guarded_tool": spec},
        host_label="local-cpu",
        on_decision=lambda tool_name, verdicts, action: calls.append((tool_name, verdicts, action)),
    )
    guardrail.before_tool_call(_make_event("guarded_tool"))

    assert len(calls) == 1
    tool_name, verdicts, action = calls[0]
    assert tool_name == "guarded_tool"
    assert verdicts["q1"].route == "AUTO"
    assert isinstance(action, Proceed)


def test_guardrail_on_decision_fires_for_escalate_route():
    client = make_client(_noul_response_handler(0.55))
    spec = CheckpointSpec(
        build_questions=lambda state: {"q1": {"type": "noul", "instructions": "x"}},
        resolve=lambda verdicts: pytest.fail("resolve must not be called on ESCALATE"),
    )
    calls = []
    guardrail = DeciderGuardrail(
        client=client,
        gate=ConfidenceGate(),
        checkpoints={"guarded_tool": spec},
        host_label="local-cpu",
        on_decision=lambda tool_name, verdicts, action: calls.append((tool_name, verdicts, action)),
    )
    guardrail.before_tool_call(_make_event("guarded_tool"))

    assert len(calls) == 1
    tool_name, verdicts, action = calls[0]
    assert tool_name == "guarded_tool"
    assert verdicts["q1"].route == "ESCALATE"
    assert isinstance(action, Confirm)


def test_guardrail_on_error_fires_and_deny_still_returned():
    def handler(request):
        raise httpx.ConnectError("refused")

    client = make_client(handler)
    spec = CheckpointSpec(
        build_questions=lambda state: {"q1": {"type": "noul", "instructions": "x"}},
        resolve=lambda verdicts: pytest.fail("resolve must not be called on error"),
    )
    calls = []
    guardrail = DeciderGuardrail(
        client=client,
        gate=ConfidenceGate(),
        checkpoints={"guarded_tool": spec},
        host_label="local-cpu",
        on_error=lambda tool_name, error: calls.append((tool_name, error)),
    )
    action = guardrail.before_tool_call(_make_event("guarded_tool"))

    assert len(calls) == 1
    assert calls[0][0] == "guarded_tool"
    assert isinstance(calls[0][1], DeciderError)
    assert isinstance(action, Deny)


def test_guardrail_malformed_answer_missing_confidence_denies_and_fires_on_error():
    def handler(request):
        # Missing "confidence" -- gate.evaluate will KeyError on this.
        return httpx.Response(
            200,
            json={"model": "m", "answers": {"q1": {"type": "choice", "choice": "X", "probabilities": {}}}, "usage": {}},
        )

    client = make_client(handler)
    spec = CheckpointSpec(
        build_questions=lambda state: {"q1": {"type": "choice", "instructions": "x", "criteria": {"X": "x"}}},
        resolve=lambda verdicts: pytest.fail("resolve must not be called on a malformed answer"),
    )
    calls = []
    guardrail = DeciderGuardrail(
        client=client,
        gate=ConfidenceGate(),
        checkpoints={"guarded_tool": spec},
        host_label="local-cpu",
        on_error=lambda tool_name, error: calls.append((tool_name, error)),
    )

    action = guardrail.before_tool_call(_make_event("guarded_tool"))

    assert isinstance(action, Deny)
    assert len(calls) == 1
    assert calls[0][0] == "guarded_tool"
    assert isinstance(calls[0][1], KeyError)


class _FakeAgent:
    def __init__(self) -> None:
        self._interrupt_state = _InterruptState()


def test_guardrail_replay_after_resume_skips_decider_and_decision_record():
    """Strands replays before_tool_call for the same toolUseId once a paused
    run resumes (see decider_gate.intervention's module docstring). The
    guardrail must resolve straight from the already-answered interrupt
    instead of asking Decider again or firing on_decision a second time."""
    call_count = {"n": 0}

    def handler(request):
        call_count["n"] += 1
        return httpx.Response(
            200, json={"model": "m", "answers": {"q1": {"type": "noul", "noul": 0.55}}, "usage": {}}
        )

    client = make_client(handler)
    spec = CheckpointSpec(
        build_questions=lambda state: {"q1": {"type": "noul", "instructions": "x"}},
        resolve=lambda verdicts: pytest.fail("resolve must not be called on ESCALATE"),
    )
    calls = []
    guardrail = DeciderGuardrail(
        client=client,
        gate=ConfidenceGate(),
        checkpoints={"guarded_tool": spec},
        host_label="local-cpu",
        on_decision=lambda tool_name, verdicts, action: calls.append((tool_name, verdicts, action)),
    )
    agent = _FakeAgent()
    tool_use = {"name": "guarded_tool", "input": {}, "toolUseId": "tool-use-42"}
    first_event = BeforeToolCallEvent(agent=agent, selected_tool=None, tool_use=tool_use, invocation_state={})

    first_action = guardrail.before_tool_call(first_event)

    assert isinstance(first_action, Confirm)
    assert call_count["n"] == 1
    assert len(calls) == 1

    # Simulate the human resuming "yes" for this exact (toolUseId, handler name) interrupt.
    interrupt_id = first_event._interrupt_id(guardrail.name)
    agent._interrupt_state.interrupts[interrupt_id] = Interrupt(id=interrupt_id, name=guardrail.name, response="yes")

    # Strands replays the same tool_use as a fresh BeforeToolCallEvent after resume.
    replay_event = BeforeToolCallEvent(agent=agent, selected_tool=None, tool_use=dict(tool_use), invocation_state={})
    second_action = guardrail.before_tool_call(replay_event)

    assert isinstance(second_action, Proceed)
    assert call_count["n"] == 1  # Decider never called a second time
    assert len(calls) == 1  # no new decision record


def test_guardrail_replay_after_resume_denies_on_rejected_response():
    def handler(request):
        return httpx.Response(
            200, json={"model": "m", "answers": {"q1": {"type": "noul", "noul": 0.55}}, "usage": {}}
        )

    client = make_client(handler)
    spec = CheckpointSpec(
        build_questions=lambda state: {"q1": {"type": "noul", "instructions": "x"}},
        resolve=lambda verdicts: pytest.fail("resolve must not be called on ESCALATE"),
    )
    guardrail = DeciderGuardrail(
        client=client, gate=ConfidenceGate(), checkpoints={"guarded_tool": spec}, host_label="local-cpu"
    )
    agent = _FakeAgent()
    tool_use = {"name": "guarded_tool", "input": {}, "toolUseId": "tool-use-77"}
    first_event = BeforeToolCallEvent(agent=agent, selected_tool=None, tool_use=tool_use, invocation_state={})
    guardrail.before_tool_call(first_event)

    interrupt_id = first_event._interrupt_id(guardrail.name)
    agent._interrupt_state.interrupts[interrupt_id] = Interrupt(id=interrupt_id, name=guardrail.name, response="no")
    replay_event = BeforeToolCallEvent(agent=agent, selected_tool=None, tool_use=dict(tool_use), invocation_state={})

    action = guardrail.before_tool_call(replay_event)

    assert isinstance(action, Deny)


def test_guardrail_deny_on_error_even_without_on_error_callback():
    def handler(request):
        raise httpx.ConnectError("refused")

    client = make_client(handler)
    spec = CheckpointSpec(
        build_questions=lambda state: {"q1": {"type": "noul", "instructions": "x"}},
        resolve=lambda verdicts: pytest.fail("resolve must not be called on error"),
    )
    guardrail = DeciderGuardrail(
        client=client, gate=ConfidenceGate(), checkpoints={"guarded_tool": spec}, host_label="local-cpu"
    )
    action = guardrail.before_tool_call(_make_event("guarded_tool"))
    assert isinstance(action, Deny)
