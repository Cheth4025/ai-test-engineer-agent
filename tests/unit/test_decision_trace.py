"""Unit tests for ai_test_engineer.decision_trace.build_decision_trace --
the one formatter both ui.py and reporting.py use."""

from __future__ import annotations

from ai_test_engineer import decision_trace

DECIDER_CFG = {"choice_threshold": 0.75, "noul_upper": 0.80, "noul_lower": 0.40}


def _auto_record(checkpoint: str, question: str, answer: str, confidence: float, **extra) -> dict:
    return {
        "checkpoint": checkpoint,
        "decision_source": "DECIDER",
        "decision_status": "VALIDATED",
        "route": "AUTO",
        "selected_action": "Proceed",
        "per_question": [
            {
                "question": question,
                "answer": answer,
                "suggestion": answer,
                "confidence": confidence,
                "probabilities": {answer: confidence},
            }
        ],
        **extra,
    }


def _escalated_record(checkpoint: str, question: str, suggestion: str, confidence: float) -> dict:
    return {
        "checkpoint": checkpoint,
        "decision_source": "DECIDER",
        "decision_status": "VALIDATED",
        "route": "ESCALATED",
        "selected_action": "Confirm",
        "per_question": [
            {
                "question": question,
                "answer": None,
                "suggestion": suggestion,
                "confidence": confidence,
                "probabilities": {suggestion: confidence, "OTHER": 1 - confidence},
            }
        ],
    }


def _human_record(checkpoint: str, selected_action: str, *, status: str = "VALIDATED", rejection_reason=None) -> dict:
    return {
        "checkpoint": checkpoint,
        "decision_source": "HUMAN_ESCALATION",
        "decision_status": status,
        "route": "ESCALATED",
        "selected_action": selected_action,
        "rejection_reason": rejection_reason,
        "per_question": [],
    }


# ---------------------------------------------------------------------------
# Standalone AUTO/REUSED rows -- no pairing needed.
# ---------------------------------------------------------------------------


def test_auto_record_is_its_own_row_with_primary_question_confidence():
    record = _auto_record("COVERAGE", "coverage_action", "ACCEPT_COVERAGE", 0.97)
    rows = decision_trace.build_decision_trace([record], DECIDER_CFG)

    assert len(rows) == 1
    row = rows[0]
    assert row["checkpoint"] == "COVERAGE"
    assert row["decision_text"] == "ACCEPT_COVERAGE"
    assert row["confidence"] == 0.97
    assert row["threshold"] == 0.75  # choice_threshold, not noul_upper
    assert row["route"] == "AUTO"
    assert row["source"] == "DECIDER"
    assert row["override"] is False
    assert row["icon"] == "✅"


def test_reused_record_is_its_own_row():
    record = _auto_record("COVERAGE", "coverage_action", "ACCEPT_COVERAGE", 0.97, decision_source="REUSED")
    rows = decision_trace.build_decision_trace([record], DECIDER_CFG)
    assert len(rows) == 1
    assert rows[0]["source"] == "REUSED"


def test_noul_primary_question_threshold_is_noul_upper():
    """A hypothetical primary question whose probabilities are the noul
    {"true","false"} shape reads noul_upper, not choice_threshold."""
    record = {
        "checkpoint": "COMPLETION",
        "decision_source": "DECIDER",
        "decision_status": "VALIDATED",
        "route": "AUTO",
        "selected_action": "Proceed",
        "per_question": [
            {
                "question": "completion_action",
                "answer": "YES",
                "suggestion": "YES",
                "confidence": 0.9,
                "probabilities": {"true": 0.9, "false": 0.1},
            }
        ],
    }
    rows = decision_trace.build_decision_trace([record], DECIDER_CFG)
    assert rows[0]["threshold"] == 0.80


# ---------------------------------------------------------------------------
# Pairing: no stale "Awaiting human" once answered.
# ---------------------------------------------------------------------------


def test_escalated_then_human_pairs_into_one_row_not_two():
    decider = _escalated_record("COVERAGE", "coverage_action", "ACCEPT_COVERAGE", 0.55)
    human = _human_record("COVERAGE", "REMEDIATE_GAP")

    rows = decision_trace.build_decision_trace([decider, human], DECIDER_CFG)

    assert len(rows) == 1  # not 2 -- never a dangling escalation-moment row
    row = rows[0]
    assert "Awaiting human" not in row["decision_text"]
    assert row["decision_text"].startswith("REMEDIATE_GAP")
    assert row["confidence"] == 0.55  # the Decider's own confidence, preserved
    assert row["threshold"] == 0.75


