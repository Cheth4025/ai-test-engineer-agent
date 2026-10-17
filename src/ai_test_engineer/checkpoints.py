"""Payment-domain Decider wiring for Milestone 4.

decider_gate stays domain-agnostic (plus the one generic
CheckpointSpec.build_state field added this milestone); this module supplies
the only business knowledge: which questions get asked, what state Decider
sees, and what an AUTO verdict means, for generate_scenarios and
save_scenario_set.

Guard/action-crafting logic (full_coverage, remediation_feedback,
grounding_items) is written as pure functions so it can be called from BOTH
resolve() (the AUTO path, run synchronously inside DeciderGuardrail.before_tool_call)
and orchestration.py's _apply_human_decision() (the human-escalation path,
run after a human resumes a paused run) -- one implementation, two callers,
never duplicated logic that could drift.

Remediation is detected via an explicit holder["remediation_feedback"] flag,
set the instant resolve() decides on REMEDIATE_GAP, and read by
orchestration.py immediately once the triggering agent() call returns --
never inferred from decisions.json after the fact. A Guide action was
deliberately NOT used for REMEDIATE_GAP: Guide hands control back to the
model within the same agent() call (the model could call another tool before
orchestration regains control); Deny is used instead, and orchestration
drives the actual regeneration itself via _drive_recheck.
"""

from __future__ import annotations

from typing import Any

from decider_gate.client import DeciderClient
from decider_gate.gate import ConfidenceGate, Verdict
from decider_gate.intervention import CheckpointSpec
from strands.interventions import Confirm, Deny, Proceed

from ai_test_engineer.coverage import compute_coverage
from ai_test_engineer.state import RunController
from ai_test_engineer.tools import AttemptLog

# ---------------------------------------------------------------------------
# Pure guard / action-crafting logic -- shared by resolve() and by
# orchestration.py's _apply_human_decision().
# ---------------------------------------------------------------------------


def full_coverage(coverage: dict) -> bool:
    return coverage["covered"] == coverage["required"]


def remediation_feedback(missing_ids: list[str], coverage: dict) -> str:
    if missing_ids == ["S04"]:
        return (
            "S04 / BR06 missing: add a duplicate-callback scenario asserting "
            "exactly one ledger posting; keep S01-S03, S05 unchanged"
        )
    return (
        f"Coverage gap: {missing_ids} still missing/invalid; "
        f"see coverage_v{coverage['scenario_set_version']}.json"
    )


def grounding_items(coverage: dict, verdicts: dict[str, Verdict]) -> list[dict]:
    """Builds the grounding sub-record from whatever verdicts are available.

    AUTO verdicts have .answer (True/False); ESCALATEd ones have .answer=None
    -> NEEDS_REVIEW. Never participates in full_coverage() -- cannot promote
    an otherwise-INVALID/MISSING row to valid.
    """
    items = []
    for row in coverage["rows"]:
        v = verdicts.get(f"grounding_{row['scenario_id']}")
        if v is None:
            continue
        if v.route == "ESCALATE":
            verdict = "NEEDS_REVIEW"
        else:
            verdict = "GROUNDED" if v.answer else "NOT_GROUNDED"
        items.append(
            {
                "scenario_id": row["scenario_id"],
                "p_yes": v.probabilities["true"],
                "confidence": v.confidence,
                "verdict": verdict,
                "human_resolution": None,
                "native_response_ref": None,
            }
        )
    return items


# ---------------------------------------------------------------------------
# generate_scenarios: combined AMBIGUITY + STRATEGY
# ---------------------------------------------------------------------------


