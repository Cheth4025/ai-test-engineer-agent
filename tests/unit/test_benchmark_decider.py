"""Unit tests for scripts/benchmark_decider.py -- fully stubbed, no network.

Mirrors this repo's tests/live vs tests/unit split: nothing here touches a
real Decider server or real Bedrock. scripts.benchmark_decider.main() itself
(the only part that makes real network calls) is intentionally NOT exercised
here -- only its pure/injectable pieces are.
"""

from __future__ import annotations

import json

import pytest

from decider_gate.gate import ConfidenceGate
from scripts import benchmark_decider as bd

# ---------------------------------------------------------------------------
# Fixture items
# ---------------------------------------------------------------------------

SINGLE_NOUL_ITEM = {
    "item_id": "ambiguity_br01_clear_not_ambiguous",
    "item_type": "single",
    "question_type": "ambiguity_BRxx",
    "question_name": "ambiguity_BR01",
    "question": {
        "type": "noul",
        "instructions": "Rule BR01 ... non-critical ... does not change any assertion.",
        "criteria": {"true": "leaves a question that changes the test", "false": "fully specified"},
    },
    "state": {"rules": [{"rule_id": "BR01", "meaning": "..."}]},
    "label": False,
    "label_rationale": "BR01's ambiguities are both non-critical and out of scope.",
}

SINGLE_CHOICE_ITEM = {
    "item_id": "failure_action_report_clear",
    "item_type": "single",
    "question_type": "failure_action",
    "question_name": "failure_action",
    "question": {
        "type": "choice",
        "instructions": "Given this failure, investigate further or report now?",
        "criteria": {"REPORT_FAILURE": "evidence is conclusive", "INVESTIGATE_FAILURE": "evidence is incomplete"},
    },
    "state": {"failure_facts": {"has_failure": True, "test_id": "T04"}},
    "label": "REPORT_FAILURE",
    "label_rationale": "Evidence is conclusive; no further investigation is needed.",
}

REPLAY_ITEM = {
    "item_id": "replay_coverage_run_20261021",
    "item_type": "replay",
    "question_type": "replay_COVERAGE",
    "questions": {
        "grounding_S01": {
            "type": "noul",
            "instructions": "Does S01's Gherkin match its oracle?",
            "criteria": {"true": "matches", "false": "diverges"},
        },
        "coverage_action": {
            "type": "choice",
            "instructions": "Accept coverage or remediate?",
            "criteria": {"ACCEPT_COVERAGE": "complete", "REMEDIATE_GAP": "gap exists"},
        },
    },
    "state": {"validator_matrix": {"covered": 5, "required": 5}},
    "label": {"grounding_S01": True, "coverage_action": "ACCEPT_COVERAGE"},
    "label_rationale": "Copied verbatim from a run where 5/5 scenarios were VALID.",
    "live_reference": {
        "source_run": "runs/20261021T034730Z_f18751e6/decisions.json",
        "live_confidence": {"grounding_S01": 0.6609, "coverage_action": 0.8711},
    },
}


# ---------------------------------------------------------------------------
# load_benchmark_items / validate_item
# ---------------------------------------------------------------------------


def test_load_benchmark_items_reads_a_json_array(tmp_path):
    path = tmp_path / "bench.json"
    path.write_text(json.dumps([SINGLE_NOUL_ITEM, SINGLE_CHOICE_ITEM]), encoding="utf-8")

    items = bd.load_benchmark_items(path)

    assert [item["item_id"] for item in items] == [
        "ambiguity_br01_clear_not_ambiguous",
        "failure_action_report_clear",
    ]


def test_validate_item_accepts_well_formed_single_item():
    bd.validate_item(SINGLE_NOUL_ITEM)  # raises on failure; no exception means pass


def test_validate_item_accepts_well_formed_replay_item():
    bd.validate_item(REPLAY_ITEM)


def test_validate_item_rejects_a_label_not_in_its_own_criteria():
    bad = {**SINGLE_CHOICE_ITEM, "label": "NOT_A_REAL_OPTION"}
    with pytest.raises(ValueError):
        bd.validate_item(bad)


