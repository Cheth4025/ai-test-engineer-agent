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
    assert row["outcome"] == decision_trace.OUTCOME_AUTO


def test_reused_record_is_its_own_row():
    record = _auto_record("COVERAGE", "coverage_action", "ACCEPT_COVERAGE", 0.97, decision_source="REUSED")
    rows = decision_trace.build_decision_trace([record], DECIDER_CFG)
    assert len(rows) == 1
    assert rows[0]["source"] == "REUSED"
    assert rows[0]["outcome"] == decision_trace.OUTCOME_AUTO


def test_reused_record_of_an_already_answered_checkpoint_does_not_add_a_stale_waiting_row():
    """polish-2 D10 regression, real shape from runs/20261018T212639Z_
    d5b23d26/decisions.json: a DECIDER+HUMAN_ESCALATION pair for
    AMBIGUITY+STRATEGY resolves cleanly (one row, not waiting) -- then a
    later REUSED record for the SAME checkpoint (the controller replaying
    a cached decision for a retried generate_scenarios tool call) arrives
    with route still ESCALATED and every per_question answer still null
    (decision_record_reused() re-evaluates the gate's verdict fresh, which
    still reports ESCALATE -- the human's resolution lives in the
    HUMAN_ESCALATION record two entries back, not in this one). Before the
    fix, this REUSED record fell through to the standalone-row branch,
    whose decider_decision_value() found no answered primary question and
    returned "Awaiting human" -- a second, stale ⏳ row for a checkpoint
    that was, in fact, already answered."""
    _COMMON_PER_QUESTION = [
        {"question": "ambiguity_BR01", "answer": None, "suggestion": True, "confidence": 0.6808, "probabilities": {"true": 0.6808, "false": 0.3192}, "route": "ESCALATE"},
        {"question": "strategy_action", "answer": None, "suggestion": "REQUEST_CLARIFICATION", "confidence": 0.4283, "probabilities": {"GENERATE_TESTS": 0.2859, "REQUEST_CLARIFICATION": 0.7141}, "route": "ESCALATE"},
    ]
    decider_record = {
        "checkpoint": "AMBIGUITY+STRATEGY",
        "selected_action": "Confirm",
        "confidence": 0.6808,
        "route": "ESCALATED",
        "decision_source": "DECIDER",
        "decision_status": "VALIDATED",
        "per_question": _COMMON_PER_QUESTION,
    }
    human_record = {
        "checkpoint": "AMBIGUITY+STRATEGY",
        "selected_action": "GENERATE_TESTS",
        "route": "ESCALATED",
        "decision_source": "HUMAN_ESCALATION",
        "decision_status": "VALIDATED",
        "per_question": _COMMON_PER_QUESTION,
    }
    reused_record = {
        "checkpoint": "AMBIGUITY+STRATEGY",
        "selected_action": "Proceed",
        "confidence": 0.6808,
        "route": "ESCALATED",
        "decision_source": "REUSED",
        "decision_status": "VALIDATED",
        "per_question": _COMMON_PER_QUESTION,
        "reused_from_tool_use_id": "tooluse_5La7DlWZcTshkxW1queZE8",
    }

    rows = decision_trace.build_decision_trace([decider_record, human_record, reused_record], DECIDER_CFG)

    ambiguity_rows = [row for row in rows if row["checkpoint"] == "AMBIGUITY+STRATEGY"]
    assert len(ambiguity_rows) == 1
    assert ambiguity_rows[0]["icon"] == "✅"
    assert ambiguity_rows[0]["outcome"] != decision_trace.OUTCOME_WAITING
    # The human's pick differs from the Decider's own suggestion
    # (REQUEST_CLARIFICATION) -- real pre-existing behavior (now in
    # "detail", not decision_text -- group D12), not part of this
    # regression.
    assert ambiguity_rows[0]["decision_text"] == "GENERATE_TESTS (human)"
    assert ambiguity_rows[0]["override"] is True
    assert "REQUEST_CLARIFICATION" in ambiguity_rows[0]["detail"]


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
    assert rows[0]["outcome"] == decision_trace.OUTCOME_WAITING


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
    # outcome is a fixed, stable literal -- the dynamic reason stays in
    # decision_text only (asserted above), never duplicated into outcome.
    assert rows[0]["outcome"] == decision_trace.OUTCOME_REJECTED_BY_GUARD
    assert rows[1]["decision_text"].startswith("REMEDIATE_GAP")
    assert rows[1]["confidence"] == 0.62
    assert rows[1]["outcome"] == decision_trace.OUTCOME_HUMAN_CONFIRMED
    assert not any("Awaiting human" in r["decision_text"] for r in rows)


