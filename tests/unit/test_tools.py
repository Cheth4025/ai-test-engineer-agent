"""Unit tests for ai_test_engineer.tools: deterministic validation + attempt cap.

All inputs below are synthetic, hand-built fixtures for testing validation
logic in isolation -- never real Bedrock output. No Bedrock/Agent call is
made anywhere in this file. Fixtures are plain dicts, matching what Strands
actually hands a @tool function body at runtime (FunctionToolMetadata.
validate_input() calls .model_dump() on its way out, so nested list items
arrive as dicts even though the tool signature types them as RuleItem/
ScenarioItem for schema-generation purposes).
"""

from __future__ import annotations

import pytest

from ai_test_engineer.tools import (
    AttemptLog,
    BedrockOutputError,
    ToolAttemptsExhausted,
    _run_with_attempt_cap,
    _validate_interpretation,
    _validate_scenario_set,
    build_generate_missing_scenarios_tool,
    build_save_scenario_set_tool,
)

REQUIREMENT_TEXT = (
    "As a customer, I want to retry a payment when the payment gateway times "
    "out, without being charged twice.\n"
    "BR01: Every payment request must include a unique idempotency key.\n"
    "BR02: Reusing the same key with the same payload resolves the original payment.\n"
    "BR03: Reusing the same key with a different amount is rejected.\n"
    "BR04: A timeout must not automatically mark the payment as failed.\n"
    "BR05: Verify the original payment status before initiating another charge.\n"
    "BR06: Re-delivery of the same successful callback must not create another posting.\n"
)


def _rule(rule_id: str, excerpt: str) -> dict:
    return {
        "rule_id": rule_id,
        "source_excerpt": excerpt,
        "meaning": f"meaning of {rule_id}",
        "expected_observations": [f"{rule_id} observation"],
    }


GOOD_RULES = [
    _rule("BR01", "BR01: Every payment request must include a unique idempotency key."),
    _rule("BR02", "BR02: Reusing the same key with the same payload resolves the original payment."),
    _rule("BR03", "BR03: Reusing the same key with a different amount is rejected."),
    _rule("BR04", "BR04: A timeout must not automatically mark the payment as failed."),
    _rule("BR05", "BR05: Verify the original payment status before initiating another charge."),
    _rule("BR06", "BR06: Re-delivery of the same successful callback must not create another posting."),
]


def test_valid_interpretation_passes():
    result = _validate_interpretation(GOOD_RULES, [], REQUIREMENT_TEXT)
    assert sorted(r["rule_id"] for r in result["rules"]) == [f"BR0{n}" for n in range(1, 7)]
    assert result["ambiguities"] == []


def test_interpretation_rejects_five_rules():
    with pytest.raises(BedrockOutputError, match="exactly six"):
        _validate_interpretation(GOOD_RULES[:5], [], REQUIREMENT_TEXT)


def test_interpretation_rejects_fabricated_source_quote():
    bad_rules = list(GOOD_RULES)
    bad_rules[0] = _rule("BR01", "This sentence does not appear in the requirement text.")
    with pytest.raises(BedrockOutputError, match="not a literal substring"):
        _validate_interpretation(bad_rules, [], REQUIREMENT_TEXT)


def test_interpretation_rejects_duplicate_rule_id():
    # BR05 twice, BR06 missing -- six items, but not the required ID set.
    bad_rules = list(GOOD_RULES[:5]) + [GOOD_RULES[4]]
    with pytest.raises(BedrockOutputError, match="exactly six"):
        _validate_interpretation(bad_rules, [], REQUIREMENT_TEXT)


def test_interpretation_rejects_ambiguity_with_no_rule_ids():
    ambiguities = [
        {"id": "AMB01", "rule_ids": [], "question": "which gateway?", "critical": False, "status": "open"}
    ]
    with pytest.raises(BedrockOutputError, match="no rule_ids"):
        _validate_interpretation(GOOD_RULES, ambiguities, REQUIREMENT_TEXT)


