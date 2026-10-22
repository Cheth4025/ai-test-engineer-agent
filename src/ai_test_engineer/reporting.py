"""Final report.md + report_metadata.json (F09, docs/05 Section 14).

Reads only already-recorded run evidence off disk -- never invents a value,
never calls Bedrock/Decider, never reruns anything. A missing file makes its
own report section say so (NOT RUN/unavailable) rather than being silently
omitted, per F09's acceptance criteria. Called only from
checkpoints.build_completion_checkpoint's resolve()/apply_human_decision,
after the COMPLETION readiness guard has already passed -- never on its own.

The decision trace is built by ai_test_engineer.decision_trace.
build_decision_trace -- the SAME function ui.py's Results/Scenarios pages
call -- so report.md and the UI can never show a different trace for the
same run (see tests/unit/test_reporting.py's report-vs-UI-row test).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_test_engineer.bedrock import DEFAULT_MODEL_ID
from ai_test_engineer.decision_trace import build_decision_trace, label_decision_trace
from ai_test_engineer.state import RunController

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _resolve_host_label(decider_cfg: dict) -> str:
    """Same precedence as checkpoints.resolve_host_label, reimplemented
    locally (a one-line lookup) rather than importing checkpoints.py --
    keeps this module's own dependency footprint to "recorded evidence +
    static config", with no coupling to the Strands/DeciderGuardrail
    machinery that module also carries."""
    return os.environ.get("AITE_DECIDER_HOST_LABEL") or decider_cfg.get("host_label") or "unknown"


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
        f"**What happened:** {analysis['actual_behavior']}",
        "",
        f"**Rule violated:** {analysis['expected_behavior']}",
        "",
        "**Impact:**",
        *[f"- {item}" for item in analysis.get("impact", [])],
        "",
        f"**Evidence:** {', '.join(analysis.get('evidence_references', [])) or 'unavailable'}",
    ]
    return "\n".join(lines)


def _decision_trace_md(decisions: list[dict] | None, decider_cfg: dict) -> str:
    rows = label_decision_trace(build_decision_trace(decisions or [], decider_cfg))
    if not rows:
        return "No decisions recorded."
    header = "| Checkpoint | Decision | Confidence vs τ | Outcome | Route | Source |\n|---|---|---|---|---|---|"
    lines = []
    for row in rows:
        if row["confidence"] is not None and row["threshold"] is not None:
            conf_text = f"{row['confidence']:.2f} vs {row['threshold']:.2f}"
        else:
            conf_text = "—"
        # detail (polish-2 group D12): report.md has no tooltips, so the
        # override/escalated-by-other-question explanation goes in the
        # Outcome cell as a parenthetical instead of a long Decision
        # sentence.
        outcome_cell = f"{row['outcome']} ({row['detail']})" if row.get("detail") else row["outcome"]
        lines.append(
            f"| {row['label']} | {row['icon']} {row['decision_text']} | {conf_text} | "
            f"{outcome_cell} | {row['route']} | {row['source']} |"
        )
    return "\n".join([header, *lines])


def _approval_md(approval: dict | None) -> str:
    if not approval:
        return "NOT RUN -- no approval recorded."
    comment = f" Comment: {approval['comment']}" if approval.get("comment") else ""
    return f"{approval['decision']} by {approval['reviewer']} at {approval['timestamp']}.{comment}"


def _header_md(controller: RunController, requirement: dict, decider_cfg: dict) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    bedrock_model_id = os.environ.get("AITE_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)
    lines = [
        "# Assurance Agent Experience — AI Test Engineer Agent report",
        "",
        f"**Run ID:** {controller.run_id}",
        f"**Generated at (UTC):** {generated_at}",
        f"**Requirement ID:** {requirement.get('requirement_id', 'unknown')}",
        f"**Decider:** {_resolve_host_label(decider_cfg)} · {decider_cfg.get('model', 'unknown')}",
        f"**Bedrock model:** {bedrock_model_id}",
    ]
    return "\n".join(lines)


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
    requirement = _load(CONFIG_DIR / "requirement.json") or {}
    decider_cfg = _load(CONFIG_DIR / "decider.json") or {}

    verification_status = payment_verification_status(results)

    body = "\n\n".join(
        [
            _header_md(controller, requirement, decider_cfg),
            f"**Agent workflow:** COMPLETED  \n**Payment verification:** {verification_status}",
            "## Results",
            _results_table_md(results),
            "## T04 failure detail",
            _failure_detail_md(analysis),
            "## Decider decision trace",
            _decision_trace_md(decisions, decider_cfg),
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
