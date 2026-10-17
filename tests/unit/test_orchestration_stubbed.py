"""Stubbed-model, stubbed-Decider regression coverage for orchestration.py's
agent-driving logic (run_initial_pass / resume_pending_decision / _drive_recheck).

No Bedrock, no GPU, no network: the Strands Agent is given a ScriptedModel
test double (implements strands.models.model.Model) instead of
get_bedrock_model(), and DeciderClient is given an httpx.MockTransport
instead of a real connection -- same MockTransport pattern as
tests/unit/test_decider_gate.py.

test_run_initial_pass_stops_cleanly_instead_of_crashing_... reproduces the
live-run crash: a model that calls a gated tool (generate_scenarios) in the
SAME turn as the supposedly-ungated interpret_requirement call, which
escalates and leaves Strands' interrupt state activated. Before the fix,
run_initial_pass fell through to its next (string-prompt) agent() call and
strands.interrupt._InterruptState.resume() raised TypeError. This test must
fail with that TypeError on unfixed code and pass once orchestration checks
result.stop_reason after the interpretation call.

test_run_initial_pass_full_happy_path_... drives the whole documented flow
with a well-behaved model: interpretation -> generation (AUTO) -> coverage
escalation (4/5) -> human resume (REMEDIATE_GAP) -> remediation -> coverage
recheck (AUTO, 5/5) -> finish. It guards against the fix being too broad
(e.g. treating every post-resume turn as an unexpected interrupt).
"""

from __future__ import annotations

import json

import httpx
import pytest
from strands.models.model import Model

from ai_test_engineer import checkpoints, orchestration
from decider_gate.client import DeciderClient


class ScriptedModel(Model):
    """Deterministic Model test double.

    `turns` is a queue of plans, one per model call (one per stream()
    invocation) -- Strands calls the model again after every tool-use batch,
    so one orchestration agent() call can consume several queue entries.
    Each plan is either a str (final assistant text, ends the turn) or a
    list of (tool_name, tool_input) pairs the model "calls" in that turn.
    """

    def __init__(self, turns: list) -> None:
        self._turns = list(turns)
        self._next_tool_use_id = 0

    def update_config(self, **model_config) -> None:
        pass

    def get_config(self):
        return {}

    def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError

    async def stream(
        self,
        messages,
        tool_specs=None,
        system_prompt=None,
        *,
        tool_choice=None,
        system_prompt_content=None,
        invocation_state=None,
        cancel_signal=None,
        agent_metadata=None,
        **kwargs,
    ):
        if not self._turns:
            raise AssertionError("ScriptedModel ran out of scripted turns")
        plan = self._turns.pop(0)

        yield {"messageStart": {"role": "assistant"}}
        if isinstance(plan, str):
            yield {"contentBlockDelta": {"delta": {"text": plan}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        else:
            for tool_name, tool_input in plan:
                self._next_tool_use_id += 1
                tool_use_id = f"tooluse_{self._next_tool_use_id}"
                yield {
                    "contentBlockStart": {
                        "start": {"toolUse": {"toolUseId": tool_use_id, "name": tool_name}}
                    }
                }
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(tool_input)}}}}
                yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                "metrics": {"latencyMs": 1},
            }
        }


def _rules_from_requirement(requirement_text: str) -> list[dict]:
    rules = []
    for n in range(1, 7):
        rule_id = f"BR0{n}"
        for line in requirement_text.splitlines():
            if line.startswith(f"{rule_id}:"):
                rules.append(
                    {
                        "rule_id": rule_id,
                        "source_excerpt": line,
                        "meaning": line,
                        "expected_observations": ["observed"],
                    }
                )
                break
    assert len(rules) == 6, "requirement.json must define BR01..BR06"
    return rules


def _scenario_from_catalog(catalog: dict, scenario_id: str) -> dict:
    entry = catalog["scenarios"][scenario_id]
    return {
        "scenario_id": scenario_id,
        "title": entry["title"],
        "rule_ids": entry["rule_ids"],
        "test_id": entry["test_id"],
        "preconditions": entry["required_preconditions"],
        "actions": [f"perform {code}" for code in entry["required_action_codes"]],
        "action_codes": entry["required_action_codes"],
        "expected_assertions": entry["required_assertions"],
        "gherkin": f"Given {scenario_id} preconditions\nWhen the action occurs\nThen the assertions hold",
    }


def _mock_decider_client_factory(transport: httpx.MockTransport):
    def factory(url, timeout_s, connect_timeout_s):
        return DeciderClient(url=url, timeout_s=timeout_s, connect_timeout_s=connect_timeout_s, transport=transport)

    return factory


def _load_requirement_and_catalog():
    requirement = json.loads((orchestration.CONFIG_DIR / "requirement.json").read_text())
    catalog = json.loads((orchestration.CONFIG_DIR / "scenario_catalog.json").read_text())
    return requirement["text"], catalog


# ---------------------------------------------------------------------------
# Regression: model calls a gated tool during the (supposedly ungated)
# interpretation turn; that call escalates, leaving an unanswered interrupt.
# ---------------------------------------------------------------------------


def _overeager_decider_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    answers = {}
    for name in body["questions"]:
        if name == "strategy_action":
            answers[name] = {
                "type": "choice",
                "choice": "GENERATE_TESTS",
                "probabilities": {"GENERATE_TESTS": 0.5, "REQUEST_CLARIFICATION": 0.5},
                "confidence": 0.5,  # below choice_threshold=0.75 -> ESCALATE
            }
        elif name.startswith("ambiguity_"):
            answers[name] = {"type": "noul", "noul": 0.1}
        else:
            raise AssertionError(f"unexpected question {name!r} in overeager-model scenario")
    return httpx.Response(200, json={"model": "m", "answers": answers, "usage": {}})