def build_generate_scenarios_checkpoint(attempt_log: AttemptLog, controller: RunController) -> CheckpointSpec:
    def _rules() -> list[dict]:
        return attempt_log.get_result("interpret_requirement")["rules"]

    def build_questions(state: Any) -> dict:
        questions = {
            f"ambiguity_{rule['rule_id']}": {
                "type": "noul",
                "instructions": f"Rule {rule['rule_id']}: {rule['meaning']}.",
                "criteria": {
                    "true": (
                        f"Rule {rule['rule_id']} ({rule['meaning']}) leaves a question whose "
                        "answer would change what the test must assert -- e.g. which exact "
                        "condition, value, or branch applies is not pinned down by the "
                        "requirement text."
                    ),
                    "false": (
                        f"Rule {rule['rule_id']} ({rule['meaning']}) is fully specified; "
                        "nothing about it is unclear for writing a test that asserts the "
                        "correct outcome."
                    ),
                },
            }
            for rule in _rules()
        }
        questions["strategy_action"] = {
            "type": "choice",
            "instructions": (
                "Given the rules and any flagged ambiguities, should the agent generate "
                "the scenario set now, or request human clarification first?"
            ),
            "criteria": {
                "GENERATE_TESTS": (
                    "The six business rules are clear enough to generate scenarios now; "
                    "no outstanding ambiguity would change what a test must assert."
                ),
                "REQUEST_CLARIFICATION": (
                    "At least one rule leaves a genuine ambiguity that would change what "
                    "a test must assert; stop and ask a human to resolve it before "
                    "generating scenarios."
                ),
            },
        }
        return questions

    def build_state(state: Any) -> dict:
        return {
            "rules": [
                {"rule_id": r["rule_id"], "source_excerpt": r["source_excerpt"], "meaning": r["meaning"]}
                for r in _rules()
            ]
        }

    def resolve(verdicts: dict[str, Verdict]) -> Proceed | Deny:
        flagged = [rid for rid, v in verdicts.items() if rid.startswith("ambiguity_") and v.answer is True]
        strategy = verdicts["strategy_action"].answer
        if flagged or strategy == "REQUEST_CLARIFICATION":
            controller.set_workflow_state(
                "AWAITING_CLARIFICATION", stage="AWAITING_CLARIFICATION", busy=False
            )
            return Deny(
                reason=(
                    f"AWAITING_CLARIFICATION: flagged={flagged}, strategy={strategy}; "
                    "do not call further tools"
                )
            )
        return Proceed()

    return CheckpointSpec(build_questions=build_questions, resolve=resolve, build_state=build_state)


# ---------------------------------------------------------------------------
# save_scenario_set: COVERAGE + GROUNDING
# ---------------------------------------------------------------------------


