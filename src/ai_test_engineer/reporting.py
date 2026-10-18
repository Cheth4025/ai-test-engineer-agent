"""Final report.md + report_metadata.json (F09, docs/05 Section 14).

Reads only already-recorded run evidence off disk -- never invents a value,
never calls Bedrock/Decider, never reruns anything. A missing file makes its
own report section say so (NOT RUN/unavailable) rather than being silently
omitted, per F09's acceptance criteria. Called only from
checkpoints.build_completion_checkpoint's resolve()/apply_human_decision,
after the COMPLETION readiness guard has already passed -- never on its own.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ai_test_engineer.state import RunController

# Every checkpoint in this codebase names its human-facing bounded choice
# question "<noun>_action" -- the one question whose real answer is the
# decision, as opposed to the gate's own internal action name ("Confirm"/
# "Proceed") that selected_action carries for a DECIDER/REUSED record.
_PRIMARY_CHOICE_QUESTIONS = {"strategy_action", "coverage_action", "failure_action", "completion_action"}


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _decision_value(record: dict) -> str:
    """Same classification ui.py's decider_decision_value uses, generalized
    to every checkpoint's own "<noun>_action" question rather than only
    coverage_action -- this module never imports ui.py (a Streamlit-free
    backend module), so the small shared logic is duplicated, not reused."""
    if record.get("decision_status") == "REJECTED":
        return f"Rejected by guard: {record.get('rejection_reason') or 'deterministic guard'}"
    if record.get("decision_source") == "HUMAN_ESCALATION":
        return f"{record.get('selected_action') or '—'} (human)"
    for question in record.get("per_question") or []:
        if question.get("question") in _PRIMARY_CHOICE_QUESTIONS and question.get("answer") is not None:
            return question["answer"]
    return "Awaiting human"


def _round4(value: Any) -> Any:
    return round(value, 4) if isinstance(value, (int, float)) else value


def _results_table_md(results: dict | None) -> str:
    if not results:
        return "NOT RUN -- no execution results recorded."
    header = "| ID | Scenario | Rules | Expected | Actual | Status |\n|---|---|---|---|---|---|"
    rows = [
        f"| {t['test_id']} | {t['scenario_id']} | {', '.join(t['rule_ids'])} | {t['expected']} | "
        f"{t['actual']} | {t['status']} |"
        for t in results["tests"]
    ]
    return "\n".join([header, *rows])


def _failure_detail_md(analysis: dict | None) -> str:
    if not analysis or not analysis.get("has_failure"):
        return "No failure observed."
    lines = [
        f"**Rule violated:** {analysis['expected_behavior']}",
        "",
        "**Impact:**",
        *[f"- {item}" for item in analysis.get("impact", [])],
        "",
        f"**Evidence:** {', '.join(analysis.get('evidence_references', [])) or 'unavailable'}",
    ]
    return "\n".join(lines)


def _decision_trace_md(decisions: list[dict] | None) -> str:
    if not decisions:
        return "No decisions recorded."
    header = "| Checkpoint | Decision | Confidence vs τ | Route | Source |\n|---|---|---|---|---|"
    rows = []
    for record in decisions:
        confidence = _round4(record.get("confidence"))
        threshold = _round4(record.get("threshold"))
        conf_text = (
            f"{confidence if confidence is not None else '—'} vs "
            f"{threshold if threshold is not None else '—'}"
        )
        rows.append(
            f"| {record.get('checkpoint')} | {_decision_value(record)} | {conf_text} | "
            f"{record.get('route')} | {record.get('decision_source')} |"
        )
    return "\n".join([header, *rows])


def _approval_md(approval: dict | None) -> str:
    if not approval:
        return "NOT RUN -- no approval recorded."
    comment = f" Comment: {approval['comment']}" if approval.get("comment") else ""
    return f"{approval['decision']} by {approval['reviewer']} at {approval['timestamp']}.{comment}"


def payment_verification_status(results: dict | None) -> str:
    if not results:
        return "INCOMPLETE"
    statuses = {t["status"] for t in results["tests"]}
    return "PASSED" if statuses <= {"PASS"} else "FAILED"


def render_report(controller: RunController) -> dict:
    run_dir = controller.run_dir
    results = _load(run_dir / "results.json")
    decisions = _load(run_dir / "decisions.json") or []
    analysis = _load(run_dir / "analysis.json")
    approval = _load(run_dir / "approval.json")

    verification_status = payment_verification_status(results)

    body = "\n\n".join(
        [
            "# AI Test Engineer Agent -- Report",
            f"**Agent workflow:** COMPLETED  \n**Payment verification:** {verification_status}",
            "## Results",
            _results_table_md(results),
            "## T04 failure detail",
            _failure_detail_md(analysis),
            "## Decider decision trace",
            _decision_trace_md(decisions),
            "## Human approval",
            _approval_md(approval),
            "## Statement",
            "The agent reports defects; it does not change code or tests.",
        ]
    )
    report_path = run_dir / "report.md"
    report_path.write_text(body, encoding="utf-8")

    evidence_references = [
        ref
        for ref in ("results.json", "coverage_final.json", "analysis.json", "decisions.json", "approval.json")
        if (run_dir / ref).exists()
    ]
    metadata = {
        "agent_workflow_status": "COMPLETED",
        "payment_verification_status": verification_status,
        "report_filename": "report.md",
        "evidence_references": evidence_references,
        "stopping_reason": None,
        "stage_outcomes": {
            "EXECUTING": "COMPLETED" if results else "NOT_RUN",
            "ANALYZING_RESULTS": "COMPLETED" if analysis is not None else "NOT_RUN",
            "REPORTING": "COMPLETED",
        },
    }
    return {"report_path": str(report_path), "report_text": body, "metadata": metadata}
