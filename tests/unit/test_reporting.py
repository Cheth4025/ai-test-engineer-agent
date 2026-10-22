"""Unit tests for ai_test_engineer.reporting.render_report -- content only,
built from hand-written fixture files (no live run, no Bedrock/Decider)."""

from __future__ import annotations

import json
import re
from datetime import datetime

import pytest

from ai_test_engineer import decision_trace, reporting
from ai_test_engineer.state import RunController


@pytest.fixture
def controller(tmp_path) -> RunController:
    return RunController("run-1", tmp_path / "run-1")


def _write(controller: RunController, name: str, data) -> None:
    (controller.run_dir / name).write_text(json.dumps(data))


RESULTS_DEFECT_ON = {
    "execution_id": "run-1",
    "return_code": 1,
    "duration_ms": 412,
    "tests": [
        {"test_id": "T01", "scenario_id": "S01", "rule_ids": ["BR01"], "status": "PASS", "duration_ms": 3, "expected": "1 charge", "actual": "1 charge"},
        {"test_id": "T02", "scenario_id": "S02", "rule_ids": ["BR01", "BR02"], "status": "PASS", "duration_ms": 4, "expected": "1 charge", "actual": "1 charge"},
        {"test_id": "T03", "scenario_id": "S03", "rule_ids": ["BR01", "BR03"], "status": "PASS", "duration_ms": 2, "expected": "Conflict", "actual": "Conflict"},
        {"test_id": "T04", "scenario_id": "S04", "rule_ids": ["BR06"], "status": "FAIL", "duration_ms": 5, "expected": "1 ledger entry", "actual": "2 ledger entries"},
        {"test_id": "T05", "scenario_id": "S05", "rule_ids": ["BR01", "BR02"], "status": "PASS", "duration_ms": 6, "expected": "1 charge", "actual": "1 charge"},
    ],
}

ANALYSIS = {
    "has_failure": True,
    "test_id": "T04",
    "rule_ids": ["BR06"],
    "classification": "BUSINESS_RULE_VIOLATION",
    "expected_behavior": "BR06: Re-delivery of the same successful callback, identified by callback ID and payment ID, must not create another financial ledger posting.",
    "actual_behavior": "Observed 2 ledger entries instead of 1 ledger entry.",
    "impact": ["Books overstate the payment.", "Reconciliation disagrees with the gateway."],
    "evidence_references": ["execution/T04_evidence.json"],
    "handling_action": "REPORT_FAILURE",
}

DECISIONS = [
    {
        "checkpoint": "COVERAGE",
        "decision_source": "DECIDER",
        "decision_status": "VALIDATED",
        "confidence": 0.4242,
        "threshold": 0.75,
        "route": "AUTO",
        "per_question": [{"question": "coverage_action", "answer": "ACCEPT_COVERAGE", "confidence": 0.4242}],
    },
    {
        "checkpoint": "FAILURE",
        "decision_source": "HUMAN_ESCALATION",
        "decision_status": "VALIDATED",
        "confidence": None,
        "threshold": None,
        "route": "ESCALATED",
        "selected_action": "REPORT_FAILURE",
        "per_question": [],
    },
]

APPROVAL = {"decision": "APPROVE", "reviewer": "Chethan", "comment": "Looks good", "timestamp": "2026-10-18T19:00:00Z"}


def test_verdict_lines_show_both_statuses(controller):
    _write(controller, "results.json", RESULTS_DEFECT_ON)

    report = reporting.render_report(controller)

    assert "**Agent workflow:** COMPLETED" in report["report_text"]
    assert "**Payment verification:** FAILED" in report["report_text"]
    assert report["metadata"]["agent_workflow_status"] == "COMPLETED"
    assert report["metadata"]["payment_verification_status"] == "FAILED"


def test_all_pass_reports_payment_verification_passed(controller):
    all_pass = json.loads(json.dumps(RESULTS_DEFECT_ON))
    all_pass["tests"][3]["status"] = "PASS"
    _write(controller, "results.json", all_pass)

    report = reporting.render_report(controller)

    assert "**Payment verification:** PASSED" in report["report_text"]
    assert report["metadata"]["payment_verification_status"] == "PASSED"


def test_results_table_includes_every_test_row(controller):
    _write(controller, "results.json", RESULTS_DEFECT_ON)

    report = reporting.render_report(controller)

    for test_id in ("T01", "T02", "T03", "T04", "T05"):
        assert test_id in report["report_text"]
    assert "2 ledger entries" in report["report_text"]


def test_t04_failure_detail_block(controller):
    _write(controller, "results.json", RESULTS_DEFECT_ON)
    _write(controller, "analysis.json", ANALYSIS)

    report = reporting.render_report(controller)

    assert "**What happened:** Observed 2 ledger entries instead of 1 ledger entry." in report["report_text"]
    assert "BR06: Re-delivery of the same successful callback" in report["report_text"]
    assert "Books overstate the payment." in report["report_text"]
    assert "execution/T04_evidence.json" in report["report_text"]