def build_coverage_checkpoint(
    attempt_log: AttemptLog, catalog: dict, controller: RunController, holder: dict
) -> CheckpointSpec:
    """`holder` is written by build_questions/build_state and read by resolve()
    -- decider_gate calls build_questions, then build_state, then (on the
    AUTO path) resolve, synchronously within one before_tool_call invocation,
    so sharing state through this plain dict is safe. holder["remediation_feedback"]
    is the explicit flag orchestration.py checks immediately after the
    triggering agent() call returns -- never inferred from decisions.json.
    """

    def _compute() -> tuple[dict, dict]:
        scenario_set = attempt_log.get_result("generate_scenarios")
        coverage = compute_coverage(
            scenario_set["scenarios"], catalog, scenario_set_version=scenario_set["version"]
        )
        holder["scenario_set"], holder["coverage"] = scenario_set, coverage
        return scenario_set, coverage

    def build_questions(state: Any) -> dict:
        scenario_set, coverage = _compute()
        interpretation = attempt_log.get_result("interpret_requirement")
        rules_by_id = {r["rule_id"]: r for r in interpretation["rules"]}
        scenarios_by_id = {s["scenario_id"]: s for s in scenario_set["scenarios"]}

        questions: dict = {}
        for row in coverage["rows"]:
            if row["status"] == "MISSING":
                continue
            scenario = scenarios_by_id[row["scenario_id"]]
            rule_meanings = "; ".join(
                rules_by_id[rid]["meaning"] for rid in row["rule_ids"] if rid in rules_by_id
            )
            assertions = "; ".join(
                f"{a['observation']} {a['operator']} {a['expected']!r}"
                for a in scenario["expected_assertions"]
            )
            questions[f"grounding_{row['scenario_id']}"] = {
                "type": "noul",
                "instructions": (
                    f"{row['scenario_id']} ('{scenario['title']}') is linked to rule(s): "
                    f"{rule_meanings}. Its structured oracle requires: {assertions}. Does "
                    f"{row['scenario_id']}'s Given/When/Then explicitly assert this same "
                    "outcome -- no weaker, missing, or additional claim?"
                ),
                "criteria": {
                    "true": (
                        f"{row['scenario_id']}'s Gherkin/prose asserts exactly the outcome its "
                        "structured oracle (expected_assertions) and linked rule(s) require -- "
                        "nothing extra, nothing missing, nothing weaker."
                    ),
                    "false": (
                        f"{row['scenario_id']}'s Gherkin/prose diverges from its structured "
                        "oracle or linked rule(s) -- it asserts something different, weaker, "
                        "or additional to what is actually required."
                    ),
                },
            }
        questions["coverage_action"] = {
            "type": "choice",
            "instructions": (
                f"{coverage['covered']}/{coverage['required']} required scenarios are VALID "
                "per the validator matrix. Accept coverage, or remediate the gap?"
            ),
            "criteria": {
                "ACCEPT_COVERAGE": (
                    "All required scenarios S01-S05 are VALID and grounded; the set is "
                    "complete and ready for human review."
                ),
                "REMEDIATE_GAP": (
                    "At least one required scenario is MISSING or INVALID per the validator "
                    "matrix; regenerate only the missing scenario once, leaving valid "
                    "scenarios unchanged."
                ),
            },
        }
        return questions

    def build_state(state: Any) -> dict:
        scenario_set, coverage = holder["scenario_set"], holder["coverage"]
        interpretation = attempt_log.get_result("interpret_requirement")
        rules_by_id = {r["rule_id"]: r for r in interpretation["rules"]}
        scenarios_by_id = {s["scenario_id"]: s for s in scenario_set["scenarios"]}

        per_scenario = []
        for row in coverage["rows"]:
            scenario = scenarios_by_id.get(row["scenario_id"])
            per_scenario.append(
                {
                    "scenario_id": row["scenario_id"],
                    "status": row["status"],
                    "rule_ids": row["rule_ids"],
                    "rules": [
                        {"rule_id": rid, "meaning": rules_by_id[rid]["meaning"]}
                        for rid in row["rule_ids"]
                        if rid in rules_by_id
                    ],
                    "preconditions": scenario["preconditions"] if scenario else None,
                    "action_codes": scenario["action_codes"] if scenario else None,
                    "expected_assertions": scenario["expected_assertions"] if scenario else None,
                    "gherkin": scenario["gherkin"] if scenario else None,
                }
            )
        missing = [
            {"scenario_id": r["scenario_id"], "rule_ids": r["rule_ids"]}
            for r in coverage["rows"]
            if r["status"] != "VALID"
        ]
        return {
            "scenarios": per_scenario,
            "validator_matrix": {
                "covered": coverage["covered"],
                "required": coverage["required"],
                "missing": missing,
            },
        }

    def resolve(verdicts: dict[str, Verdict]) -> Proceed | Deny:
        coverage, scenario_set = holder["coverage"], holder["scenario_set"]
        controller.record_grounding(coverage["scenario_set_version"], grounding_items(coverage, verdicts))
        holder["remediation_feedback"] = None  # always reset so a stale flag can never linger
        action = verdicts["coverage_action"].answer

        if action == "ACCEPT_COVERAGE":
            if not full_coverage(coverage):  # deterministic, NEVER overridable
                return Deny(
                    reason=(
                        f"ACCEPT_COVERAGE denied: {coverage['covered']}/{coverage['required']} "
                        "valid; 5/5 required"
                    )
                )
            controller.save_evidence("scenarios_accepted.json", scenario_set)
            controller.save_evidence("coverage_accepted.json", coverage)
            return Proceed()

        # REMEDIATE_GAP
        if controller.use_remediation():
            controller.save_evidence(f"scenarios_v{coverage['scenario_set_version']}.json", scenario_set)
            controller.save_evidence(f"coverage_v{coverage['scenario_set_version']}.json", coverage)
            missing = [r["scenario_id"] for r in coverage["rows"] if r["status"] != "VALID"]
            holder["remediation_feedback"] = remediation_feedback(missing, coverage)
            # Deny, never Guide: see module docstring for why.
            return Deny(
                reason=(
                    "REMEDIATION_SCHEDULED: do not call further tools; "
                    "the controller will start remediation"
                )
            )
        controller.set_workflow_state("BLOCKED", stage="BLOCKED", busy=False)
        return Deny(reason="REMEDIATE_GAP requested a second time in this run; BLOCKED.")

    return CheckpointSpec(build_questions=build_questions, resolve=resolve, build_state=build_state)


