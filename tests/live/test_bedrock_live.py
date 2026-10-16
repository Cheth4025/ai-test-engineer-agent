"""Real-Bedrock smoke test for the Milestone 3 agent. Deselected by default.

Run manually (Decider server off, this test only):
    .venv/Scripts/python.exe -m pytest tests/live -v -m live -s -k bedrock
"""

from __future__ import annotations

import pytest

from ai_test_engineer.orchestration import run_initial_pass

pytestmark = pytest.mark.live


def test_live_run_initial_pass_extracts_six_rules_and_reports_4_of_5():
    result = run_initial_pass()

    rules = result["interpretation"]["rules"]
    rule_ids = sorted(rule["rule_id"] for rule in rules)
    assert rule_ids == [f"BR0{n}" for n in range(1, 7)]

    coverage = result["coverage"]
    assert coverage["covered"] == 4
    assert coverage["required"] == 5
    s04_row = next(row for row in coverage["rows"] if row["scenario_id"] == "S04")
    assert s04_row["status"] == "MISSING"

    print(f"\nrun_id: {result['run_id']}")
    print("tool attempts:")
    for tool_name, count in result["tool_attempts"].items():
        print(f"  {tool_name}: {count} attempt(s)")
    print(f"coverage: {coverage['covered']}/{coverage['required']} ({coverage['percentage']}%)")
    print("extracted rules:")
    for rule in sorted(rules, key=lambda r: r["rule_id"]):
        print(f"  {rule['rule_id']}: {rule['meaning']}")