def test_validate_item_rejects_replay_label_missing_a_question():
    bad = {**REPLAY_ITEM, "label": {"grounding_S01": True}}  # coverage_action label missing
    with pytest.raises(ValueError):
        bd.validate_item(bad)


def test_real_benchmark_file_loads_and_every_item_validates():
    items = bd.load_benchmark_items(bd.DEFAULT_BENCHMARK_FILE)
    assert len(items) >= 35
    for item in items:
        bd.validate_item(item)


# ---------------------------------------------------------------------------
# call_decider
# ---------------------------------------------------------------------------


class _StubDeciderClient:
    def __init__(self, answers_by_questions=None, latency_ms=100, model="stub-model", exc=None):
        self._answers_by_questions = answers_by_questions
        self._latency_ms = latency_ms
        self._model = model
        self._exc = exc
        self.calls = []

    def ask(self, state, questions):
        self.calls.append({"state": state, "questions": questions})
        if self._exc is not None:
            raise self._exc
        answers = self._answers_by_questions
        if callable(answers):
            answers = answers(questions)
        return {
            "model": self._model,
            "usage": None,
            "answers": answers,
            "latency_ms": self._latency_ms,
            "raw_response": {},
        }


def test_call_decider_sends_one_question_for_a_single_item_and_marks_it_correct():
    client = _StubDeciderClient(
        answers_by_questions={"ambiguity_BR01": {"type": "noul", "noul": 0.1}}
    )
    gate = ConfidenceGate()

    result = bd.call_decider(client, gate, SINGLE_NOUL_ITEM)

    assert len(client.calls) == 1
    assert list(client.calls[0]["questions"].keys()) == ["ambiguity_BR01"]
    assert result.error is None
    assert len(result.outcomes) == 1
    outcome = result.outcomes[0]
    assert outcome.question_name == "ambiguity_BR01"
    assert outcome.route == "AUTO"
    assert outcome.correct is True  # suggestion False == label False
    assert outcome.probabilities == {"true": 0.1, "false": 0.9}


def test_call_decider_marks_a_wrong_suggestion_as_incorrect():
    client = _StubDeciderClient(
        answers_by_questions={
            "failure_action": {
                "type": "choice",
                "choice": "INVESTIGATE_FAILURE",
                "confidence": 0.9,
                "probabilities": {"REPORT_FAILURE": 0.3, "INVESTIGATE_FAILURE": 0.7},
            }
        }
    )
    gate = ConfidenceGate()

    result = bd.call_decider(client, gate, SINGLE_CHOICE_ITEM)

    outcome = result.outcomes[0]
    assert outcome.route == "AUTO"
    assert outcome.correct is False  # label is REPORT_FAILURE


def test_call_decider_sends_every_question_of_a_replay_item_in_one_batched_call():
    client = _StubDeciderClient(
        answers_by_questions={
            "grounding_S01": {"type": "noul", "noul": 0.9},
            "coverage_action": {
                "type": "choice",
                "choice": "ACCEPT_COVERAGE",
                "confidence": 0.87,
                "probabilities": {"ACCEPT_COVERAGE": 0.93, "REMEDIATE_GAP": 0.07},
            },
        }
    )
    gate = ConfidenceGate()

    result = bd.call_decider(client, gate, REPLAY_ITEM)

    assert len(client.calls) == 1
    assert set(client.calls[0]["questions"].keys()) == {"grounding_S01", "coverage_action"}
    assert {o.question_name for o in result.outcomes} == {"grounding_S01", "coverage_action"}
    assert all(o.correct for o in result.outcomes)


def test_call_decider_records_error_and_does_not_raise_when_client_fails():
    client = _StubDeciderClient(exc=RuntimeError("boom"))
    gate = ConfidenceGate()

    result = bd.call_decider(client, gate, SINGLE_NOUL_ITEM)

    assert result.error == "boom"
    assert result.outcomes == []


# ---------------------------------------------------------------------------
# build_bedrock_prompt / parse_bedrock_json_answer
# ---------------------------------------------------------------------------


def test_build_bedrock_prompt_includes_the_state_blob_not_just_instructions():
    prompt = bd.build_bedrock_prompt(SINGLE_NOUL_ITEM)

    assert "non-critical" in prompt  # instructions text present
    assert "BR01" in prompt  # state blob content present (fairness requirement)
    assert "json" in prompt.lower()