def test_interpretation_accepts_ambiguity_with_rule_ids():
    ambiguities = [
        {"id": "AMB01", "rule_ids": ["BR01"], "question": "which gateway?", "critical": False, "status": "open"}
    ]
    result = _validate_interpretation(GOOD_RULES, ambiguities, REQUIREMENT_TEXT)
    assert result["ambiguities"][0]["id"] == "AMB01"


def _scenario(scenario_id: str) -> dict:
    return {
        "scenario_id": scenario_id,
        "title": f"title {scenario_id}",
        "rule_ids": ["BR01"],
        "test_id": f"T{scenario_id[1:]}",
        "preconditions": ["precondition"],
        "actions": ["action"],
        "action_codes": ["CODE"],
        "expected_assertions": [{"observation": "obs", "operator": "EQ", "expected": 1}],
        "gherkin": f"Given ... When ... Then ... ({scenario_id})",
    }


def test_valid_scenario_set_passes_and_records_s04_omission():
    target_ids = ["S01", "S02", "S03", "S05"]
    scenarios = [_scenario(sid) for sid in target_ids]

    result = _validate_scenario_set(target_ids, scenarios, target_ids)

    assert result["version"] == 1
    assert {s["scenario_id"] for s in result["scenarios"]} == set(target_ids)
    assert result["omission_event"]["scenario_id"] == "S04"
    assert result["omission_event"]["event_type"] == "SCENARIO_OMISSION"


def test_scenario_set_rejects_missing_requested_id():
    target_ids = ["S01", "S02", "S03", "S05"]
    scenarios = [_scenario(sid) for sid in target_ids[:3]]  # S05 missing

    with pytest.raises(BedrockOutputError, match="must cover exactly target_ids"):
        _validate_scenario_set(target_ids, scenarios, target_ids)


def test_scenario_set_rejects_unrequested_s04():
    target_ids = ["S01", "S02", "S03", "S05"]
    scenarios = [_scenario(sid) for sid in target_ids] + [_scenario("S04")]

    with pytest.raises(BedrockOutputError, match="must cover exactly target_ids"):
        _validate_scenario_set(target_ids, scenarios, target_ids)


def test_omission_event_absent_when_s04_included():
    target_ids = ["S01", "S02", "S03", "S04", "S05"]
    scenarios = [_scenario(sid) for sid in target_ids]

    result = _validate_scenario_set(target_ids, scenarios, target_ids)

    assert result["omission_event"] is None


# -- attempt cap --------------------------------------------------------------


def test_attempt_cap_rejects_third_attempt_without_validating():
    attempt_log = AttemptLog()
    attempt_log.record_failure("interpret_requirement", 1, "bad #1")
    attempt_log.record_failure("interpret_requirement", 2, "bad #2")

    calls: list[int] = []

    def stub() -> dict:
        calls.append(1)
        return {"ok": True}

    with pytest.raises(ToolAttemptsExhausted, match="already used its last allowed attempt"):
        _run_with_attempt_cap(attempt_log, "interpret_requirement", stub)

    assert calls == []  # the 3rd attempt never even runs validation
    assert attempt_log.count("interpret_requirement") == 2  # rejected attempt isn't recorded


def test_attempt_cap_raises_exhausted_on_second_failure():
    attempt_log = AttemptLog()

    def always_fails() -> dict:
        raise BedrockOutputError("still wrong")

    with pytest.raises(BedrockOutputError) as first:
        _run_with_attempt_cap(attempt_log, "generate_scenarios", always_fails)
    assert not isinstance(first.value, ToolAttemptsExhausted)
    assert attempt_log.count("generate_scenarios") == 1

    with pytest.raises(ToolAttemptsExhausted, match=r"failed validation on attempt 2/2"):
        _run_with_attempt_cap(attempt_log, "generate_scenarios", always_fails)
    assert attempt_log.count("generate_scenarios") == 2
    assert attempt_log.get_result("generate_scenarios") is None