def test_truly_unanswered_escalation_still_reads_awaiting_human():
    decider = _escalated_record("COVERAGE", "coverage_action", "ACCEPT_COVERAGE", 0.55)
    rows = decision_trace.build_decision_trace([decider], DECIDER_CFG)

    assert len(rows) == 1
    assert rows[0]["decision_text"] == "Awaiting human"
    assert rows[0]["icon"] == "⏳"


def test_rejected_then_retried_pick_both_pair_with_same_decider_suggestion():
    """Live-run bug scenario: a human's first pick is rejected by a
    deterministic guard, then a second pick on the SAME still-open
    escalation succeeds -- both resulting rows reference the one original
    Decider suggestion/confidence, and neither leaves a dangling
    'Awaiting human' row."""
    decider = _escalated_record("COVERAGE", "coverage_action", "REMEDIATE_GAP", 0.62)
    rejected = _human_record("COVERAGE", "ACCEPT_COVERAGE", status="REJECTED", rejection_reason="5/5 required")
    retried = _human_record("COVERAGE", "REMEDIATE_GAP")

    rows = decision_trace.build_decision_trace([decider, rejected, retried], DECIDER_CFG)

    assert len(rows) == 2
    assert rows[0]["decision_text"] == "Rejected by guard: 5/5 required"
    assert rows[0]["confidence"] == 0.62  # same original Decider confidence
    assert rows[1]["decision_text"].startswith("REMEDIATE_GAP")
    assert rows[1]["confidence"] == 0.62
    assert not any("Awaiting human" in r["decision_text"] for r in rows)


# ---------------------------------------------------------------------------
# Override marking.
# ---------------------------------------------------------------------------


def test_human_override_of_decider_suggestion_is_marked():
    decider = _escalated_record("COVERAGE", "coverage_action", "ACCEPT_COVERAGE", 0.55)
    human = _human_record("COVERAGE", "REMEDIATE_GAP")  # differs from the Decider's own suggestion

    rows = decision_trace.build_decision_trace([decider, human], DECIDER_CFG)

    assert rows[0]["override"] is True
    assert "override" in rows[0]["decision_text"]
    assert "ACCEPT_COVERAGE" in rows[0]["decision_text"]  # names what was overridden


def test_human_agreeing_with_decider_suggestion_is_not_marked_override():
    decider = _escalated_record("COVERAGE", "coverage_action", "REMEDIATE_GAP", 0.55)
    human = _human_record("COVERAGE", "REMEDIATE_GAP")  # same as the Decider's own suggestion

    rows = decision_trace.build_decision_trace([decider, human], DECIDER_CFG)

    assert rows[0]["override"] is False
    assert "override" not in rows[0]["decision_text"]


def test_rejected_pick_is_never_marked_as_an_override():
    decider = _escalated_record("COVERAGE", "coverage_action", "REMEDIATE_GAP", 0.62)
    rejected = _human_record("COVERAGE", "ACCEPT_COVERAGE", status="REJECTED", rejection_reason="5/5 required")

    rows = decision_trace.build_decision_trace([decider, rejected], DECIDER_CFG)

    assert rows[0]["override"] is False


# ---------------------------------------------------------------------------
# Multiple independent checkpoints interleave correctly.
# ---------------------------------------------------------------------------


def test_independent_checkpoints_do_not_cross_pair():
    strategy_auto = _auto_record("AMBIGUITY+STRATEGY", "strategy_action", "GENERATE_TESTS", 0.95)
    coverage_escalate = _escalated_record("COVERAGE", "coverage_action", "ACCEPT_COVERAGE", 0.55)
    coverage_human = _human_record("COVERAGE", "ACCEPT_COVERAGE")

    rows = decision_trace.build_decision_trace([strategy_auto, coverage_escalate, coverage_human], DECIDER_CFG)

    assert len(rows) == 2
    assert rows[0]["checkpoint"] == "AMBIGUITY+STRATEGY"
    assert rows[1]["checkpoint"] == "COVERAGE"
    assert rows[1]["decision_text"].startswith("ACCEPT_COVERAGE")


def test_empty_decisions_list():
    assert decision_trace.build_decision_trace([], DECIDER_CFG) == []


def test_round4_and_round4_probabilities():
    assert decision_trace.round4(0.41879999999999995) == 0.4188
    assert decision_trace.round4(None) is None
    assert decision_trace.round4_probabilities({"a": 0.123456}) == {"a": 0.1235}