def test_no_failure_states_no_failure_observed(controller):
    _write(controller, "results.json", RESULTS_DEFECT_ON)

    report = reporting.render_report(controller)  # no analysis.json written

    assert "No failure observed." in report["report_text"]


def test_decision_trace_rows(controller):
    _write(controller, "results.json", RESULTS_DEFECT_ON)
    _write(controller, "decisions.json", DECISIONS)

    report = reporting.render_report(controller)

    assert "COVERAGE" in report["report_text"]
    assert "ACCEPT_COVERAGE" in report["report_text"]
    assert "0.42 vs" in report["report_text"]  # polish-2 group D12: 2 decimals, was bare 0.4242
    assert "0.4242" not in report["report_text"]
    assert "REPORT_FAILURE (human)" in report["report_text"]


def test_decision_trace_table_has_outcome_column(controller):
    _write(controller, "results.json", RESULTS_DEFECT_ON)
    _write(controller, "decisions.json", DECISIONS)

    report = reporting.render_report(controller)
    text = report["report_text"]

    assert "| Checkpoint | Decision | Confidence vs τ | Outcome | Route | Source |" in text
    # polish-2 group D11/D12: friendly checkpoint label (Coverage v1, was
    # the raw "COVERAGE") and confidence to 2 decimals (was bare 0.4242).
    assert "| Coverage v1 | ✅ ACCEPT_COVERAGE | 0.42 vs 0.75 | Auto | AUTO | DECIDER |" in text
    assert "| Failure | ✅ REPORT_FAILURE (human) | — | Human confirmed | ESCALATED | HUMAN_ESCALATION |" in text


def test_decision_trace_override_detail_is_an_outcome_parenthetical_not_a_long_decision_sentence(controller):
    # polish-2 group D12: report.md has no tooltips, so the override
    # explanation goes in the Outcome cell as a parenthetical instead --
    # the Decision cell stays short (action + "(human)").
    decisions_with_override = [
        {
            "checkpoint": "COVERAGE",
            "decision_source": "DECIDER",
            "decision_status": "VALIDATED",
            "route": "ESCALATED",
            "per_question": [
                {"question": "coverage_action", "answer": None, "suggestion": "ACCEPT_COVERAGE", "confidence": 0.55, "probabilities": {}}
            ],
        },
        {
            "checkpoint": "COVERAGE",
            "decision_source": "HUMAN_ESCALATION",
            "decision_status": "VALIDATED",
            "route": "ESCALATED",
            "selected_action": "REMEDIATE_GAP",
            "per_question": [],
        },
    ]
    _write(controller, "results.json", RESULTS_DEFECT_ON)
    _write(controller, "decisions.json", decisions_with_override)

    report = reporting.render_report(controller)
    text = report["report_text"]

    assert "REMEDIATE_GAP (human)" in text
    assert "override of Decider's 'ACCEPT_COVERAGE' suggestion" in text
    decision_cell_end = text.index("REMEDIATE_GAP (human)") + len("REMEDIATE_GAP (human)")
    row_end = text.index("\n", decision_cell_end)
    assert "override" not in text[decision_cell_end:row_end].split("|")[0]  # not glued onto Decision


def test_approval_block(controller):
    _write(controller, "results.json", RESULTS_DEFECT_ON)
    _write(controller, "approval.json", APPROVAL)

    report = reporting.render_report(controller)

    assert "APPROVE by Chethan at 2026-10-18T19:00:00Z" in report["report_text"]
    assert "Looks good" in report["report_text"]


def test_closing_statement_always_present(controller):
    report = reporting.render_report(controller)
    assert "The agent reports defects; it does not change code or tests." in report["report_text"]


def test_missing_files_read_as_not_run_not_invented(controller):
    report = reporting.render_report(controller)

    assert "NOT RUN -- no execution results recorded." in report["report_text"]
    assert "NOT RUN -- no approval recorded." in report["report_text"]
    assert "No decisions recorded." in report["report_text"]


def test_report_md_written_to_run_dir(controller):
    _write(controller, "results.json", RESULTS_DEFECT_ON)

    report = reporting.render_report(controller)

    assert (controller.run_dir / "report.md").read_text(encoding="utf-8") == report["report_text"]


def test_metadata_evidence_references_only_lists_files_that_exist(controller):
    _write(controller, "results.json", RESULTS_DEFECT_ON)

    report = reporting.render_report(controller)

    assert report["metadata"]["evidence_references"] == ["results.json"]


# ---------------------------------------------------------------------------
# report.md header: title, run ID, generated-at (UTC), requirement ID,
# Decider host label + model, Bedrock model ID.
# ---------------------------------------------------------------------------


