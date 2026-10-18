"""Stubbed-model, stubbed-Decider regression coverage for orchestration.py's
EXECUTING -> ANALYZING_RESULTS -> REPORTING chain (execute_approved_run /
_continue_to_analysis / _continue_to_reporting). Real pytest subprocess runs
against tests/payment (fast, local, defect-on by default) -- no Bedrock, no
Decider server, no live run.

Same ScriptedModel/DeciderClient-MockTransport harness as
tests/unit/test_orchestration_stubbed.py (duplicated here rather than
imported -- tests/ has no __init__.py, so it isn't an importable package).
"""

from __future__ import annotations

import json

import httpx
import pytest
from decider_gate.client import DeciderClient
from decider_gate.intervention import DeciderGuardrail
from strands import Agent
from strands.models.model import Model

from ai_test_engineer import checkpoints, orchestration
from ai_test_engineer.checkpoints import HaltGuard, StageGuard
from ai_test_engineer.state import RunController
from ai_test_engineer.tools import AttemptLog


class ScriptedModel(Model):
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
                yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": tool_use_id, "name": tool_name}}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(tool_input)}}}}
                yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        yield {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}, "metrics": {"latencyMs": 1}}}


def _mock_decider_client_factory(transport: httpx.MockTransport):
    def factory(url, timeout_s, connect_timeout_s):
        return DeciderClient(url=url, timeout_s=timeout_s, connect_timeout_s=connect_timeout_s, transport=transport)

    return factory


def _load_requirement_text() -> str:
    return json.loads((orchestration.CONFIG_DIR / "requirement.json").read_text())["text"]


def _build_approved_run(tmp_path, model: Model) -> str:
    """A run already APPROVED, with a live Agent/guardrail wired exactly like
    run_initial_pass's, but skipping straight past intake/generation/coverage
    -- those are covered by test_orchestration_stubbed.py."""
    requirement_text = _load_requirement_text()
    catalog = json.loads((orchestration.CONFIG_DIR / "scenario_catalog.json").read_text())
    decider_cfg = json.loads((orchestration.CONFIG_DIR / "decider.json").read_text())
    run_id = "exec-run-1"
    controller = RunController(run_id, tmp_path / run_id)
    controller.set_workflow_state("APPROVED", stage="APPROVED", busy=False)
    attempt_log = AttemptLog()
    holder: dict = {}

    client, gate, checkpoints_dict, on_decision, on_error, host_label = checkpoints.build_all(
        attempt_log, catalog, controller, decider_cfg, holder, requirement_text
    )

    def on_reuse(tool_name, verdicts, action, origin_tool_use_id):
        controller.record_decision(
            checkpoints.decision_record_reused(tool_name, verdicts, action, origin_tool_use_id, controller, decider_cfg, host_label)
        )

    def on_call(tool_name, tool_use_id, action):
        controller.record_tool_call(tool_name, tool_use_id, controller.tool_stage, "decider-gate", type(action).__name__)

    guardrail = DeciderGuardrail(
        client=client, gate=gate, checkpoints=checkpoints_dict, host_label=host_label,
        on_decision=on_decision, on_error=on_error, on_reuse=on_reuse, on_call=on_call,
    )
    agent = Agent(
        model=model,
        tools=[],
        interventions=[StageGuard(controller), HaltGuard(controller), guardrail],
        callback_handler=None,
    )
    orchestration._ACTIVE_RUNS[run_id] = {
        "agent": agent, "controller": controller, "attempt_log": attempt_log,
        "catalog": catalog, "holder": holder, "last_result": None,
    }
    return run_id


@pytest.fixture(autouse=True)
def _clean_active_runs():
    orchestration._ACTIVE_RUNS.clear()
    yield
    orchestration._ACTIVE_RUNS.clear()


# ---------------------------------------------------------------------------
# Guards -- checked before anything touches the agent.
# ---------------------------------------------------------------------------


def test_execute_approved_run_requires_approved_state(tmp_path):
    controller = RunController("r1", tmp_path / "r1")  # defaults to PREPARING
    orchestration._ACTIVE_RUNS["r1"] = {"controller": controller}

    with pytest.raises(RuntimeError, match="APPROVED"):
        orchestration.execute_approved_run("r1")


def test_execute_approved_run_refuses_second_execution(tmp_path):
    controller = RunController("r1", tmp_path / "r1")
    controller.set_workflow_state("APPROVED", stage="APPROVED", busy=False)
    assert controller.use_execution() is True  # already reserved by an earlier call
    orchestration._ACTIVE_RUNS["r1"] = {"controller": controller}

    with pytest.raises(RuntimeError, match="already started"):
        orchestration.execute_approved_run("r1")


# ---------------------------------------------------------------------------
# Happy path: defect-on batch (real T04 FAIL) -> FAILURE checkpoint AUTO ->
# COMPLETION checkpoint AUTO -> COMPLETED, report.md written.
# ---------------------------------------------------------------------------


def _auto_execution_decider_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    questions = body["questions"]
    if "failure_class" in questions:
        answers = {
            "failure_class": {
                "type": "choice", "choice": "BUSINESS_RULE_VIOLATION",
                "probabilities": {"BUSINESS_RULE_VIOLATION": 0.95, "INFRASTRUCTURE_ERROR": 0.03, "INSUFFICIENT_EVIDENCE": 0.02},
                "confidence": 0.95,
            },
            "failure_action": {
                "type": "choice", "choice": "REPORT_FAILURE",
                "probabilities": {"REPORT_FAILURE": 0.9, "INVESTIGATE_FAILURE": 0.1},
                "confidence": 0.9,
            },
        }
        return httpx.Response(200, json={"model": "m", "answers": answers, "usage": {}})
    if "completion_action" in questions:
        answers = {
            "completion_action": {
                "type": "choice", "choice": "FINALIZE_RESULTS",
                "probabilities": {"FINALIZE_RESULTS": 0.95, "NOT_READY": 0.05},
                "confidence": 0.95,
            }
        }
        return httpx.Response(200, json={"model": "m", "answers": answers, "usage": {}})
    raise AssertionError(f"unrecognized questions: {list(questions)!r}")