def test_run_initial_pass_stops_cleanly_instead_of_crashing_on_premature_gated_tool_call(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    requirement_text, catalog = _load_requirement_and_catalog()
    rules = _rules_from_requirement(requirement_text)

    model = ScriptedModel(
        turns=[
            [
                ("interpret_requirement", {"rules": rules, "ambiguities": []}),
                (
                    "generate_scenarios",
                    {
                        "target_ids": orchestration.INITIAL_TARGET_IDS,
                        "scenarios": [
                            _scenario_from_catalog(catalog, sid) for sid in orchestration.INITIAL_TARGET_IDS
                        ],
                    },
                ),
            ],
        ]
    )
    monkeypatch.setattr(orchestration, "get_bedrock_model", lambda: model)
    monkeypatch.setattr(
        checkpoints,
        "DeciderClient",
        _mock_decider_client_factory(httpx.MockTransport(_overeager_decider_handler)),
    )

    result = orchestration.run_initial_pass()

    assert result["workflow_state"] == "STOPPED"
    assert result["stage"] == "STOPPED"
    assert result["interrupts"]
    run_dir = orchestration._ACTIVE_RUNS[result["run_id"]]["controller"].run_dir
    assert (run_dir / "unexpected_interrupt.json").exists()
    # The interpretation itself was never allowed to complete/persist -- a
    # clean stop, not a half-finished run.
    assert not (run_dir / "interpretation.json").exists()


# ---------------------------------------------------------------------------
# Full happy path with a well-behaved model: interpretation -> generation ->
# coverage escalation -> human resume -> remediation -> 5/5.
# ---------------------------------------------------------------------------


def _happy_path_decider_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    questions = body["questions"]
    answers = {}

    if "strategy_action" in questions:
        for name in questions:
            if name == "strategy_action":
                answers[name] = {
                    "type": "choice",
                    "choice": "GENERATE_TESTS",
                    "probabilities": {"GENERATE_TESTS": 0.95, "REQUEST_CLARIFICATION": 0.05},
                    "confidence": 0.95,
                }
            elif name.startswith("ambiguity_"):
                answers[name] = {"type": "noul", "noul": 0.05}
            else:
                raise AssertionError(f"unexpected question {name!r}")
        return httpx.Response(200, json={"model": "m", "answers": answers, "usage": {}})

    if "coverage_action" in questions:
        is_second_pass = "grounding_S04" in questions
        for name in questions:
            if name == "coverage_action":
                answers[name] = (
                    {
                        "type": "choice",
                        "choice": "ACCEPT_COVERAGE",
                        "probabilities": {"ACCEPT_COVERAGE": 0.95, "REMEDIATE_GAP": 0.05},
                        "confidence": 0.95,
                    }
                    if is_second_pass
                    else {
                        "type": "choice",
                        "choice": "REMEDIATE_GAP",
                        "probabilities": {"ACCEPT_COVERAGE": 0.45, "REMEDIATE_GAP": 0.55},
                        "confidence": 0.55,  # below threshold -> ESCALATE
                    }
                )
            elif name.startswith("grounding_"):
                answers[name] = {"type": "noul", "noul": 0.95}
            else:
                raise AssertionError(f"unexpected question {name!r}")
        return httpx.Response(200, json={"model": "m", "answers": answers, "usage": {}})

    raise AssertionError(f"unrecognized checkpoint questions: {list(questions)!r}")


def test_run_initial_pass_full_happy_path_through_remediation_reaches_five_of_five(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    requirement_text, catalog = _load_requirement_and_catalog()
    rules = _rules_from_requirement(requirement_text)

    model = ScriptedModel(
        turns=[
            [("interpret_requirement", {"rules": rules, "ambiguities": []})],
            "Interpretation recorded.",
            [
                (
                    "generate_scenarios",
                    {
                        "target_ids": orchestration.INITIAL_TARGET_IDS,
                        "scenarios": [
                            _scenario_from_catalog(catalog, sid) for sid in orchestration.INITIAL_TARGET_IDS
                        ],
                    },
                )
            ],
            "Scenarios generated.",
            [("save_scenario_set", {})],  # escalates -> pauses, no follow-up call
            "Acknowledged.",  # resume_pending_decision's agent([...]) call
            [("generate_missing_scenarios", {"target_ids": ["S04"], "scenarios": [_scenario_from_catalog(catalog, "S04")]})],
            "S04 regenerated.",
            [("save_scenario_set", {})],
            "Coverage accepted.",
        ]
    )
    monkeypatch.setattr(orchestration, "get_bedrock_model", lambda: model)
    monkeypatch.setattr(
        checkpoints,
        "DeciderClient",
        _mock_decider_client_factory(httpx.MockTransport(_happy_path_decider_handler)),
    )

    result = orchestration.run_initial_pass()
    assert result["stage"] == "AWAITING_DECISION_CONFIRMATION"
    assert result["pending_decision"]["tool_name"] == "save_scenario_set"
    run_id = result["run_id"]

    final = orchestration.resume_pending_decision(run_id, "REMEDIATE_GAP")

    assert final["workflow_state"] not in {"STOPPED", "BLOCKED"}
    assert final["coverage"]["covered"] == 5
    assert final["coverage"]["required"] == 5
    controller = orchestration._ACTIVE_RUNS[run_id]["controller"]
    assert (controller.run_dir / "coverage_v1.json").exists()  # pre-remediation 4/5 snapshot
    # generate_scenarios AUTO, save_scenario_set ESCALATE, the human's REMEDIATE_GAP pick,
    # then save_scenario_set's AUTO re-check -- one record per checkpoint resolution.
    assert len(controller.decisions) == 4