def test_build_bedrock_prompt_includes_every_question_of_a_replay_item():
    prompt = bd.build_bedrock_prompt(REPLAY_ITEM)

    assert "grounding_S01" in prompt
    assert "coverage_action" in prompt


def test_parse_bedrock_json_answer_handles_a_fenced_code_block():
    raw = '```json\n{"ambiguity_BR01": {"answer": false, "confidence": 0.9}}\n```'
    parsed = bd.parse_bedrock_json_answer(raw)
    assert parsed == {"ambiguity_BR01": {"answer": False, "confidence": 0.9}}


def test_parse_bedrock_json_answer_handles_leading_prose():
    raw = 'Sure, here is my answer:\n{"failure_action": {"answer": "REPORT_FAILURE", "confidence": 0.8}}'
    parsed = bd.parse_bedrock_json_answer(raw)
    assert parsed == {"failure_action": {"answer": "REPORT_FAILURE", "confidence": 0.8}}


def test_parse_bedrock_json_answer_raises_on_unparseable_text():
    with pytest.raises(ValueError):
        bd.parse_bedrock_json_answer("I refuse to answer in JSON.")


# ---------------------------------------------------------------------------
# call_bedrock
# ---------------------------------------------------------------------------


class _StubBedrockClient:
    def __init__(self, response_text=None, input_tokens=100, output_tokens=20, exc=None):
        self._response_text = response_text
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self._exc = exc
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return {
            "output": {"message": {"content": [{"text": self._response_text}]}},
            "usage": {"inputTokens": self._input_tokens, "outputTokens": self._output_tokens},
        }


def test_call_bedrock_parses_answer_extracts_tokens_and_marks_correct():
    client = _StubBedrockClient(
        response_text='{"ambiguity_BR01": {"answer": false, "confidence": 0.92}}'
    )

    result = bd.call_bedrock(client, "us.anthropic.claude-sonnet-4-6", SINGLE_NOUL_ITEM)

    assert result.error is None
    assert result.input_tokens == 100
    assert result.output_tokens == 20
    outcome = result.outcomes[0]
    assert outcome.answer is False
    assert outcome.confidence == 0.92
    assert outcome.correct is True


def test_call_bedrock_records_error_and_does_not_raise_when_client_fails():
    client = _StubBedrockClient(exc=RuntimeError("throttled"))

    result = bd.call_bedrock(client, "us.anthropic.claude-sonnet-4-6", SINGLE_NOUL_ITEM)

    assert result.error == "throttled"
    assert result.outcomes == []


def test_call_bedrock_records_error_when_response_is_unparseable():
    client = _StubBedrockClient(response_text="not json at all")

    result = bd.call_bedrock(client, "us.anthropic.claude-sonnet-4-6", SINGLE_NOUL_ITEM)

    assert result.error is not None
    assert result.outcomes == []


# ---------------------------------------------------------------------------
# compute_summary / render_markdown_summary
# ---------------------------------------------------------------------------


def _decider_result(item_id, question_type, outcomes, latency_ms=100, error=None):
    return bd.DeciderResult(item_id=item_id, question_type=question_type, latency_ms=latency_ms, error=error, outcomes=outcomes)


def _bedrock_result(item_id, question_type, outcomes, latency_ms=500, input_tokens=100, output_tokens=20, error=None):
    return bd.BedrockResult(
        item_id=item_id,
        question_type=question_type,
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        error=error,
        outcomes=outcomes,
    )


