"""The one decision-trace formatter both ui.py (Scenarios/Results pages) and
reporting.py (report.md) use to turn decisions.json records into display
rows -- so the two surfaces can never silently drift apart. No Streamlit
import here; ui.py imports these names, reporting.py imports the same ones.

Three things the naive "one row per decisions.json record" approach got
wrong, all fixed by build_decision_trace:

1. A dangling "Awaiting human" row. An escalated DECIDER record and the
   HUMAN_ESCALATION record that later resolves it are two separate
   decisions.json entries; showing both as independent rows leaves the
   first one permanently reading "Awaiting human" even after the human
   answered. build_decision_trace pairs them into one row instead.

2. Confidence/threshold read from the wrong question. decisions.json's
   top-level "confidence"/"threshold" belong to whichever verdict escalated
   first in dict order -- for COVERAGE that can be a grounding noul, not
   the coverage_action choice a human actually sees and picks. Every row
   here reads confidence from the PRIMARY CHOICE question's own per_question
   entry, and picks the matching threshold (choice_threshold for a choice
   question, noul_upper for a noul) rather than assuming one or the other.

3. A human override wasn't visible. When a human's final pick differs from
   the Decider's own suggestion for that escalation, the row says so.
"""

from __future__ import annotations

# Every checkpoint in this codebase names its human-facing bounded choice
# question "<noun>_action" -- the one question whose real answer is the
# decision, as opposed to the gate's own internal action name ("Confirm"/
# "Proceed") that selected_action carries for a DECIDER/REUSED record.
PRIMARY_CHOICE_QUESTIONS = {"strategy_action", "coverage_action", "failure_action", "completion_action"}


def round4(value):
    """Confidence/probability values round-trip through JSON as raw floats
    (e.g. 0.41879999999999995) -- rounds for display only, never for
    comparison/storage. Non-numeric/None values pass through unchanged."""
    return round(value, 4) if isinstance(value, (int, float)) else value


def round4_probabilities(probabilities: dict | None) -> dict:
    return {key: round4(value) for key, value in (probabilities or {}).items()}


def _primary_question(record: dict) -> dict | None:
    for question in record.get("per_question") or []:
        if question.get("question") in PRIMARY_CHOICE_QUESTIONS:
            return question
    return None


def _question_type(question_fields: dict) -> str:
    """decider_gate's ConfidenceGate always builds a noul's probabilities as
    exactly {"true": p, "false": 1-p} (gate.py); a choice question's
    probabilities keys are its own criteria names, which never collide with
    that exact pair for any checkpoint in this codebase."""
    probabilities = question_fields.get("probabilities") or {}
    return "noul" if set(probabilities) == {"true", "false"} else "choice"


def primary_choice_confidence(record: dict) -> float | None:
    """The primary choice question's OWN confidence (per_question), not the
    record's top-level confidence -- see module docstring point 2."""
    question = _primary_question(record)
    return question.get("confidence") if question else None


def _threshold_for(question_fields: dict | None, decider_cfg: dict) -> float | None:
    if question_fields is None:
        return None
    key = "choice_threshold" if _question_type(question_fields) == "choice" else "noul_upper"
    return decider_cfg.get(key)


def decider_decision_value(record: dict) -> str:
    """The real bounded-action choice (e.g. ACCEPT_COVERAGE/REMEDIATE_GAP)
    for one decisions.json record, as the Decision line should read it --
    never the gate's own internal "Confirm" action name, and never a
    normal-looking choice for a pick a guard actually rejected.

    - decision_status == "REJECTED": "Rejected by guard: <reason>".
    - decision_source == "HUMAN_ESCALATION": the human's literal choice
      (selected_action already IS that string) with " (human)" appended.
    - DECIDER/REUSED whose primary choice question is still unanswered
      (per_question's "answer" is None -- the escalation-moment record,
      where selected_action is just the gate's "Confirm" action name, not
      a real choice): "Awaiting human", not "Confirm".
    - DECIDER/REUSED with an answered primary choice (resolved AUTO, no
      escalation needed for that question even though the batch escalated
      overall): that real answer.
    """
    if record.get("decision_status") == "REJECTED":
        return f"Rejected by guard: {record.get('rejection_reason') or 'deterministic guard'}"
    if record.get("decision_source") == "HUMAN_ESCALATION":
        return f"{record.get('selected_action') or '—'} (human)"
    for question in record.get("per_question") or []:
        if question.get("question") in PRIMARY_CHOICE_QUESTIONS and question.get("answer") is not None:
            return question["answer"]
    return "Awaiting human"