# ---------------------------------------------------------------------------
# Override marking.
# ---------------------------------------------------------------------------


def test_human_override_of_decider_suggestion_is_marked():
    decider = _escalated_record("COVERAGE", "coverage_action", "ACCEPT_COVERAGE", 0.55)
    human = _human_record("COVERAGE", "REMEDIATE_GAP")  # differs from the Decider's own suggestion

    rows = decision_trace.build_decision_trace([decider, human], DECIDER_CFG)

    assert rows[0]["override"] is True
    # polish-2 group D12: decision_text stays short (action + "(human)");
    # the override explanation moved to its own "detail" field (the
    # Outcome pill's tooltip/sub-text in the UI, a parenthetical in
    # report.md), not folded into decision_text as a long sentence.
    assert rows[0]["decision_text"] == "REMEDIATE_GAP (human)"
    assert "override" in rows[0]["detail"]
    assert "ACCEPT_COVERAGE" in rows[0]["detail"]  # names what was overridden
    assert rows[0]["outcome"] == decision_trace.OUTCOME_HUMAN_OVERRIDE


def test_human_agreeing_with_decider_suggestion_is_not_marked_override():
    decider = _escalated_record("COVERAGE", "coverage_action", "REMEDIATE_GAP", 0.55)
    human = _human_record("COVERAGE", "REMEDIATE_GAP")  # same as the Decider's own suggestion

    rows = decision_trace.build_decision_trace([decider, human], DECIDER_CFG)

    assert rows[0]["override"] is False
    assert rows[0]["detail"] is None
    assert rows[0]["outcome"] == decision_trace.OUTCOME_HUMAN_CONFIRMED


def test_rejected_pick_is_never_marked_as_an_override():
    decider = _escalated_record("COVERAGE", "coverage_action", "REMEDIATE_GAP", 0.62)
    rejected = _human_record("COVERAGE", "ACCEPT_COVERAGE", status="REJECTED", rejection_reason="5/5 required")

    rows = decision_trace.build_decision_trace([decider, rejected], DECIDER_CFG)

    assert rows[0]["override"] is False
    assert rows[0]["outcome"] == decision_trace.OUTCOME_REJECTED_BY_GUARD


def test_rejected_outcome_is_a_fixed_literal_regardless_of_the_real_reason():
    """outcome must stay a small, stable pill-ready vocabulary -- the
    dynamic rejection_reason text lives only in decision_text, never
    embedded into outcome (two different real reasons still produce the
    identical outcome literal)."""
    for reason in ("5/5 required", "a completely different guard reason"):
        decider = _escalated_record("COVERAGE", "coverage_action", "REMEDIATE_GAP", 0.62)
        rejected = _human_record("COVERAGE", "ACCEPT_COVERAGE", status="REJECTED", rejection_reason=reason)
        rows = decision_trace.build_decision_trace([decider, rejected], DECIDER_CFG)
        assert rows[0]["outcome"] == decision_trace.OUTCOME_REJECTED_BY_GUARD
        assert reason in rows[0]["decision_text"]


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


