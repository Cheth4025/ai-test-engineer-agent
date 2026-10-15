"""Unit tests for the deterministic coverage validator (ai_test_engineer.coverage).

Not payment tests -- never part of the T01-T05 batch. Covers the three
required cases: missing S04 -> 4/5, valid S04 addition -> 5/5, and a
correct-ID-but-wrong-content S04 -> rejected (not counted).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from ai_test_engineer.coverage import compute_coverage

CATALOG_PATH = Path(__file__).resolve().parents[2] / "config" / "scenario_catalog.json"


def load_catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text())


def build_valid_scenario(catalog_entry: dict) -> dict:
    return {
        "scenario_id": catalog_entry["scenario_id"],
        "title": catalog_entry["title"],
        "rule_ids": list(catalog_entry["rule_ids"]),
        "test_id": catalog_entry["test_id"],
        "preconditions": list(catalog_entry["required_preconditions"]),
        "actions": [f"Perform {code}" for code in catalog_entry["required_action_codes"]],
        "action_codes": list(catalog_entry["required_action_codes"]),
        "expected_assertions": copy.deepcopy(catalog_entry["required_assertions"]),
        "gherkin": f"Given ... When ... Then ... ({catalog_entry['scenario_id']})",
    }


def test_missing_s04_reports_4_of_5():
    catalog = load_catalog()
    scenario_set = [
        build_valid_scenario(catalog["scenarios"][sid]) for sid in ("S01", "S02", "S03", "S05")
    ]

    report = compute_coverage(scenario_set, catalog)

    assert report["covered"] == 4
    assert report["required"] == 5
    assert report["percentage"] == 80.0

    s04_row = next(r for r in report["rows"] if r["scenario_id"] == "S04")
    assert s04_row["status"] == "MISSING"
    assert any("BR06" in finding for finding in s04_row["findings"])
    assert report["rule_traceability"]["BR06"] == []


def test_valid_s04_addition_gives_5_of_5():
    catalog = load_catalog()
    scenario_set = [
        build_valid_scenario(catalog["scenarios"][sid])
        for sid in ("S01", "S02", "S03", "S04", "S05")
    ]

    report = compute_coverage(scenario_set, catalog)

    assert report["covered"] == 5
    assert report["percentage"] == 100.0
    s04_row = next(r for r in report["rows"] if r["scenario_id"] == "S04")
    assert s04_row["status"] == "VALID"
    assert s04_row["findings"] == []
    assert report["rule_traceability"]["BR06"] == ["S04"]


def test_wrong_content_correct_id_rejected():
    catalog = load_catalog()
    scenario_set = [
        build_valid_scenario(catalog["scenarios"][sid]) for sid in ("S01", "S02", "S03", "S05")
    ]

    bad_s04 = build_valid_scenario(catalog["scenarios"]["S04"])
    for assertion in bad_s04["expected_assertions"]:
        if assertion["observation"] == "ledger_success_count":
            assertion["expected"] = 2  # wrong oracle: should be 1
    scenario_set.append(bad_s04)

    report = compute_coverage(scenario_set, catalog)

    assert report["covered"] == 4
    assert report["percentage"] == 80.0
    s04_row = next(r for r in report["rows"] if r["scenario_id"] == "S04")
    assert s04_row["status"] == "INVALID"
    assert any("ledger_success_count" in finding for finding in s04_row["findings"])
    assert report["rule_traceability"]["BR06"] == []
