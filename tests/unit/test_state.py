"""RunController.resolve_grounding / effective_coverage / unresolved_grounding_flags
/ approval_blockers -- the human-grounding-resolution surface Milestone 5's UI
Tab 2 drives, plus the read-only aggregate the next milestone's Approve guard
will consume.

Evidence immutability: resolve_grounding must never rewrite coverage_vN.json
(the validator's recorded output) -- every test here that asserts a human
override's effect also asserts the recorded coverage file is untouched.
"""

from __future__ import annotations

import json

import pytest

from ai_test_engineer.state import RunController


def _write_grounding(controller: RunController, version: int, items: list[dict]) -> None:
    (controller.run_dir / f"grounding_v{version}.json").write_text(
        json.dumps({"scenario_set_version": version, "items": items})
    )


def _write_coverage(controller: RunController, version: int, rows: list[dict], rule_traceability: dict) -> dict:
    required = len(rows)
    covered = sum(1 for r in rows if r["status"] == "VALID")
    coverage = {
        "metric_name": "Required Scenario Coverage",
        "scenario_set_version": version,
        "covered": covered,
        "required": required,
        "percentage": round(covered / required * 100, 2) if required else 0.0,
        "rows": rows,
        "rule_traceability": rule_traceability,
    }
    (controller.run_dir / f"coverage_v{version}.json").write_text(json.dumps(coverage))
    return coverage


def _five_valid_rows() -> list[dict]:
    return [
        {"scenario_id": sid, "test_id": f"T{sid[1:]}", "rule_ids": ["BR01"], "status": "VALID", "findings": []}
        for sid in ("S01", "S02", "S03", "S04", "S05")
    ]


# ---------------------------------------------------------------------------
# resolve_grounding
# ---------------------------------------------------------------------------


def test_resolve_grounding_writes_human_fields_to_grounding_file_only(tmp_path):
    controller = RunController("r1", tmp_path)
    _write_grounding(
        controller,
        1,
        [{"scenario_id": "S04", "p_yes": 0.5, "confidence": 0.5, "verdict": "NEEDS_REVIEW", "human_resolution": None}],
    )
    rows = _five_valid_rows()
    _write_coverage(controller, 1, rows, {"BR01": ["S01", "S02", "S03", "S04", "S05"]})
    recorded_before = (controller.run_dir / "coverage_v1.json").read_text()

    controller.resolve_grounding(1, "S04", "CONFIRMED_GROUNDED", note="looks fine on review")

    grounding = json.loads((controller.run_dir / "grounding_v1.json").read_text())
    item = grounding["items"][0]
    assert item["human_resolution"] == "CONFIRMED_GROUNDED"
    assert item["human_note"] == "looks fine on review"
    assert "human_resolved_at_utc" in item and item["human_resolved_at_utc"]

    # Evidence immutability: coverage_v1.json is untouched, byte for byte.
    assert (controller.run_dir / "coverage_v1.json").read_text() == recorded_before


def test_resolve_grounding_unknown_scenario_id_raises(tmp_path):
    controller = RunController("r1", tmp_path)
    _write_grounding(controller, 1, [{"scenario_id": "S04", "verdict": "NEEDS_REVIEW", "human_resolution": None}])

    with pytest.raises(ValueError):
        controller.resolve_grounding(1, "S99", "CONFIRMED_GROUNDED")


def test_resolve_grounding_unknown_resolution_raises(tmp_path):
    controller = RunController("r1", tmp_path)
    _write_grounding(controller, 1, [{"scenario_id": "S04", "verdict": "NEEDS_REVIEW", "human_resolution": None}])

    with pytest.raises(AssertionError):
        controller.resolve_grounding(1, "S04", "MAYBE")


def test_resolve_grounding_repeat_call_overwrites_previous_resolution(tmp_path):
    """Not idempotence (the result does change): resolve_grounding is safe
    to call again with a different resolution -- a human reconsidering --
    and the later call's fields replace the earlier one's, not append a
    duplicate item."""
    controller = RunController("r1", tmp_path)
    _write_grounding(controller, 1, [{"scenario_id": "S04", "verdict": "NEEDS_REVIEW", "human_resolution": None}])

    controller.resolve_grounding(1, "S04", "CONFIRMED_NOT_GROUNDED", note="first")
    controller.resolve_grounding(1, "S04", "CONFIRMED_GROUNDED", note="changed my mind")

    items = json.loads((controller.run_dir / "grounding_v1.json").read_text())["items"]
    assert len(items) == 1
    assert items[0]["human_resolution"] == "CONFIRMED_GROUNDED"
    assert items[0]["human_note"] == "changed my mind"