def test_execute_approved_run_defect_on_happy_path_reaches_completed(monkeypatch, tmp_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.delenv("AITE_DUPLICATE_LEDGER_DEFECT", raising=False)  # default true
    model = ScriptedModel(
        turns=[
            [("execute_tests", {})],
            "Tests executed.",
            [("finalize_analysis", {})],
            "Analysis recorded.",
            [("render_report", {})],
            "Report finalized.",
        ]
    )
    monkeypatch.setattr(checkpoints, "DeciderClient", _mock_decider_client_factory(httpx.MockTransport(_auto_execution_decider_handler)))
    run_id = _build_approved_run(tmp_path, model)

    result = orchestration.execute_approved_run(run_id)

    assert result["workflow_state"] == "COMPLETED"
    controller = orchestration._ACTIVE_RUNS[run_id]["controller"]
    assert controller.execution_started is True
    results = json.loads((controller.run_dir / "results.json").read_text())
    t04 = next(t for t in results["tests"] if t["test_id"] == "T04")
    assert t04["status"] == "FAIL"
    analysis = json.loads((controller.run_dir / "analysis.json").read_text())
    assert analysis["classification"] == "BUSINESS_RULE_VIOLATION"
    assert analysis["handling_action"] == "REPORT_FAILURE"
    assert (controller.run_dir / "report.md").exists()
    assert "Payment verification:** FAILED" in (controller.run_dir / "report.md").read_text(encoding="utf-8")


def test_execute_approved_run_defect_off_no_failure_skips_failure_checkpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("AITE_DUPLICATE_LEDGER_DEFECT", "false")
    model = ScriptedModel(
        turns=[
            [("execute_tests", {})],
            "Tests executed.",
            [("render_report", {})],  # no finalize_analysis turn needed
            "Report finalized.",
        ]
    )
    monkeypatch.setattr(checkpoints, "DeciderClient", _mock_decider_client_factory(httpx.MockTransport(_auto_execution_decider_handler)))
    run_id = _build_approved_run(tmp_path, model)

    result = orchestration.execute_approved_run(run_id)

    assert result["workflow_state"] == "COMPLETED"
    controller = orchestration._ACTIVE_RUNS[run_id]["controller"]
    analysis = json.loads((controller.run_dir / "analysis.json").read_text())
    assert analysis["has_failure"] is False
    assert controller.investigation_used is False


# ---------------------------------------------------------------------------
# FAILURE checkpoint escalates; human picks REPORT_FAILURE; chain continues
# through COMPLETION to COMPLETED, same radio/form pattern as every other
# escalation.
# ---------------------------------------------------------------------------


def _escalating_failure_decider_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    questions = body["questions"]
    if "failure_class" in questions:
        answers = {
            "failure_class": {
                "type": "choice", "choice": "BUSINESS_RULE_VIOLATION",
                "probabilities": {"BUSINESS_RULE_VIOLATION": 0.55, "INFRASTRUCTURE_ERROR": 0.25, "INSUFFICIENT_EVIDENCE": 0.2},
                "confidence": 0.55,  # below threshold -> ESCALATE
            },
            "failure_action": {
                "type": "choice", "choice": "REPORT_FAILURE",
                "probabilities": {"REPORT_FAILURE": 0.5, "INVESTIGATE_FAILURE": 0.5},
                "confidence": 0.5,  # below threshold -> ESCALATE
            },
        }
        return httpx.Response(200, json={"model": "m", "answers": answers, "usage": {}})
    return _auto_execution_decider_handler(request)


def test_failure_checkpoint_escalation_then_human_pick_reaches_completed(monkeypatch, tmp_path):
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.delenv("AITE_DUPLICATE_LEDGER_DEFECT", raising=False)
    model = ScriptedModel(
        turns=[
            [("execute_tests", {})],
            "Tests executed.",
            [("finalize_analysis", {})],
            # -- paused here: FAILURE escalated --
            "Analysis recorded.",
            [("render_report", {})],
            "Report finalized.",
        ]
    )
    monkeypatch.setattr(checkpoints, "DeciderClient", _mock_decider_client_factory(httpx.MockTransport(_escalating_failure_decider_handler)))
    run_id = _build_approved_run(tmp_path, model)

    paused = orchestration.execute_approved_run(run_id)
    assert paused["stage"] == "AWAITING_DECISION_CONFIRMATION"
    assert paused["pending_decision"]["tool_name"] == "finalize_analysis"

    final = orchestration.resume_pending_decision(run_id, "REPORT_FAILURE")

    assert final["workflow_state"] == "COMPLETED"
    controller = orchestration._ACTIVE_RUNS[run_id]["controller"]
    analysis = json.loads((controller.run_dir / "analysis.json").read_text())
    assert analysis["handling_action"] == "REPORT_FAILURE"
    decisions = json.loads((controller.run_dir / "decisions.json").read_text())
    assert any(d["checkpoint"] == "FAILURE" and d["decision_source"] == "HUMAN_ESCALATION" for d in decisions)
    assert (controller.run_dir / "report.md").exists()