def test_compute_summary_per_question_type_accuracy_and_escalation_rate():
    items = [SINGLE_NOUL_ITEM, SINGLE_CHOICE_ITEM]
    decider_results = [
        _decider_result(
            "ambiguity_br01_clear_not_ambiguous",
            "ambiguity_BRxx",
            [bd.Outcome("ambiguity_BR01", False, 0.9, {"true": 0.1, "false": 0.9}, "AUTO", False, True)],
        ),
        _decider_result(
            "failure_action_report_clear",
            "failure_action",
            [bd.Outcome("failure_action", None, 0.3, {"REPORT_FAILURE": 0.6, "INVESTIGATE_FAILURE": 0.4}, "ESCALATE", "REPORT_FAILURE", True)],
        ),
    ]
    bedrock_results = [
        _bedrock_result(
            "ambiguity_br01_clear_not_ambiguous",
            "ambiguity_BRxx",
            [bd.Outcome("ambiguity_BR01", False, 0.95, None, None, False, True)],
        ),
        _bedrock_result(
            "failure_action_report_clear",
            "failure_action",
            [bd.Outcome("failure_action", "REPORT_FAILURE", 0.8, None, None, "REPORT_FAILURE", True)],
        ),
    ]

    summary = bd.compute_summary(decider_results, bedrock_results, items, bd.DEFAULT_DECIDER_CFG)

    ambiguity = summary["per_question_type"]["ambiguity_BRxx"]
    assert ambiguity["n"] == 1
    assert ambiguity["decider_accuracy"] == 1.0
    assert ambiguity["bedrock_accuracy"] == 1.0
    assert ambiguity["decider_escalation_rate"] == 0.0

    failure_action = summary["per_question_type"]["failure_action"]
    assert failure_action["decider_escalation_rate"] == 1.0


def test_compute_summary_latency_percentiles():
    items = [SINGLE_NOUL_ITEM]
    decider_results = [
        _decider_result("a", "ambiguity_BRxx", [bd.Outcome("q", False, 0.9, None, "AUTO", False, True)], latency_ms=100),
    ]
    bedrock_results = [
        _bedrock_result("a", "ambiguity_BRxx", [bd.Outcome("q", False, 0.9, None, None, False, True)], latency_ms=500),
    ]

    summary = bd.compute_summary(decider_results, bedrock_results, items, bd.DEFAULT_DECIDER_CFG)

    assert summary["latency"]["decider_p50_ms"] == 100
    assert summary["latency"]["bedrock_p50_ms"] == 500


def test_compute_summary_bedrock_cost_per_decision():
    items = [SINGLE_NOUL_ITEM]
    decider_results = [_decider_result("a", "ambiguity_BRxx", [])]
    bedrock_results = [
        _bedrock_result("a", "ambiguity_BRxx", [], input_tokens=1_000_000, output_tokens=1_000_000)
    ]

    summary = bd.compute_summary(decider_results, bedrock_results, items, bd.DEFAULT_DECIDER_CFG)

    expected = bd.BEDROCK_INPUT_COST_PER_1M + bd.BEDROCK_OUTPUT_COST_PER_1M
    assert summary["cost"]["bedrock_cost_per_decision_usd"] == pytest.approx(expected)


def test_compute_summary_threshold_sweep_choice_uses_confidence_not_top_probability():
    """A choice item whose self-reported confidence and top probability diverge
    (per Phase 1 finding 3) must sweep against confidence -- top probability is
    informational only."""
    items = [SINGLE_CHOICE_ITEM]
    decider_results = [
        _decider_result(
            "failure_action_report_clear",
            "failure_action",
            [bd.Outcome("failure_action", "REPORT_FAILURE", 0.27, {"REPORT_FAILURE": 0.65, "INVESTIGATE_FAILURE": 0.35}, "ESCALATE", "REPORT_FAILURE", True)],
        )
    ]
    bedrock_results = [_bedrock_result("failure_action_report_clear", "failure_action", [])]

    summary = bd.compute_summary(decider_results, bedrock_results, items, bd.DEFAULT_DECIDER_CFG)

    sweep = summary["threshold_sweep"]["failure_action"]
    assert sweep["type"] == "choice"
    assert sweep["thresholds"] == bd.CHOICE_THRESHOLD_SWEEP
    # confidence is 0.27: auto only at thresholds <= 0.27 -> none of the swept thresholds qualify
    assert sweep["auto_rate"] == [0.0] * len(bd.CHOICE_THRESHOLD_SWEEP)
    # top probability column is informational, present, and NOT what drove auto_rate
    assert sweep["top_probability_avg"] == pytest.approx(0.65)


