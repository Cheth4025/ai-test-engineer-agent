"""Deterministic content-based Required Scenario Coverage validator.

Validates generated scenarios (05_Data_Contracts.md Section 4 shape) against
the fixed semantic oracle in config/scenario_catalog.json. Checks structured
content (preconditions, action_codes, expected_assertions, rule/test mapping),
not just scenario_id presence. Gherkin/prose semantic grounding is explicitly
out of scope here -- that is the Decider guardrail's job in a later milestone
(07_Test_Automation_Design.md Section 6, "Revision 2"); this validator only
requires Gherkin to be present.
"""

from __future__ import annotations


def compute_coverage(
    scenario_set: list[dict], catalog: dict, scenario_set_version: int = 1
) -> dict:
    required_ids: list[str] = catalog["required_scenarios"]
    rows: list[dict] = []
    covered = 0

    for scenario_id in required_ids:
        catalog_entry = catalog["scenarios"][scenario_id]
        matches = [s for s in scenario_set if s.get("scenario_id") == scenario_id]

        if not matches:
            rows.append(
                {
                    "scenario_id": scenario_id,
                    "test_id": catalog_entry["test_id"],
                    "rule_ids": catalog_entry["rule_ids"],
                    "status": "MISSING",
                    "findings": [
                        f"{scenario_id}/{'+'.join(catalog_entry['rule_ids'])} content missing"
                    ],
                }
            )
            continue

        candidate = matches[0]
        findings = _validate_content(candidate, catalog_entry)
        if len(matches) > 1:
            findings.append(
                f"duplicate scenario_id {scenario_id}: {len(matches)} entries submitted; extras ignored"
            )

        status = "VALID" if not findings else "INVALID"
        if status == "VALID":
            covered += 1

        rows.append(
            {
                "scenario_id": scenario_id,
                "test_id": catalog_entry["test_id"],
                "rule_ids": catalog_entry["rule_ids"],
                "status": status,
                "findings": findings,
            }
        )

    required = len(required_ids)
    percentage = round(covered / required * 100, 2) if required else 0.0
    rule_traceability = _rule_traceability(catalog, rows)

    return {
        "metric_name": "Required Scenario Coverage",
        "scenario_set_version": scenario_set_version,
        "covered": covered,
        "required": required,
        "percentage": percentage,
        "rows": rows,
        "rule_traceability": rule_traceability,
    }


def _validate_content(candidate: dict, catalog_entry: dict) -> list[str]:
    findings: list[str] = []

    if candidate.get("test_id") != catalog_entry["test_id"]:
        findings.append(
            f"test_id mismatch: expected {catalog_entry['test_id']}, got {candidate.get('test_id')!r}"
        )

    if set(candidate.get("rule_ids") or []) != set(catalog_entry["rule_ids"]):
        findings.append(
            f"rule_ids mismatch: expected {catalog_entry['rule_ids']}, got {candidate.get('rule_ids')!r}"
        )

    preconditions = candidate.get("preconditions") or []
    if not preconditions or not all(isinstance(p, str) and p.strip() for p in preconditions):
        findings.append("preconditions missing or empty")

    actions = candidate.get("actions") or []
    if not actions or not all(isinstance(a, str) and a.strip() for a in actions):
        findings.append("actions missing or empty")

    action_codes = candidate.get("action_codes") or []
    if action_codes != catalog_entry["required_action_codes"]:
        findings.append(
            f"action_codes mismatch: expected {catalog_entry['required_action_codes']}, got {action_codes!r}"
        )

    submitted_assertions = candidate.get("expected_assertions") or []
    submitted_map = {
        (a.get("observation"), a.get("operator")): a.get("expected")
        for a in submitted_assertions
    }
    for required_assertion in catalog_entry["required_assertions"]:
        key = (required_assertion["observation"], required_assertion["operator"])
        if key not in submitted_map:
            findings.append(
                f"missing assertion {required_assertion['observation']} {required_assertion['operator']}"
            )
        elif submitted_map[key] != required_assertion["expected"]:
            findings.append(
                f"assertion {required_assertion['observation']} expected "
                f"{required_assertion['expected']!r}, got {submitted_map[key]!r}"
            )

    gherkin = candidate.get("gherkin")
    if not isinstance(gherkin, str) or not gherkin.strip():
        findings.append("gherkin missing or empty")

    return findings


def _rule_traceability(catalog: dict, rows: list[dict]) -> dict[str, list[str]]:
    all_rule_ids = sorted(
        {rule_id for entry in catalog["scenarios"].values() for rule_id in entry["rule_ids"]}
    )
    traceability: dict[str, list[str]] = {rule_id: [] for rule_id in all_rule_ids}
    for row in rows:
        if row["status"] != "VALID":
            continue
        for rule_id in row["rule_ids"]:
            traceability[rule_id].append(row["scenario_id"])
    return traceability
