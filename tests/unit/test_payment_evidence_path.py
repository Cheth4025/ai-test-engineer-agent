"""Direct unit test for tests/payment/conftest.py's write_evidence path
resolution -- loaded by file path (tests/ has no __init__.py, so it isn't an
importable package) rather than imported as a module."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_CONFTEST_PATH = Path(__file__).resolve().parents[1] / "payment" / "conftest.py"


def _load_conftest():
    spec = importlib.util.spec_from_file_location("payment_conftest_under_test", _CONFTEST_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_write_evidence_uses_run_execution_dir_when_set(tmp_path, monkeypatch):
    conftest = _load_conftest()
    run_execution_dir = tmp_path / "runs" / "run-1" / "execution"
    monkeypatch.setenv("AITE_RUN_EXECUTION_DIR", str(run_execution_dir))

    path = conftest.write_evidence("T04", {"test_id": "T04"})

    assert path == run_execution_dir / "T04_evidence.json"
    assert json.loads(path.read_text()) == {"test_id": "T04"}


def test_write_evidence_falls_back_to_milestone1_when_unset(tmp_path, monkeypatch):
    conftest = _load_conftest()
    monkeypatch.delenv("AITE_RUN_EXECUTION_DIR", raising=False)
    monkeypatch.setenv("AITE_ARTIFACT_ROOT", str(tmp_path))

    path = conftest.write_evidence("T01", {"test_id": "T01"})

    assert path == tmp_path / "milestone1" / "execution" / "T01_evidence.json"
