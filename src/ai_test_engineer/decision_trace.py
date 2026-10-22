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


OUTCOME_AUTO = "Auto"
OUTCOME_HUMAN_CONFIRMED = "Human confirmed"
OUTCOME_HUMAN_OVERRIDE = "Human override"
OUTCOME_WAITING = "⏳ Waiting"
# A fixed literal, not a dynamic f-string: the real rejection reason lives
# only in decision_text (decider_decision_value's own "Rejected by guard:
# <reason>" string), never duplicated into outcome -- outcome stays a
# small, stable, pill-ready vocabulary a renderer can match by exact
# lookup, with no prefix-matching glue needed for a value that could
# otherwise carry arbitrary runtime text. "not reached" is produced only
# by pad_not_reached_rows, never by _outcome_for itself.
OUTCOME_REJECTED_BY_GUARD = "Rejected by guard"


def _outcome_for(base_record: dict, human_record: dict | None, override: bool, *, still_waiting: bool) -> str:
    """The short pill-ready outcome literal for one row -- a separate,
    stable vocabulary from decision_text (which stays free-form prose: the
    override suffix, the "(human)" marker, the escalated-by-other-question
    suffix, the real rejection reason).

    still_waiting is computed by the caller (_emit), which already knows
    structurally whether this row is the genuinely-still-paused case --
    re-deriving it here via a string match against decider_decision_value's
    "Awaiting human" prose would couple this stable enum to wording that's
    free to change independently."""
    if base_record.get("decision_status") == "REJECTED":
        return OUTCOME_REJECTED_BY_GUARD
    if still_waiting:
        return OUTCOME_WAITING
    if human_record is not None:
        return OUTCOME_HUMAN_OVERRIDE if override else OUTCOME_HUMAN_CONFIRMED
    return OUTCOME_AUTO  # AUTO/REUSED standalone, or a DECIDER record whose
    # own primary question resolved automatically even though the batch
    # escalated overall (see _emit's "escalated by other question" suffix)


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
    carries the "(human)"/"Rejected by guard"/override/escalated-by-other-
    question wording -- callers render it as-is, no further
    interpretation), confidence, threshold, route, source, override (bool),
    outcome (a short, stable pill-ready literal -- see OUTCOME_* and
    _outcome_for).
    """
    rows: list[dict] = []
    pending: dict[str, dict] = {}
    resolved_checkpoints: set[str] = set()

    def _emit(decider_record: dict | None, human_record: dict | None) -> None:
        base_record = human_record or decider_record
        primary = _primary_question(decider_record) if decider_record else None
        decision_text = decider_decision_value(base_record)
        override = False
        # detail (polish-2 group D12): the override/escalated-by-other-
        # question explanation, kept OUT of decision_text so the Decision
        # column stays short (action + "(human)") -- callers render this
        # as the Outcome pill's tooltip/sub-text (UI) or a parenthetical
        # next to Outcome (report.md, which has no tooltips).
        detail = None
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
                detail = f"override of Decider's {suggestion!r} suggestion"
        # A DECIDER record whose own PRIMARY question resolved AUTO (a real
        # answer, not "Awaiting human") yet whose overall route is still
        # ESCALATED: some OTHER question in the batch (a grounding/ambiguity
        # noul) needed a human, resolved via a different control entirely
        # (e.g. rule-match resolution) -- never a HUMAN_ESCALATION record
        # for THIS checkpoint, so human_record is None here.
        if (
            decider_record is not None
            and human_record is None
            and primary is not None
            and primary.get("answer") is not None
            and base_record.get("route") == "ESCALATED"
        ):
            detail = "escalated by rule-match/ambiguity questions"
        # Genuinely still paused on this exact question: a decider_record
        # with no pairing human answer, whose own primary choice question
        # is itself unanswered (as opposed to the escalated-by-other-
        # question case just above, where primary DID answer). Computed
        # here, not re-derived from decider_decision_value's prose, so
        # _outcome_for's WAITING classification can never drift from what
        # this function already knows structurally about the record.
        still_waiting = decider_record is not None and human_record is None and (
            primary is None or primary.get("answer") is None
        )
        rows.append(
            {
                "checkpoint": base_record.get("checkpoint"),
                "icon": decision_icon(base_record),
                "decision_text": decision_text,
                "detail": detail,
                "confidence": round4(primary_choice_confidence(decider_record)) if decider_record else None,
                "threshold": round4(_threshold_for(primary, decider_cfg)) if decider_record else None,
                "route": base_record.get("route"),
                "source": base_record.get("decision_source"),
                "override": override,
                "outcome": _outcome_for(base_record, human_record, override, still_waiting=still_waiting),
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
                resolved_checkpoints.add(checkpoint)
            continue
        if source == "DECIDER" and route == "ESCALATED":
            pending[checkpoint] = record
            continue
        if source == "REUSED" and route == "ESCALATED" and checkpoint in resolved_checkpoints:
            # The controller replayed a cached decision for a retried tool
            # call after this checkpoint's own DECIDER+HUMAN_ESCALATION
            # pair already produced its one row above (decision_record_
            # reused() re-evaluates the gate fresh, which still reports
            # ESCALATE/null answers even though a human already resolved
            # this checkpoint via a prior toolUseId) -- not a new
            # occurrence, so no second row.
            continue
        _emit(record, None)  # AUTO or REUSED -- standalone resolved row
        resolved_checkpoints.add(checkpoint)

    for decider_record in pending.values():
        _emit(decider_record, None)  # genuinely still awaiting human

    return rows


# checkpoints._checkpoint_name's tool->checkpoint mapping, in the fixed
# pipeline order the Analysis page's live trace shows them.
ALL_CHECKPOINTS = ["AMBIGUITY+STRATEGY", "COVERAGE", "FAILURE", "COMPLETION"]


# Friendly checkpoint names (polish-2 group D11) -- display only, never
# used for matching/comparison (that stays "checkpoint", the real enum
# string from decisions.json).
_FRIENDLY_BASE = {
    "AMBIGUITY+STRATEGY": "Generate",
    "COVERAGE": "Coverage",
    "FAILURE": "Failure",
    "COMPLETION": "Completion",
}


def label_decision_trace(rows: list[dict]) -> list[dict]:
    """Adds a "label" field (never mutates the input rows) -- the
    friendly checkpoint name from _FRIENDLY_BASE, with a "v{occurrence}"
    suffix for a checkpoint that appears more than once in the same rows
    list (COVERAGE can escalate twice: once pre-remediation, once after).
    Anything not in _FRIENDLY_BASE (ui.py's synthetic "Approval" row,
    or any future checkpoint) passes through with its own name as the
    label. Shared by both renderers (ui.py and reporting.py) so the
    wording can't drift between them."""
    occurrence_counts: dict[str, int] = {}
    labeled = []
    for row in rows:
        checkpoint = row["checkpoint"]
        base = _FRIENDLY_BASE.get(checkpoint, checkpoint)
        if checkpoint == "COVERAGE":
            # Always versioned, even for a single occurrence -- COVERAGE
            # checks a scenario-set version (same "v1"/"v2" terminology
            # the Scenarios page already uses for scenario sets), not a
            # one-shot step like the other three checkpoints.
            occurrence_counts[checkpoint] = occurrence_counts.get(checkpoint, 0) + 1
            label = f"{base} v{occurrence_counts[checkpoint]}"
        else:
            label = base
        labeled.append({**row, "label": label})
    return labeled


def pad_not_reached_rows(rows: list[dict]) -> list[dict]:
    """UI-only (the Analysis page's live decision trace): appends a greyed
    "not reached" row for every checkpoint in ALL_CHECKPOINTS absent from
    rows, in ALL_CHECKPOINTS order, after the real rows. report.md and the
    Scenarios/Results pages never call this -- a finished run's
    decisions.json already has an entry for every checkpoint it reached,
    and report.md documents only what happened, never padded placeholders."""
    seen = {row["checkpoint"] for row in rows}
    return rows + [
        {
            "checkpoint": checkpoint,
            "icon": "⬜",
            "decision_text": "not reached",
            "detail": None,
            "confidence": None,
            "threshold": None,
            "route": None,
            "source": None,
            "override": False,
            "outcome": "not reached",
        }
        for checkpoint in ALL_CHECKPOINTS
        if checkpoint not in seen
    ]
