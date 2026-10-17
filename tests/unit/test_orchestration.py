"""Unit tests for ai_test_engineer.orchestration's static prompt builders and
_unexpected_interrupt_stop (the no-agent-needed safety net used when an
interrupt surfaces somewhere stage gating doesn't catch).

Driving the full Agent-based flow (run_initial_pass / resume_pending_decision
/ _drive_recheck) needs a Strands Agent and a Decider client, so that's
covered by tests/unit/test_orchestration_stubbed.py instead, not here.
"""

from __future__ import annotations

import json

from ai_test_engineer.orchestration import (
    CONFIG_DIR,
    _interpretation_prompt,
    _unexpected_interrupt_stop,
)
from ai_test_engineer.state import RunController

_CATALOG = json.loads((CONFIG_DIR / "scenario_catalog.json").read_text())


def test_interpretation_prompt_scopes_critical_to_s01_s05():
    prompt = _interpretation_prompt("some requirement text", _CATALOG)

    assert "S01" in prompt and "S05" in prompt
    assert "T01" in prompt and "T05" in prompt
    assert "critical ONLY if" in prompt
    assert "scripted" in prompt.lower() and "deterministic" in prompt.lower()


def test_interpretation_prompt_lists_out_of_scope_topics_as_non_critical_not_omitted():
    prompt = _interpretation_prompt("some requirement text", _CATALOG)

    for topic in ("key generation ownership", "TTLs", "non-amount payload fields"):
        assert topic in prompt
    assert "non-critical" in prompt
    assert "never suppress" in prompt
    assert "omitting" in prompt


def test_interpretation_prompt_includes_real_scenario_assertions_for_each_s01_s05():
    prompt = _interpretation_prompt("some requirement text", _CATALOG)

    for scenario_id in ("S01", "S02", "S03", "S04", "S05"):
        scenario = _CATALOG["scenarios"][scenario_id]
        assert scenario["title"] in prompt
        assert scenario["required_assertions"][0]["observation"] in prompt
    assert "rule_id" in prompt and "GENERAL" in prompt


class _FakeInterrupt:
    def __init__(self, id: str, name: str, reason: str) -> None:
        self.id = id
        self.name = name
        self.reason = reason


class _FakeResult:
    def __init__(self, interrupts: list) -> None:
        self.interrupts = interrupts


def test_unexpected_interrupt_stop_records_evidence_and_stops_the_run(tmp_path):
    controller = RunController("test-run", tmp_path)
    interrupt = _FakeInterrupt(id="v1:before_tool_call:x:y", name="decider-gate", reason="low confidence")

    outcome = _unexpected_interrupt_stop("test-run", controller, _FakeResult([interrupt]), "some turn")

    expected_interrupts = [{"id": interrupt.id, "name": "decider-gate", "reason": "low confidence"}]
    assert outcome["workflow_state"] == "STOPPED"
    assert outcome["stage"] == "STOPPED"
    assert outcome["interrupts"] == expected_interrupts
    assert controller.workflow_state == "STOPPED"
    evidence = json.loads((tmp_path / "unexpected_interrupt.json").read_text())
    assert evidence == {"context": "some turn", "interrupts": expected_interrupts}