# ---------------------------------------------------------------------------
# effective_coverage
# ---------------------------------------------------------------------------


def test_effective_coverage_matches_recorded_when_no_human_resolution(tmp_path):
    controller = RunController("r1", tmp_path)
    rows = _five_valid_rows()
    recorded = _write_coverage(controller, 1, rows, {"BR01": ["S01", "S02", "S03", "S04", "S05"]})

    effective = controller.effective_coverage(1)

    assert effective == recorded


def test_effective_coverage_confirmed_not_grounded_flips_valid_row_to_invalid(tmp_path):
    controller = RunController("r1", tmp_path)
    rows = _five_valid_rows()
    _write_coverage(controller, 1, rows, {"BR01": ["S01", "S02", "S03", "S04", "S05"]})
    _write_grounding(
        controller,
        1,
        [{"scenario_id": "S04", "verdict": "NOT_GROUNDED", "human_resolution": None}],
    )
    recorded_before = (controller.run_dir / "coverage_v1.json").read_text()

    controller.resolve_grounding(1, "S04", "CONFIRMED_NOT_GROUNDED", note="fabricated preconditions")
    effective = controller.effective_coverage(1)

    s04_row = next(r for r in effective["rows"] if r["scenario_id"] == "S04")
    assert s04_row["status"] == "INVALID"
    assert s04_row["findings"]  # a reason was appended
    assert effective["covered"] == 4
    assert effective["percentage"] == 80.0
    assert "S04" not in effective["rule_traceability"]["BR01"]

    # Evidence immutability: the recorded file on disk never changed.
    assert (controller.run_dir / "coverage_v1.json").read_text() == recorded_before


def test_effective_coverage_confirmed_grounded_produces_no_change(tmp_path):
    controller = RunController("r1", tmp_path)
    rows = _five_valid_rows()
    recorded = _write_coverage(controller, 1, rows, {"BR01": ["S01", "S02", "S03", "S04", "S05"]})
    _write_grounding(controller, 1, [{"scenario_id": "S04", "verdict": "NEEDS_REVIEW", "human_resolution": None}])

    controller.resolve_grounding(1, "S04", "CONFIRMED_GROUNDED")
    effective = controller.effective_coverage(1)

    assert effective == recorded


def test_effective_coverage_raises_if_coverage_file_missing(tmp_path):
    controller = RunController("r1", tmp_path)

    with pytest.raises(FileNotFoundError):
        controller.effective_coverage(1)


def test_effective_coverage_tolerates_missing_grounding_file(tmp_path):
    controller = RunController("r1", tmp_path)
    rows = _five_valid_rows()
    recorded = _write_coverage(controller, 1, rows, {"BR01": ["S01", "S02", "S03", "S04", "S05"]})

    effective = controller.effective_coverage(1)

    assert effective == recorded


# ---------------------------------------------------------------------------
# unresolved_grounding_flags
# ---------------------------------------------------------------------------


def test_unresolved_grounding_flags_lists_not_grounded_and_needs_review_only(tmp_path):
    controller = RunController("r1", tmp_path)
    _write_grounding(
        controller,
        1,
        [
            {"scenario_id": "S01", "verdict": "GROUNDED", "human_resolution": None},
            {"scenario_id": "S02", "verdict": "NOT_GROUNDED", "human_resolution": None},
            {"scenario_id": "S03", "verdict": "NEEDS_REVIEW", "human_resolution": None},
        ],
    )

    assert controller.unresolved_grounding_flags(1) == ["S02", "S03"]


def test_unresolved_grounding_flags_excludes_already_resolved(tmp_path):
    controller = RunController("r1", tmp_path)
    _write_grounding(
        controller,
        1,
        [
            {"scenario_id": "S02", "verdict": "NOT_GROUNDED", "human_resolution": None},
            {"scenario_id": "S03", "verdict": "NEEDS_REVIEW", "human_resolution": None},
        ],
    )
    controller.resolve_grounding(1, "S02", "CONFIRMED_NOT_GROUNDED")

    assert controller.unresolved_grounding_flags(1) == ["S03"]


