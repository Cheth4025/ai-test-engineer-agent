"""Runs the approved, fixed T01-T05 pytest batch as a child process and
assembles results.json from its real JUnit output (docs/07 Section 4-5).

Deterministic and model-free: no Bedrock, no Decider. Called exactly once
per run, only after workflow_state == APPROVED and
RunController.use_execution() has reserved the batch -- see
tools.build_execute_tests_tool and orchestration.execute_approved_run.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from ai_test_engineer.state import RunController

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
PYTEST_TIMEOUT_SECONDS = int(os.environ.get("AITE_PYTEST_TIMEOUT_SECONDS", "60"))

# The one fixed payment fixture every T01-T05 test uses (docs/02 Section 2).
FIXED_PAYMENT_ID = "P-001"

_NODE_NAME_PATTERN = re.compile(r"test_(t\d{2})_")


class ExecutionError(RuntimeError):
    """A runner-level failure -- launch failure, timeout, or a wrong
    collected-test count. Never silently treated as five successful runs."""


def _load_manifest() -> list[dict]:
    manifest = json.loads((CONFIG_DIR / "test_manifest.json").read_text())
    return manifest["tests"]


def _test_id_from_node_name(name: str) -> str:
    match = _NODE_NAME_PATTERN.match(name)
    return match.group(1).upper() if match else name


def _strip_junit_hostname(junit_path: Path) -> None:
    """pytest's own junitxml writer stamps <testsuite hostname="..."> with
    the real machine name (confirmed directly: the literal Windows computer
    name) -- junit.xml is one of the files evidence.zip bundles, so this
    runs immediately after pytest writes it, before anything else reads or
    ships the file."""
    tree = ET.parse(junit_path)
    for testsuite in tree.getroot().iter("testsuite"):
        testsuite.attrib.pop("hostname", None)
    tree.write(junit_path, encoding="utf-8", xml_declaration=True)


def _parse_junit(junit_path: Path) -> dict[str, dict]:
    """test_id ("T01".."T05") -> {"status", "duration_ms", "failure_message"}."""
    root = ET.parse(junit_path).getroot()
    results: dict[str, dict] = {}
    for case in root.findall(".//testcase"):
        test_id = _test_id_from_node_name(case.get("name", ""))
        duration_ms = round(float(case.get("time", "0")) * 1000)
        failure = case.find("failure")
        error = case.find("error")
        skipped = case.find("skipped")
        if failure is not None:
            status, message = "FAIL", failure.get("message") or (failure.text or "").strip()
        elif error is not None:
            status, message = "ERROR", error.get("message") or (error.text or "").strip()
        elif skipped is not None:
            status, message = "SKIPPED", skipped.get("message")
        else:
            status, message = "PASS", None
        results[test_id] = {"status": status, "duration_ms": duration_ms, "failure_message": message}
    return results


def _gateway_charge_count(evidence: dict) -> int:
    return sum(1 for c in evidence.get("gateway_charges", []) if c.get("payment_id") == FIXED_PAYMENT_ID)


def _ledger_entry_count(evidence: dict) -> int:
    return sum(1 for e in evidence.get("ledger", []) if e.get("payment_id") == FIXED_PAYMENT_ID)


def _conflict_rejected(evidence: dict) -> bool:
    return any(e.get("action") == "conflict_rejected" for e in evidence.get("events", []))


def expected_and_actual(test_id: str, evidence: dict) -> tuple[str, str]:
    """One real, human-readable Expected/Actual pair per test, derived from
    the test's own dumped state (tests/payment/conftest.py's T0N_evidence.json)
    -- never copied from a catalog Expected string. T03's headline is the
    conflict-rejection outcome; T04's is the ledger-duplication defect (the
    one BR06 actually governs); every other test's is the gateway-charge
    count."""
    if test_id == "T03":
        return "Conflict", ("Conflict" if _conflict_rejected(evidence) else "Accepted")
    if test_id == "T04":
        count = _ledger_entry_count(evidence)
        return "1 ledger entry", f"{count} ledger entr{'y' if count == 1 else 'ies'}"
    count = _gateway_charge_count(evidence)
    return "1 charge", f"{count} charge{'' if count == 1 else 's'}"


def run_approved_tests(controller: RunController) -> dict:
    manifest = _load_manifest()
    node_ids = [test["node_id"] for test in manifest]
    execution_dir = controller.run_dir / "execution"
    execution_dir.mkdir(parents=True, exist_ok=True)
    junit_path = execution_dir / "junit.xml"
    log_path = execution_dir / "pytest_output.log"

    argv = [
        sys.executable,
        "-m",
        "pytest",
        *node_ids,
        f"--junitxml={junit_path}",
        "-q",
        "-p",
        "no:cacheprovider",
    ]
    # Never pass AWS credentials to the pytest child (docs/08 Section 4).
    env = {key: value for key, value in os.environ.items() if not key.startswith("AWS_")}
    env["AITE_RUN_EXECUTION_DIR"] = str(execution_dir)

    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=PYTEST_TIMEOUT_SECONDS,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExecutionError(f"pytest batch exceeded {PYTEST_TIMEOUT_SECONDS}s timeout") from exc
    duration_ms = round((time.monotonic() - started) * 1000)

    log_path.write_text((completed.stdout or "") + (completed.stderr or ""))

    if not junit_path.exists():
        raise ExecutionError(
            f"pytest did not produce junit.xml (return_code={completed.returncode}); "
            "see execution/pytest_output.log"
        )
    _strip_junit_hostname(junit_path)
    junit_results = _parse_junit(junit_path)

    expected_ids = {test["test_id"] for test in manifest}
    collected_ids = set(junit_results)
    if collected_ids != expected_ids:
        raise ExecutionError(
            f"expected exactly {sorted(expected_ids)} collected, got {sorted(collected_ids)} "
            "-- incomplete batch, never reported as five successful executions"
        )

    tests = []
    for test in manifest:
        test_id = test["test_id"]
        evidence_path = execution_dir / f"{test_id}_evidence.json"
        evidence = json.loads(evidence_path.read_text()) if evidence_path.exists() else {}
        expected, actual = expected_and_actual(test_id, evidence)
        junit_result = junit_results[test_id]
        tests.append(
            {
                "test_id": test_id,
                "scenario_id": test["scenario_id"],
                "rule_ids": test["rule_ids"],
                "status": junit_result["status"],
                "duration_ms": junit_result["duration_ms"],
                "expected": expected,
                "actual": actual,
                "failure_message": junit_result["failure_message"],
                "evidence_reference": f"execution/{test_id}_evidence.json" if evidence_path.exists() else None,
            }
        )

    return {
        "execution_id": controller.run_id,
        "return_code": completed.returncode,
        "duration_ms": duration_ms,
        "tests": tests,
        "junit_reference": "execution/junit.xml",
        "log_reference": "execution/pytest_output.log",
    }