def test_compute_summary_threshold_sweep_choice_auto_rate_follows_confidence():
    items = [SINGLE_CHOICE_ITEM]
    decider_results = [
        _decider_result(
            "failure_action_report_clear",
            "failure_action",
            [bd.Outcome("failure_action", "REPORT_FAILURE", 0.82, {"REPORT_FAILURE": 0.60, "INVESTIGATE_FAILURE": 0.40}, "AUTO", "REPORT_FAILURE", True)],
        )
    ]
    bedrock_results = [_bedrock_result("failure_action_report_clear", "failure_action", [])]

    summary = bd.compute_summary(decider_results, bedrock_results, items, bd.DEFAULT_DECIDER_CFG)

    sweep = summary["threshold_sweep"]["failure_action"]
    # confidence 0.82 clears every threshold up to 0.80, not 0.85
    assert sweep["auto_rate"] == [1.0, 1.0, 1.0, 1.0, 1.0, 0.0]
    assert sweep["auto_accuracy"][0] == 1.0


def test_compute_summary_threshold_sweep_noul_upper_only():
    items = [SINGLE_NOUL_ITEM]
    decider_results = [
        _decider_result(
            "ambiguity_br01_clear_not_ambiguous",
            "ambiguity_BRxx",
            [bd.Outcome("ambiguity_BR01", False, 0.9, {"true": 0.1, "false": 0.9}, "AUTO", False, True)],
        )
    ]
    bedrock_results = [_bedrock_result("ambiguity_br01_clear_not_ambiguous", "ambiguity_BRxx", [])]

    summary = bd.compute_summary(decider_results, bedrock_results, items, bd.DEFAULT_DECIDER_CFG)

    sweep = summary["threshold_sweep"]["ambiguity_BRxx"]
    assert sweep["type"] == "noul"
    assert sweep["thresholds"] == bd.NOUL_UPPER_SWEEP
    assert "top_probability_avg" not in sweep


def test_compute_summary_replay_fidelity_flags_large_divergence():
    items = [REPLAY_ITEM]
    decider_results = [
        _decider_result(
            "replay_coverage_run_20261021",
            "replay_COVERAGE",
            [
                bd.Outcome("grounding_S01", None, 0.50, {"true": 0.50, "false": 0.50}, "ESCALATE", True, True),
                bd.Outcome("coverage_action", "ACCEPT_COVERAGE", 0.87, {"ACCEPT_COVERAGE": 0.93, "REMEDIATE_GAP": 0.07}, "AUTO", "ACCEPT_COVERAGE", True),
            ],
        )
    ]
    bedrock_results = [_bedrock_result("replay_coverage_run_20261021", "replay_COVERAGE", [])]

    summary = bd.compute_summary(decider_results, bedrock_results, items, bd.DEFAULT_DECIDER_CFG)

    fidelity = {row["question_name"]: row for row in summary["replay_fidelity"]}
    assert fidelity["grounding_S01"]["flagged"] is True  # 0.6609 live vs 0.50 benchmark, diff = 0.1609 > 0.05
    assert fidelity["coverage_action"]["flagged"] is False  # 0.8711 live vs 0.87 benchmark, diff = 0.0011


def test_compute_summary_notes_failure_action_gap_without_changing_anything():
    items = [SINGLE_CHOICE_ITEM]
    decider_results = [
        _decider_result(
            "failure_action_report_clear",
            "failure_action",
            [bd.Outcome("failure_action", "REPORT_FAILURE", 0.27, {"REPORT_FAILURE": 0.65, "INVESTIGATE_FAILURE": 0.35}, "ESCALATE", "REPORT_FAILURE", True)],
        )
    ]
    bedrock_results = [_bedrock_result("failure_action_report_clear", "failure_action", [])]

    summary = bd.compute_summary(decider_results, bedrock_results, items, bd.DEFAULT_DECIDER_CFG)

    assert any("failure_action" in note for note in summary["notes"])


def test_render_markdown_summary_includes_key_numbers():
    items = [SINGLE_NOUL_ITEM]
    decider_results = [
        _decider_result(
            "ambiguity_br01_clear_not_ambiguous",
            "ambiguity_BRxx",
            [bd.Outcome("ambiguity_BR01", False, 0.9, {"true": 0.1, "false": 0.9}, "AUTO", False, True)],
        )
    ]
    bedrock_results = [_bedrock_result("ambiguity_br01_clear_not_ambiguous", "ambiguity_BRxx", [])]
    summary = bd.compute_summary(decider_results, bedrock_results, items, bd.DEFAULT_DECIDER_CFG)

    markdown = bd.render_markdown_summary(summary)

    assert "ambiguity_BRxx" in markdown
    assert "Threshold sweep" in markdown