def decision_icon(record: dict) -> str:
    """⏳ while a decision is still awaiting a human, ✅ once it's answered
    or auto-resolved, ⛔ when a human's answer was rejected by a guard --
    derived from decider_decision_value's own classification (never a
    second, independently-drifting set of rules for the same record)."""
    value = decider_decision_value(record)
    if value.startswith("Rejected by guard"):
        return "⛔"
    if value == "Awaiting human":
        return "⏳"
    return "✅"


def build_decision_trace(decisions: list[dict], decider_cfg: dict) -> list[dict]:
    """One display row per checkpoint occurrence.

    An AUTO or REUSED record stands alone. A DECIDER record that escalated
    is paired with the human record(s) that resolve it: a REJECTED human
    pick keeps the SAME pairing open for the next pick (mirrors
    checkpoints.apply_human_decision's REJECTED_RETRY, which leaves
    pending_decision untouched), so a retried escalation still shows the
    original Decider suggestion/confidence alongside each attempt. A
    DECIDER record that never got a human answer within this decisions.json
    (the run is still genuinely paused) is the only case that still reads
    "Awaiting human" -- never a stale one left behind after it WAS answered.

    Returns one dict per row: checkpoint, icon, decision_text (already
    carries the "(human)"/"Rejected by guard"/override wording -- callers
    render it as-is, no further interpretation), confidence, threshold,
    route, source, override (bool).
    """
    rows: list[dict] = []
    pending: dict[str, dict] = {}

    def _emit(decider_record: dict | None, human_record: dict | None) -> None:
        base_record = human_record or decider_record
        primary = _primary_question(decider_record) if decider_record else None
        decision_text = decider_decision_value(base_record)
        override = False
        if (
            decider_record is not None
            and human_record is not None
            and human_record.get("decision_status") != "REJECTED"
            and primary is not None
        ):
            suggestion = primary.get("suggestion")
            human_choice = human_record.get("selected_action")
            if suggestion is not None and human_choice is not None and suggestion != human_choice:
                override = True
                decision_text = f"{decision_text}, override of Decider's {suggestion!r} suggestion"
        rows.append(
            {
                "checkpoint": base_record.get("checkpoint"),
                "icon": decision_icon(base_record),
                "decision_text": decision_text,
                "confidence": round4(primary_choice_confidence(decider_record)) if decider_record else None,
                "threshold": round4(_threshold_for(primary, decider_cfg)) if decider_record else None,
                "route": base_record.get("route"),
                "source": base_record.get("decision_source"),
                "override": override,
            }
        )

    for record in decisions:
        checkpoint = record.get("checkpoint")
        source = record.get("decision_source")
        route = record.get("route")
        if source == "HUMAN_ESCALATION":
            decider_record = pending.get(checkpoint)
            _emit(decider_record, record)
            if record.get("decision_status") != "REJECTED":
                pending.pop(checkpoint, None)
            continue
        if source == "DECIDER" and route == "ESCALATED":
            pending[checkpoint] = record
            continue
        _emit(record, None)  # AUTO or REUSED -- standalone resolved row

    for decider_record in pending.values():
        _emit(decider_record, None)  # genuinely still awaiting human

    return rows