# ---------------------------------------------------------------------------
# "escalated by rule-match/ambiguity questions" -- a DECIDER record whose
# PRIMARY choice question resolved AUTO on its own (a real answer, not
# "Awaiting human") but the record's own route is still ESCALATED, because
# some OTHER question in the same batch (a grounding noul, an ambiguity
# noul) needed a human -- resolved via a different control entirely (e.g.
# rule-match resolution), never a HUMAN_ESCALATION record for this
# checkpoint. Outcome stays Auto (no human answered THIS question); the
# prose decision_text gets the explanatory suffix.
# ---------------------------------------------------------------------------


def _escalated_but_primary_resolved_auto(checkpoint: str, question: str, answer: str, confidence: float) -> dict:
    return {
        "checkpoint": checkpoint,
        "decision_source": "DECIDER",
        "decision_status": "VALIDATED",
        "route": "ESCALATED",  # the BATCH escalated (a grounding noul, say)
        "selected_action": "Confirm",
        "per_question": [
            {
                "question": question,
                "answer": answer,  # the PRIMARY question resolved on its own
                "suggestion": answer,
                "confidence": confidence,
                "probabilities": {answer: confidence},
            },
            {
                "question": "grounding_S03",
                "answer": None,
                "suggestion": "true",
                "confidence": 0.5,
                "probabilities": {"true": 0.5, "false": 0.5},
            },
        ],
    }


def test_escalated_by_other_question_suffix_when_primary_resolved_auto_but_batch_escalated():
    record = _escalated_but_primary_resolved_auto("COVERAGE", "coverage_action", "ACCEPT_COVERAGE", 0.97)
    rows = decision_trace.build_decision_trace([record], DECIDER_CFG)

    assert len(rows) == 1
    row = rows[0]
    # polish-2 group D12: moved from a decision_text suffix to its own
    # "detail" field, same as the override case.
    assert row["decision_text"] == "ACCEPT_COVERAGE"
    assert row["detail"] == "escalated by rule-match/ambiguity questions"
    assert row["outcome"] == decision_trace.OUTCOME_AUTO
    assert row["icon"] == "✅"


def test_escalated_suffix_absent_when_primary_itself_is_the_one_awaiting_human():
    # Contrast case: primary unanswered (route ESCALATED because of the
    # primary question itself) -- plain "Awaiting human", no suffix.
    record = _escalated_record("COVERAGE", "coverage_action", "ACCEPT_COVERAGE", 0.55)
    rows = decision_trace.build_decision_trace([record], DECIDER_CFG)

    assert rows[0]["decision_text"] == "Awaiting human"
    assert "escalated by" not in rows[0]["decision_text"]


def test_escalated_suffix_absent_for_a_plain_auto_record():
    record = _auto_record("COVERAGE", "coverage_action", "ACCEPT_COVERAGE", 0.97)  # route AUTO, not ESCALATED
    rows = decision_trace.build_decision_trace([record], DECIDER_CFG)
    assert "escalated by" not in rows[0]["decision_text"]


def test_round4_and_round4_probabilities():
    assert decision_trace.round4(0.41879999999999995) == 0.4188
    assert decision_trace.round4(None) is None
    assert decision_trace.round4_probabilities({"a": 0.123456}) == {"a": 0.1235}


# ---------------------------------------------------------------------------
# pad_not_reached_rows -- UI-only (the Analysis page's live trace): appends
# a greyed "not reached" row for every checkpoint absent from the real
# rows, in ALL_CHECKPOINTS order. report.md/Scenarios/Results never call
# this -- a finished run's decisions.json already has an entry for every
# checkpoint it reached.
# ---------------------------------------------------------------------------


def test_all_checkpoints_matches_the_real_checkpoint_names():
    # checkpoints._checkpoint_name's own tool->checkpoint mapping, copied
    # here as a literal set to confirm ALL_CHECKPOINTS can't silently drift.
    assert decision_trace.ALL_CHECKPOINTS == ["AMBIGUITY+STRATEGY", "COVERAGE", "FAILURE", "COMPLETION"]


