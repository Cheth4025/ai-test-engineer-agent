"""Unit tests for ai_test_engineer.execution: a real (fast, local, no
Bedrock/Decider) pytest subprocess run against tests/payment, defect-on by
default. No live Decider/Bedrock runs -- this spawns the same pytest suite
the repo's own test command already runs, just as a child process.
"""

from __future__ import annotations

import json

import pytest

from ai_test_engineer import execution
from ai_test_engineer.state import RunController


@pytest.fixture
def controller(tmp_path) -> RunController:
    return RunController("run-1", tmp_path / "run-1")


def test_defect_on_batch_is_four_pass_one_fail(controller, monkeypatch):
    monkeypatch.delenv("AITE_DUPLICATE_LEDGER_DEFECT", raising=False)  # default true

    results = execution.run_approved_tests(controller)

    statuses = {t["test_id"]: t["status"] for t in results["tests"]}
    assert statuses == {"T01": "PASS", "T02": "PASS", "T03": "PASS", "T04": "FAIL", "T05": "PASS"}
    assert results["return_code"] != 0
    assert len(results["tests"]) == 5


def test_defect_off_batch_is_five_pass(controller, monkeypatch):
    monkeypatch.setenv("AITE_DUPLICATE_LEDGER_DEFECT", "false")

    results = execution.run_approved_tests(controller)

    assert all(t["status"] == "PASS" for t in results["tests"])
    assert results["return_code"] == 0


def test_t04_actual_reflects_the_real_duplicate_ledger_entry(controller):
    results = execution.run_approved_tests(controller)

    t04 = next(t for t in results["tests"] if t["test_id"] == "T04")
    assert t04["expected"] == "1 ledger entry"
    assert t04["actual"] == "2 ledger entries"
    assert t04["failure_message"]


def test_junit_xml_never_leaks_the_machine_hostname(controller):
    """pytest's own junitxml writer embeds <testsuite hostname="..."> --
    the real machine/account name (confirmed directly: on this dev machine
    it writes the literal Windows computer name) -- and junit.xml is one of
    the files bundled into evidence.zip. Strip it before it's ever written
    where a downloaded zip could carry it."""
    execution.run_approved_tests(controller)

    junit_text = (controller.run_dir / "execution" / "junit.xml").read_text(encoding="utf-8")
    assert "hostname=" not in junit_text


def test_results_json_shape_and_evidence_references(controller):
    results = execution.run_approved_tests(controller)

    assert results["execution_id"] == "run-1"
    assert results["junit_reference"] == "execution/junit.xml"
    assert results["log_reference"] == "execution/pytest_output.log"
    assert (controller.run_dir / "execution" / "junit.xml").exists()
    assert (controller.run_dir / "execution" / "pytest_output.log").exists()
    for test in results["tests"]:
        assert test["evidence_reference"] == f"execution/{test['test_id']}_evidence.json"
        assert (controller.run_dir / test["evidence_reference"]).exists()
        evidence = json.loads((controller.run_dir / test["evidence_reference"]).read_text())
        assert evidence["test_id"] == test["test_id"]


def test_evidence_written_under_this_runs_own_execution_dir_not_milestone1(controller):
    execution.run_approved_tests(controller)

    assert (controller.run_dir / "execution" / "T04_evidence.json").exists()


def test_timeout_raises_execution_error(controller, monkeypatch):
    import subprocess

    def _boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=1)

    monkeypatch.setattr(execution.subprocess, "run", _boom)

    with pytest.raises(execution.ExecutionError, match="timeout"):
        execution.run_approved_tests(controller)


def test_missing_junit_raises_execution_error(controller, monkeypatch):
    class _FakeCompleted:
        returncode = 1
        stdout = "collection error"
        stderr = ""

    monkeypatch.setattr(execution.subprocess, "run", lambda *a, **k: _FakeCompleted())

    with pytest.raises(execution.ExecutionError, match="junit"):
        execution.run_approved_tests(controller)


def test_aws_env_vars_never_reach_the_pytest_child(controller, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "sk-should-never-be-passed")
    captured_env = {}
    real_run = execution.subprocess.run

    def _spy_run(argv, **kwargs):
        captured_env.update(kwargs.get("env") or {})
        return real_run(argv, **kwargs)

    monkeypatch.setattr(execution.subprocess, "run", _spy_run)

    execution.run_approved_tests(controller)

    assert "AWS_SECRET_ACCESS_KEY" not in captured_env


def test_expected_and_actual_t03_conflict():
    evidence = {"events": [{"action": "conflict_rejected"}]}
    assert execution.expected_and_actual("T03", evidence) == ("Conflict", "Conflict")


def test_expected_and_actual_t01_charge_count():
    evidence = {"gateway_charges": [{"payment_id": "P-001"}]}
    assert execution.expected_and_actual("T01", evidence) == ("1 charge", "1 charge")