# ---------------------------------------------------------------------------
# Assembly: DeciderGuardrail inputs + audit hooks
# ---------------------------------------------------------------------------


def _checkpoint_name(tool_name: str) -> str:
    return {
        "generate_scenarios": "AMBIGUITY+STRATEGY",
        "save_scenario_set": "COVERAGE",
    }.get(tool_name, tool_name)


def _decision_record(
    tool_name: str,
    verdicts: dict[str, Verdict],
    action: Any,
    controller: RunController,
    decider_cfg: dict,
) -> dict:
    escalated = [v for v in verdicts.values() if v.route == "ESCALATE"]
    primary = escalated[0] if escalated else next(iter(verdicts.values()), None)
    selected_action = getattr(action, "feedback", None) or getattr(action, "reason", None) or type(action).__name__
    return {
        "checkpoint": _checkpoint_name(tool_name),
        "workflow_state": controller.workflow_state,
        "selected_action": selected_action,
        "decider_suggestion": primary.suggestion if primary else None,
        "confidence": primary.confidence if primary else None,
        "option_probabilities": primary.probabilities if primary else None,
        "threshold": decider_cfg.get("choice_threshold") if primary and primary.probabilities and len(primary.probabilities) > 2 else decider_cfg.get("noul_upper"),
        "route": "ESCALATED" if escalated else "AUTO",
        "decision_source": "DECIDER",
        "latency_ms": primary.latency_ms if primary else None,
        "host_label": primary.host_label if primary else decider_cfg.get("host_label"),
        "model_revision": primary.model if primary else decider_cfg.get("model"),
        "rationale": f"{tool_name} verdicts: " + ", ".join(f"{n}={v.route}" for n, v in verdicts.items()),
        "rationale_origin": "CONTROLLER_EVIDENCE",
        "evidence_references": [],
        "decision_status": "VALIDATED",
        "native_response_ref": None,
        "rejection_reason": None,
    }


def _decision_record_human(
    pending: dict, human_choice: str, *, status: str, rejection_reason: str | None = None
) -> dict:
    tool_name = pending["tool_name"]
    verdict_suggestions = pending.get("verdicts", {})
    return {
        "checkpoint": _checkpoint_name(tool_name),
        "workflow_state": None,
        "selected_action": human_choice,
        "decider_suggestion": verdict_suggestions,
        "confidence": None,
        "option_probabilities": None,
        "threshold": None,
        "route": "ESCALATED",
        "decision_source": "HUMAN_ESCALATION",
        "latency_ms": None,
        "host_label": None,
        "model_revision": None,
        "rationale": f"human picked {human_choice!r} for {tool_name}",
        "rationale_origin": "CONTROLLER_EVIDENCE",
        "evidence_references": [],
        "decision_status": status,
        "native_response_ref": None,
        "rejection_reason": rejection_reason,
    }