def test_pad_not_reached_rows_is_noop_when_all_checkpoints_present():
    rows = [
        {"checkpoint": cp, "icon": "✅", "decision_text": "x", "confidence": None, "threshold": None,
         "route": "AUTO", "source": "DECIDER", "override": False, "outcome": decision_trace.OUTCOME_AUTO}
        for cp in decision_trace.ALL_CHECKPOINTS
    ]
    assert decision_trace.pad_not_reached_rows(list(rows)) == rows


def test_pad_not_reached_rows_appends_missing_checkpoints_in_order():
    rows = [
        {"checkpoint": "AMBIGUITY+STRATEGY", "icon": "✅", "decision_text": "x", "confidence": None,
         "threshold": None, "route": "AUTO", "source": "DECIDER", "override": False,
         "outcome": decision_trace.OUTCOME_AUTO},
        {"checkpoint": "COVERAGE", "icon": "⏳", "decision_text": "Awaiting human", "confidence": 0.69,
         "threshold": 0.75, "route": "ESCALATED", "source": "DECIDER", "override": False,
         "outcome": decision_trace.OUTCOME_WAITING},
    ]
    padded = decision_trace.pad_not_reached_rows(list(rows))

    assert [row["checkpoint"] for row in padded] == ["AMBIGUITY+STRATEGY", "COVERAGE", "FAILURE", "COMPLETION"]
    assert padded[0] == rows[0]
    assert padded[1] == rows[1]
    for row in padded[2:]:
        assert row["decision_text"] == "not reached"
        assert row["confidence"] is None
        assert row["threshold"] is None
        assert row["outcome"] == "not reached"


def test_pad_not_reached_rows_empty_input_appends_all_four():
    padded = decision_trace.pad_not_reached_rows([])
    assert [row["checkpoint"] for row in padded] == decision_trace.ALL_CHECKPOINTS
    assert all(row["decision_text"] == "not reached" for row in padded)
    assert all(row["outcome"] == "not reached" for row in padded)


# ---------------------------------------------------------------------------
# label_decision_trace() -- polish-2 group D11: friendly checkpoint names,
# shared by both renderers (ui.py and reporting.py) so they can't drift.
# ---------------------------------------------------------------------------


def _plain_row(checkpoint: str) -> dict:
    return {
        "checkpoint": checkpoint, "icon": "✅", "decision_text": "x", "detail": None,
        "confidence": None, "threshold": None, "route": "AUTO", "source": "DECIDER",
        "override": False, "outcome": decision_trace.OUTCOME_AUTO,
    }


def test_label_decision_trace_friendly_base_names():
    rows = [_plain_row(cp) for cp in decision_trace.ALL_CHECKPOINTS]
    labeled = decision_trace.label_decision_trace(rows)
    assert [row["label"] for row in labeled] == ["Generate", "Coverage v1", "Failure", "Completion"]


def test_label_decision_trace_second_coverage_occurrence_is_v2():
    # COVERAGE can escalate twice in one run (pre- and post-remediation).
    rows = [_plain_row("COVERAGE"), _plain_row("COVERAGE")]
    labeled = decision_trace.label_decision_trace(rows)
    assert [row["label"] for row in labeled] == ["Coverage v1", "Coverage v2"]


def test_label_decision_trace_unknown_checkpoint_passes_through_unchanged():
    # "Approval" (ui.py's synthetic row) and anything else not in
    # _FRIENDLY_BASE -- never raises, just keeps its own name.
    rows = [_plain_row("Approval")]
    labeled = decision_trace.label_decision_trace(rows)
    assert labeled[0]["label"] == "Approval"


def test_label_decision_trace_never_mutates_the_input_rows():
    rows = [_plain_row("COVERAGE")]
    decision_trace.label_decision_trace(rows)
    assert "label" not in rows[0]