def test_header_title_and_run_id(controller):
    report = reporting.render_report(controller)
    text = report["report_text"]
    assert text.startswith("# Assurance Agent Experience — AI Test Engineer Agent report")
    assert "**Run ID:** run-1" in text


def test_header_generated_at_is_utc_iso8601(controller):
    report = reporting.render_report(controller)
    match = re.search(r"\*\*Generated at \(UTC\):\*\* (\S+)", report["report_text"])
    assert match is not None
    datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%SZ")  # raises if not that exact shape


def test_header_requirement_id_from_real_config(controller):
    requirement = json.loads((reporting.CONFIG_DIR / "requirement.json").read_text())
    report = reporting.render_report(controller)
    assert f"**Requirement ID:** {requirement['requirement_id']}" in report["report_text"]


def test_header_decider_host_label_and_model_from_real_config(controller, monkeypatch):
    monkeypatch.delenv("AITE_DECIDER_HOST_LABEL", raising=False)
    decider_cfg = json.loads((reporting.CONFIG_DIR / "decider.json").read_text())

    report = reporting.render_report(controller)

    assert f"**Decider:** {decider_cfg['host_label']} · {decider_cfg['model']}" in report["report_text"]


def test_header_decider_host_label_env_override(controller, monkeypatch):
    monkeypatch.setenv("AITE_DECIDER_HOST_LABEL", "ec2-g4dn-t4")
    report = reporting.render_report(controller)
    assert "**Decider:** ec2-g4dn-t4 ·" in report["report_text"]


def test_header_bedrock_model_id_default(controller, monkeypatch):
    monkeypatch.delenv("AITE_BEDROCK_MODEL_ID", raising=False)
    from ai_test_engineer.bedrock import DEFAULT_MODEL_ID

    report = reporting.render_report(controller)
    assert f"**Bedrock model:** {DEFAULT_MODEL_ID}" in report["report_text"]


def test_header_bedrock_model_id_env_override(controller, monkeypatch):
    monkeypatch.setenv("AITE_BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-5-5")
    report = reporting.render_report(controller)
    assert "**Bedrock model:** us.anthropic.claude-sonnet-5-5" in report["report_text"]


# ---------------------------------------------------------------------------
# Item 5: reporting.py and ui.py must use the SAME decision formatter.
# ---------------------------------------------------------------------------


def test_report_decision_trace_equals_build_decision_trace_output_directly(controller):
    """report.md's trace table is built from decision_trace.build_decision_trace
    -- the exact same function ui.py's Results/Scenarios pages call -- so
    this asserts the report's trace rows are byte-for-byte the formatter's
    own output, never a locally-reformatted copy that could drift."""
    _write(controller, "results.json", RESULTS_DEFECT_ON)
    _write(controller, "decisions.json", DECISIONS)
    decider_cfg = json.loads((reporting.CONFIG_DIR / "decider.json").read_text())

    expected_rows = decision_trace.build_decision_trace(DECISIONS, decider_cfg)
    report = reporting.render_report(controller)

    for row in expected_rows:
        assert f"{row['icon']} {row['decision_text']}" in report["report_text"]
        assert row["outcome"] in report["report_text"]
        if row["confidence"] is not None:
            # polish-2 group D12: report.md renders confidence/threshold to
            # 2 decimals (the UI's conf_bar does too) -- raw decisions.json
            # floats (e.g. 0.4242) are rounded for display, not truncated.
            assert f"{row['confidence']:.2f} vs {row['threshold']:.2f}" in report["report_text"]


def test_report_trace_outcome_equals_ui_trace_outcome(controller):
    """The strongest form of 'report trace == UI trace' (plan item 5's own
    verification bullet): the SAME rows, rendered by report.md's markdown
    table and by ui_components.decision_trace_table's HTML table, must
    carry the identical outcome value per checkpoint -- never a value
    that's right in one surface and stale/different in the other."""
    from ai_test_engineer import ui_components

    _write(controller, "results.json", RESULTS_DEFECT_ON)
    _write(controller, "decisions.json", DECISIONS)
    decider_cfg = json.loads((reporting.CONFIG_DIR / "decider.json").read_text())

    # polish-2 group D11: report.md's Checkpoint column now shows the
    # friendly label (reporting._decision_trace_md calls
    # label_decision_trace internally) -- label the rows here too so the
    # row-matching lookup below stays on the same real surface the UI
    # calls (ui.py's own call sites all label before rendering).
    rows = decision_trace.label_decision_trace(decision_trace.build_decision_trace(DECISIONS, decider_cfg))
    report_text = reporting.render_report(controller)["report_text"]
    ui_html = ui_components.decision_trace_table(rows)

    for row in rows:
        label, outcome = row["label"], row["outcome"]
        report_row_line = next(line for line in report_text.splitlines() if line.startswith(f"| {label} |"))
        assert outcome in report_row_line
        assert outcome in ui_html


