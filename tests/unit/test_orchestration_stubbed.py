"""Stubbed-model, stubbed-Decider regression coverage for orchestration.py's
agent-driving logic (run_initial_pass / resume_pending_decision / _drive_recheck).

No Bedrock, no GPU, no network: the Strands Agent is given a ScriptedModel
test double (implements strands.models.model.Model) instead of
get_bedrock_model(), and DeciderClient is given an httpx.MockTransport
instead of a real connection -- same MockTransport pattern as
tests/unit/test_decider_gate.py.

test_run_initial_pass_denies_premature_gated_tool_call_and_still_reaches_five_of_five
reproduces the live-run crash's trigger -- a model that calls a gated tool
(generate_scenarios) in the SAME turn as the supposedly-ungated
interpret_requirement call -- but now that orchestration.STAGE_TOOLS +
checkpoints.StageGuard are wired in, that premature call is denied before
Decider is ever consulted (no escalation, no interrupt, no decision record;
just a stage_violation evidence entry), and the run continues normally
through the rest of the documented flow to 5/5. Stopping cleanly on an
*unexpected* interrupt (orchestration._unexpected_interrupt_stop) remains a
safety net for anything stage gating doesn't catch -- see
test_orchestration.py's direct test of that helper -- but this specific,
previously-crashing scenario should never reach it anymore.

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
# Regression: a model that calls a gated tool (generate_scenarios) during the
# interpretation turn -- the exact trigger of the original live-run crash --
# is now denied by the per-stage tool gate before Decider is ever consulted,
# and the run continues normally to 5/5. Reuses _happy_path_decider_handler
# (defined below) since, from Decider's point of view, this is the same
# happy path -- the premature call never reaches it.
# ---------------------------------------------------------------------------


def test_run_initial_pass_denies_premature_gated_tool_call_and_still_reaches_five_of_five(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    requirement_text, catalog = _load_requirement_and_catalog()
    rules = _rules_from_requirement(requirement_text)

    decider_calls: list[dict] = []

    def _counting_handler(request: httpx.Request) -> httpx.Response:
        decider_calls.append(json.loads(request.content)["questions"])
        return _happy_path_decider_handler(request)

    scenarios_s01_s05 = [_scenario_from_catalog(catalog, sid) for sid in orchestration.INITIAL_TARGET_IDS]
    model = ScriptedModel(
        turns=[
            [
                ("interpret_requirement", {"rules": rules, "ambiguities": []}),
                (  # premature -- stage is still INTERPRETING, must be denied
                    "generate_scenarios",
                    {"target_ids": orchestration.INITIAL_TARGET_IDS, "scenarios": scenarios_s01_s05},
                ),
            ],
            "Noted the denial; interpretation stands.",
            [("generate_scenarios", {"target_ids": orchestration.INITIAL_TARGET_IDS, "scenarios": scenarios_s01_s05})],
            "Scenarios generated.",
            [("save_scenario_set", {})],
            "Acknowledged.",
            [
                (
                    "generate_missing_scenarios",
                    {"target_ids": ["S04"], "scenarios": [_scenario_from_catalog(catalog, "S04")]},
                )
            ],
            "S04 regenerated.",
            [("save_scenario_set", {})],
            "Coverage accepted.",
        ]
    )
    monkeypatch.setattr(orchestration, "get_bedrock_model", lambda: model)
    monkeypatch.setattr(
        checkpoints,
        "DeciderClient",
        _mock_decider_client_factory(httpx.MockTransport(_counting_handler)),
    )

    result = orchestration.run_initial_pass()
    run_dir = orchestration._ACTIVE_RUNS[result["run_id"]]["controller"].run_dir

    # Denied before Decider was ever asked -- a stage_violation, not a decision.
    assert json.loads((run_dir / "stage_violations.json").read_text()) == [
        {
            "stage": "INTERPRETING",
            "tool_name": "generate_scenarios",
            "reason": (
                "generate_scenarios is not available at stage INTERPRETING; "
                "call only ['interpret_requirement']"
            ),
        }
    ]
    assert (run_dir / "interpretation.json").exists()  # the run was never stopped

    # The run continues normally: coverage escalation -> human resume -> 5/5.
    assert result["stage"] == "AWAITING_DECISION_CONFIRMATION"
    final = orchestration.resume_pending_decision(result["run_id"], "REMEDIATE_GAP")
    assert final["workflow_state"] not in {"STOPPED", "BLOCKED"}
    assert final["coverage"]["covered"] == 5
    assert final["coverage"]["required"] == 5

    # Same 3 Decider calls as the pure happy path below -- the premature,
    # denied attempt never generated a 4th.
    assert len(decider_calls) == 3


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
