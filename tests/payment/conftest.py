"""Fixtures and evidence writer for the five fixed payment tests (T01-T05).

Each test gets a fresh MockPaymentService. Evidence is written from fixture
teardown (not the test body) so it survives a failing assertion -- T04 is
expected to fail on purpose and its evidence must still be recorded
(docs/07_Test_Automation_Design.md Section 4).
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from pathlib import Path

import pytest

from ai_test_engineer.mock_payment import MockPaymentService

PAYMENT_ID = "P-001"
KEY = "K-001"
AMOUNT = "100.00"
CURRENCY = "USD"
CALLBACK_ID = "C-001"

_NODE_NAME_PATTERN = re.compile(r"test_(t\d{2})_")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    rep = yield
    setattr(item, f"rep_{rep.when}", rep)
    return rep


def _test_id_for(node_name: str) -> str:
    match = _NODE_NAME_PATTERN.match(node_name)
    return match.group(1).upper() if match else node_name


def write_evidence(test_id: str, record: dict) -> Path:
    """AITE_RUN_EXECUTION_DIR wins when set -- execution.py sets it to the
    real run's execution/ directory when it spawns this suite as a child
    process (docs/07 Section 4: "Child receives ... evidence path through
    controller-generated environment values"). Falls back to the historical
    milestone1/execution path for a bare `pytest tests/payment` run outside
    any run context."""
    run_execution_dir = os.environ.get("AITE_RUN_EXECUTION_DIR")
    if run_execution_dir:
        evidence_dir = Path(run_execution_dir)
    else:
        artifact_root = Path(os.environ.get("AITE_ARTIFACT_ROOT", "./runs"))
        evidence_dir = artifact_root / "milestone1" / "execution"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = evidence_dir / f"{test_id}_evidence.json"
    evidence_path.write_text(json.dumps(record, indent=2, default=str))
    return evidence_path


@pytest.fixture
def mock_service(request):
    service = MockPaymentService()
    yield service

    test_id = _test_id_for(request.node.name)
    outcome_report = getattr(request.node, "rep_call", None)
    if outcome_report is not None and outcome_report.failed:
        test_outcome = "FAIL"
    elif outcome_report is not None and outcome_report.passed:
        test_outcome = "PASS"
    else:
        test_outcome = "UNKNOWN"

    record = {
        "test_id": test_id,
        "test_outcome": test_outcome,
        "duplicate_ledger_defect": service.duplicate_ledger_defect,
        "payments": {
            payment_id: dataclasses.asdict(payment)
            for payment_id, payment in service.payments.items()
        },
        "gateway_charges": [dataclasses.asdict(c) for c in service.gateway_charges],
        "ledger": [dataclasses.asdict(e) for e in service.ledger],
        "events": [dataclasses.asdict(e) for e in service.events],
    }
    write_evidence(test_id, record)