# ---------------------------------------------------------------------------
# CLI filtering (--only / --limit)
# ---------------------------------------------------------------------------


def test_filter_items_only_keeps_matching_question_type():
    items = [SINGLE_NOUL_ITEM, SINGLE_CHOICE_ITEM]
    filtered = bd.filter_items(items, only="failure_action", limit=None)
    assert [item["item_id"] for item in filtered] == ["failure_action_report_clear"]


def test_filter_items_limit_caps_count():
    items = [SINGLE_NOUL_ITEM, SINGLE_CHOICE_ITEM, REPLAY_ITEM]
    filtered = bd.filter_items(items, only=None, limit=2)
    assert len(filtered) == 2


# ---------------------------------------------------------------------------
# Bug regression: Bedrock scored 0% on every noul item because the prompt
# listed a noul question's criteria dict keys ("true"/"false" strings) as
# "valid answers", so Bedrock dutifully answered with the string "false"
# instead of the JSON boolean False -- which never equals the real bool
# label. See benchmarks/results_20261022T032446Z.json / the calibration
# Phase 3 plan for the real-run numbers this reproduces.
# ---------------------------------------------------------------------------


def test_build_bedrock_prompt_asks_for_a_json_boolean_on_noul_questions_not_a_quoted_string():
    prompt = bd.build_bedrock_prompt(SINGLE_NOUL_ITEM)
    assert "JSON boolean" in prompt
    # the old bug's literal instruction must be gone for noul questions
    assert "Valid answers for 'ambiguity_BR01'" not in prompt


def test_build_bedrock_prompt_still_lists_valid_answers_for_choice_questions():
    prompt = bd.build_bedrock_prompt(SINGLE_CHOICE_ITEM)
    assert "Valid answers for 'failure_action'" in prompt
    assert "REPORT_FAILURE" in prompt


def test_call_bedrock_coerces_a_quoted_true_false_string_for_noul_questions():
    """Reproduces the real bug verbatim: Bedrock answered the literal string
    "false" for a noul question whose correct label is the bool False."""
    client = _StubBedrockClient(
        response_text='{"ambiguity_BR01": {"answer": "false", "confidence": 0.9}}'
    )

    result = bd.call_bedrock(client, "us.anthropic.claude-sonnet-4-6", SINGLE_NOUL_ITEM)

    outcome = result.outcomes[0]
    assert outcome.answer is False  # coerced from the string "false" to a real bool
    assert outcome.correct is True  # label is False


def test_call_bedrock_coerces_a_quoted_true_string_case_insensitively():
    client = _StubBedrockClient(
        response_text='{"ambiguity_BR01": {"answer": "TRUE", "confidence": 0.7}}'
    )

    result = bd.call_bedrock(client, "us.anthropic.claude-sonnet-4-6", SINGLE_NOUL_ITEM)

    outcome = result.outcomes[0]
    assert outcome.answer is True
    assert outcome.correct is False  # label is False


def test_call_bedrock_leaves_a_real_bool_answer_untouched():
    client = _StubBedrockClient(
        response_text='{"ambiguity_BR01": {"answer": false, "confidence": 0.9}}'
    )

    result = bd.call_bedrock(client, "us.anthropic.claude-sonnet-4-6", SINGLE_NOUL_ITEM)

    outcome = result.outcomes[0]
    assert outcome.answer is False
    assert outcome.correct is True


def test_call_bedrock_does_not_coerce_a_choice_answer_string():
    client = _StubBedrockClient(
        response_text='{"failure_action": {"answer": "REPORT_FAILURE", "confidence": 0.8}}'
    )

    result = bd.call_bedrock(client, "us.anthropic.claude-sonnet-4-6", SINGLE_CHOICE_ITEM)

    outcome = result.outcomes[0]
    assert outcome.answer == "REPORT_FAILURE"
    assert outcome.correct is True