def test_unresolved_grounding_flags_empty_when_file_missing(tmp_path):
    controller = RunController("r1", tmp_path)

    assert controller.unresolved_grounding_flags(1) == []


# ---------------------------------------------------------------------------
# approval_blockers
# ---------------------------------------------------------------------------


def test_approval_blockers_empty_when_nothing_blocks(tmp_path):
    controller = RunController("r1", tmp_path)
    rows = _five_valid_rows()
    _write_coverage(controller, 1, rows, {"BR01": ["S01", "S02", "S03", "S04", "S05"]})
    (controller.run_dir / "interpretation.json").write_text(json.dumps({"rules": [], "ambiguities": []}))

    assert controller.approval_blockers(1) == []


def test_approval_blockers_includes_unresolved_grounding_flag(tmp_path):
    controller = RunController("r1", tmp_path)
    rows = _five_valid_rows()
    _write_coverage(controller, 1, rows, {"BR01": ["S01", "S02", "S03", "S04", "S05"]})
    _write_grounding(controller, 1, [{"scenario_id": "S04", "verdict": "NEEDS_REVIEW", "human_resolution": None}])
    (controller.run_dir / "interpretation.json").write_text(json.dumps({"rules": [], "ambiguities": []}))

    blockers = controller.approval_blockers(1)

    assert any("S04" in b for b in blockers)


def test_approval_blockers_includes_effective_coverage_shortfall(tmp_path):
    controller = RunController("r1", tmp_path)
    rows = _five_valid_rows()
    _write_coverage(controller, 1, rows, {"BR01": ["S01", "S02", "S03", "S04", "S05"]})
    _write_grounding(controller, 1, [{"scenario_id": "S04", "verdict": "NOT_GROUNDED", "human_resolution": None}])
    (controller.run_dir / "interpretation.json").write_text(json.dumps({"rules": [], "ambiguities": []}))
    controller.resolve_grounding(1, "S04", "CONFIRMED_NOT_GROUNDED")

    blockers = controller.approval_blockers(1)

    # Recorded coverage was 5/5, but the effective (human-adjusted) view is 4/5.
    assert "effective coverage 4/5 is below required 5/5" in blockers


def test_approval_blockers_includes_critical_open_ambiguity(tmp_path):
    controller = RunController("r1", tmp_path)
    rows = _five_valid_rows()
    _write_coverage(controller, 1, rows, {"BR01": ["S01", "S02", "S03", "S04", "S05"]})
    (controller.run_dir / "interpretation.json").write_text(
        json.dumps(
            {
                "rules": [],
                "ambiguities": [
                    {"id": "A1", "rule_ids": ["BR04"], "question": "what counts as a timeout?", "critical": True, "status": "OPEN"},
                    {"id": "A2", "rule_ids": ["BR02"], "question": "non-critical thing", "critical": False, "status": "OPEN"},
                ],
            }
        )
    )

    blockers = controller.approval_blockers(1)

    assert any("A1" in b or "timeout" in b for b in blockers)
    assert not any("A2" in b or "non-critical thing" in b for b in blockers)


def test_approval_blockers_combines_all_three_without_short_circuit(tmp_path):
    """Genuinely exercises all three blocker types at once: a recorded
    coverage shortfall (not merely an effective-coverage override), an
    unresolved grounding flag on a DIFFERENT scenario, and a critical
    ambiguity -- proving none of the three checks short-circuits the
    others."""
    controller = RunController("r1", tmp_path)
    rows = _five_valid_rows()
    rows[-1]["status"] = "MISSING"  # S05 missing -> recorded coverage is already 4/5
    rows[-1]["findings"] = ["S05/BR01 content missing"]
    _write_coverage(controller, 1, rows, {"BR01": ["S01", "S02", "S03", "S04"]})
    _write_grounding(controller, 1, [{"scenario_id": "S03", "verdict": "NEEDS_REVIEW", "human_resolution": None}])
    (controller.run_dir / "interpretation.json").write_text(
        json.dumps(
            {
                "rules": [],
                "ambiguities": [
                    {"id": "A1", "rule_ids": ["BR04"], "question": "q", "critical": True, "status": "CONFLICT"}
                ],
            }
        )
    )

    blockers = controller.approval_blockers(1)

    assert len(blockers) == 3
    assert any("S03" in b for b in blockers)
    assert "effective coverage 4/5 is below required 5/5" in blockers
    assert any("A1" in b for b in blockers)
