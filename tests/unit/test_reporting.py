"""Unit tests for ai_test_engineer.reporting.render_report -- content only,
built from hand-written fixture files (no live run, no Bedrock/Decider)."""

from __future__ import annotations

import json

import pytest

from ai_test_engineer import reporting
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
        "per_question": [{"question": "coverage_action", "answer": "ACCEPT_COVERAGE"}],
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
    assert "0.4242" in report["report_text"]
    assert "REPORT_FAILURE (human)" in report["report_text"]


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