# ---------------------------------------------------------------------------
# Held-out items: compute_summary splits them out of per_question_type /
# threshold_sweep and reports them separately, so wording fixes can be
# checked for overfitting against the exact items used to justify them.
# ---------------------------------------------------------------------------


HELD_OUT_NOUL_ITEM = {
    **SINGLE_NOUL_ITEM,
    "item_id": "ambiguity_br06_held_out",
    "held_out": True,
}


def test_compute_summary_excludes_held_out_items_from_per_question_type():
    items = [SINGLE_NOUL_ITEM, HELD_OUT_NOUL_ITEM]
    decider_results = [
        _decider_result(
            "ambiguity_br01_clear_not_ambiguous",
            "ambiguity_BRxx",
            [bd.Outcome("ambiguity_BR01", False, 0.9, {"true": 0.1, "false": 0.9}, "AUTO", False, True)],
        ),
        _decider_result(
            "ambiguity_br06_held_out",
            "ambiguity_BRxx",
            [bd.Outcome("ambiguity_BR01", True, 0.9, {"true": 0.9, "false": 0.1}, "AUTO", True, False)],
        ),
    ]
    bedrock_results = [
        _bedrock_result("ambiguity_br01_clear_not_ambiguous", "ambiguity_BRxx", []),
        _bedrock_result("ambiguity_br06_held_out", "ambiguity_BRxx", []),
    ]

    summary = bd.compute_summary(decider_results, bedrock_results, items, bd.DEFAULT_DECIDER_CFG)

    # only the non-held-out outcome counts toward the main table
    assert summary["per_question_type"]["ambiguity_BRxx"]["n"] == 1
    assert summary["per_question_type"]["ambiguity_BRxx"]["decider_accuracy"] == 1.0


def test_compute_summary_reports_held_out_accuracy_separately():
    items = [SINGLE_NOUL_ITEM, HELD_OUT_NOUL_ITEM]
    decider_results = [
        _decider_result(
            "ambiguity_br01_clear_not_ambiguous",
            "ambiguity_BRxx",
            [bd.Outcome("ambiguity_BR01", False, 0.9, {"true": 0.1, "false": 0.9}, "AUTO", False, True)],
        ),
        _decider_result(
            "ambiguity_br06_held_out",
            "ambiguity_BRxx",
            [bd.Outcome("ambiguity_BR01", True, 0.9, {"true": 0.9, "false": 0.1}, "AUTO", True, False)],
        ),
    ]
    bedrock_results = [
        _bedrock_result("ambiguity_br01_clear_not_ambiguous", "ambiguity_BRxx", []),
        _bedrock_result("ambiguity_br06_held_out", "ambiguity_BRxx", []),
    ]

    summary = bd.compute_summary(decider_results, bedrock_results, items, bd.DEFAULT_DECIDER_CFG)

    held_out = summary["held_out_per_question_type"]["ambiguity_BRxx"]
    assert held_out["n"] == 1
    assert held_out["decider_accuracy"] == 0.0


def test_render_markdown_summary_includes_held_out_section():
    items = [SINGLE_NOUL_ITEM, HELD_OUT_NOUL_ITEM]
    decider_results = [
        _decider_result(
            "ambiguity_br01_clear_not_ambiguous",
            "ambiguity_BRxx",
            [bd.Outcome("ambiguity_BR01", False, 0.9, {"true": 0.1, "false": 0.9}, "AUTO", False, True)],
        ),
        _decider_result(
            "ambiguity_br06_held_out",
            "ambiguity_BRxx",
            [bd.Outcome("ambiguity_BR01", True, 0.9, {"true": 0.9, "false": 0.1}, "AUTO", True, False)],
        ),
    ]
    bedrock_results = [
        _bedrock_result("ambiguity_br01_clear_not_ambiguous", "ambiguity_BRxx", []),
        _bedrock_result("ambiguity_br06_held_out", "ambiguity_BRxx", []),
    ]
    summary = bd.compute_summary(decider_results, bedrock_results, items, bd.DEFAULT_DECIDER_CFG)

    markdown = bd.render_markdown_summary(summary)

    assert "Held-out accuracy" in markdown