def build_all(
    attempt_log: AttemptLog, catalog: dict, controller: RunController, decider_cfg: dict, holder: dict
):
    client = DeciderClient(
        url=decider_cfg["url"],
        timeout_s=decider_cfg["timeouts"]["call_s_cpu"],
        connect_timeout_s=decider_cfg["timeouts"]["connect_s"],
    )
    gate = ConfidenceGate(
        choice_threshold=decider_cfg["choice_threshold"],
        noul_upper=decider_cfg["noul_upper"],
        noul_lower=decider_cfg["noul_lower"],
    )
    checkpoints = {
        "generate_scenarios": build_generate_scenarios_checkpoint(attempt_log, controller),
        "save_scenario_set": build_coverage_checkpoint(attempt_log, catalog, controller, holder),
    }

    def on_decision(tool_name: str, verdicts: dict[str, Verdict], action: Any) -> None:
        controller.record_decision(_decision_record(tool_name, verdicts, action, controller, decider_cfg))
        if isinstance(action, Confirm):
            pending_ctx = dict(holder) if tool_name == "save_scenario_set" else {}
            controller.set_pending_decision(
                {
                    "tool_name": tool_name,
                    "context": pending_ctx,
                    "verdicts": {name: v.suggestion for name, v in verdicts.items()},
                }
            )
            controller.set_workflow_state(
                controller.workflow_state, stage="AWAITING_DECISION_CONFIRMATION", busy=False
            )

    def on_error(tool_name: str, exc: Exception) -> None:
        controller.save_evidence("decider_error.json", {"tool_name": tool_name, "error": str(exc)})
        controller.set_workflow_state("STOPPED", stage="STOPPED", busy=False)

    return client, gate, checkpoints, on_decision, on_error


def apply_human_decision(pending: dict, human_choice: str, controller: RunController) -> tuple[str, str | None]:
    """Applies the same pure guard functions resolve() uses, for a human's pick
    on an escalated decision. Returns (outcome, feedback_or_None).

    outcome is one of: "PROCEED", "REMEDIATION_SCHEDULED", "BLOCKED",
    "AWAITING_CLARIFICATION", "REJECTED_RETRY" (the only outcome that leaves
    the decision open for another pick -- see orchestration.resume_pending_decision).
    """
    tool_name = pending["tool_name"]
    if tool_name == "save_scenario_set":
        coverage, scenario_set = pending["context"]["coverage"], pending["context"]["scenario_set"]
        if human_choice == "ACCEPT_COVERAGE":
            if not full_coverage(coverage):
                controller.record_decision(
                    _decision_record_human(
                        pending, human_choice, status="REJECTED", rejection_reason="5/5 required"
                    )
                )
                return "REJECTED_RETRY", None
            controller.save_evidence("scenarios_accepted.json", scenario_set)
            controller.record_decision(_decision_record_human(pending, human_choice, status="VALIDATED"))
            return "PROCEED", None
        if human_choice == "REMEDIATE_GAP":
            controller.record_decision(_decision_record_human(pending, human_choice, status="VALIDATED"))
            if controller.use_remediation():
                controller.save_evidence(
                    f"scenarios_v{coverage['scenario_set_version']}.json", scenario_set
                )
                missing = [r["scenario_id"] for r in coverage["rows"] if r["status"] != "VALID"]
                return "REMEDIATION_SCHEDULED", remediation_feedback(missing, coverage)
            return "BLOCKED", None
        raise ValueError(f"unrecognized human_choice {human_choice!r} for save_scenario_set")
    if tool_name == "generate_scenarios":
        controller.record_decision(_decision_record_human(pending, human_choice, status="VALIDATED"))
        if human_choice == "GENERATE_TESTS":
            return "PROCEED", None
        if human_choice == "REQUEST_CLARIFICATION":
            return "AWAITING_CLARIFICATION", None
        raise ValueError(f"unrecognized human_choice {human_choice!r} for generate_scenarios")
    raise ValueError(f"unrecognized pending tool {tool_name!r}")