def test_attempt_cap_allows_success_after_one_failure():
    attempt_log = AttemptLog()
    attempts = iter([BedrockOutputError("first try wrong"), {"ok": True}])

    def flaky() -> dict:
        value = next(attempts)
        if isinstance(value, Exception):
            raise value
        return value

    with pytest.raises(BedrockOutputError):
        _run_with_attempt_cap(attempt_log, "generate_scenarios", flaky)

    result = _run_with_attempt_cap(attempt_log, "generate_scenarios", flaky)

    assert result == {"ok": True}
    assert attempt_log.get_result("generate_scenarios") == {"ok": True}
    assert attempt_log.count("generate_scenarios") == 2


# -- overwrite_result ---------------------------------------------------------


def test_overwrite_result_replaces_without_recording_attempt():
    attempt_log = AttemptLog()
    attempt_log.record_success("generate_scenarios", 1, {"version": 1, "scenarios": []})

    attempt_log.overwrite_result("generate_scenarios", {"version": 2, "scenarios": ["merged"]})

    assert attempt_log.get_result("generate_scenarios") == {"version": 2, "scenarios": ["merged"]}
    assert attempt_log.count("generate_scenarios") == 1  # no new attempt recorded


# -- save_scenario_set ---------------------------------------------------------

_MINI_CATALOG = {
    "required_scenarios": ["S01"],
    "scenarios": {
        "S01": {
            "scenario_id": "S01",
            "test_id": "T01",
            "rule_ids": ["BR01"],
            "required_preconditions": ["precondition"],
            "required_action_codes": ["CODE"],
            "required_assertions": [{"observation": "obs", "operator": "EQ", "expected": 1}],
        }
    },
}


def test_save_scenario_set_zero_arg_reads_attempt_log():
    attempt_log = AttemptLog()
    attempt_log.record_success(
        "generate_scenarios", 1, {"version": 1, "scenarios": [_scenario("S01")], "omission_event": None}
    )
    save_scenario_set = build_save_scenario_set_tool(attempt_log, _MINI_CATALOG)

    result = save_scenario_set()

    assert result["scenario_set"]["scenarios"][0]["scenario_id"] == "S01"
    assert result["coverage"]["covered"] == 1
    assert result["coverage"]["required"] == 1


def test_save_scenario_set_before_generate_scenarios_raises():
    attempt_log = AttemptLog()
    save_scenario_set = build_save_scenario_set_tool(attempt_log, _MINI_CATALOG)

    with pytest.raises(BedrockOutputError, match="before generate_scenarios succeeded"):
        save_scenario_set()


def test_save_scenario_set_exhausts_after_two_attempts():
    attempt_log = AttemptLog()
    save_scenario_set = build_save_scenario_set_tool(attempt_log, _MINI_CATALOG)

    with pytest.raises(BedrockOutputError):
        save_scenario_set()
    with pytest.raises(ToolAttemptsExhausted):
        save_scenario_set()


# -- generate_missing_scenarios ------------------------------------------------


def test_generate_missing_scenarios_validates_against_its_own_target_ids():
    attempt_log = AttemptLog()
    generate_missing_scenarios = build_generate_missing_scenarios_tool(attempt_log, ["S04"])

    result = generate_missing_scenarios(target_ids=["S04"], scenarios=[_scenario("S04")])

    assert {s["scenario_id"] for s in result["scenarios"]} == {"S04"}
    assert result["omission_event"] is None  # S04 is present in this call's target_ids


def test_generate_missing_scenarios_has_independent_attempt_budget_from_generate_scenarios():
    attempt_log = AttemptLog()
    attempt_log.record_failure("generate_scenarios", 1, "unrelated failure")
    attempt_log.record_failure("generate_scenarios", 2, "unrelated failure")
    generate_missing_scenarios = build_generate_missing_scenarios_tool(attempt_log, ["S04"])

    # generate_scenarios' budget is exhausted, but generate_missing_scenarios
    # tracks attempts under its own key and is unaffected.
    result = generate_missing_scenarios(target_ids=["S04"], scenarios=[_scenario("S04")])

    assert {s["scenario_id"] for s in result["scenarios"]} == {"S04"}
