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

import os
from typing import Any

from decider_gate.client import DeciderClient
from decider_gate.gate import ConfidenceGate, Verdict
from decider_gate.intervention import CheckpointSpec
from strands.interventions import Confirm, Deny, InterventionHandler, Proceed

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


def _br_text_for_rule(requirement_text: str, rule_id: str) -> str:
    """The verbatim "BR0N: ..." line from requirement.json's text -- never
    paraphrased, matching how ui.py's parse_business_rules reads the same
    source."""
    for line in requirement_text.splitlines():
        line = line.strip()
        if line.startswith(f"{rule_id}:"):
            return line
    return rule_id


def failure_facts(results: dict, requirement_text: str) -> dict:
    """Deterministic, no-Bedrock facts for the FAILURE checkpoint -- which
    test (if any) failed, its real expected/actual evidence from
    execution.run_approved_tests' own results.json, and the BR text it's
    linked to. Shared by tools.build_finalize_analysis_tool (so the tool's
    own attempt_log result is meaningful, same spirit as save_scenario_set
    independently computing coverage) and this module's own
    build_failure_checkpoint (which recomputes it from the canonical
    attempt_log result rather than trusting the tool's return value, same
    pattern as build_coverage_checkpoint's _compute())."""
    failed = [t for t in results["tests"] if t["status"] in ("FAIL", "ERROR")]
    if not failed:
        return {"has_failure": False}
    test = failed[0]  # this fixed five-test suite has exactly one failure mode per run
    rule_id = test["rule_ids"][0] if test["rule_ids"] else None
    return {
        "has_failure": True,
        "test_id": test["test_id"],
        "rule_ids": test["rule_ids"],
        "rule_text": _br_text_for_rule(requirement_text, rule_id) if rule_id else None,
        "expected": test["expected"],
        "actual": test["actual"],
        "failure_message": test["failure_message"],
        "evidence_reference": test["evidence_reference"],
    }


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
    def _interpretation() -> dict:
        return attempt_log.get_result("interpret_requirement")

    def _rules() -> list[dict]:
        return _interpretation()["rules"]

    def _ambiguity_summary() -> str:
        ambiguities = _interpretation()["ambiguities"]
        if not ambiguities:
            return "none identified"
        return "; ".join(
            f"{a['id']} (critical={a['critical']}): {a['question']}" for a in ambiguities
        )

    def _ambiguities_for_rule(rule_id: str) -> list[dict]:
        return [
            a for a in _interpretation()["ambiguities"] if a["rule_id"] in (rule_id, "GENERAL")
        ]

    def _ambiguity_lines_for_rule(rule_id: str) -> str:
        rule_ambiguities = _ambiguities_for_rule(rule_id)
        if not rule_ambiguities:
            return "No ambiguities recorded for this rule."
        lines = [
            f"{a['id']} (critical): {a['question']}"
            if a["critical"]
            else (
                f"{a['id']} (non-critical): {a['question']} -- recorded, out of scope for "
                "S01-S05, does not change any assertion"
            )
            for a in rule_ambiguities
        ]
        return "Known ambiguities for this rule: " + "; ".join(lines)

    def build_questions(state: Any) -> dict:
        questions = {
            f"ambiguity_{rule['rule_id']}": {
                "type": "noul",
                "instructions": (
                    f"Rule {rule['rule_id']}: {rule['meaning']}. "
                    f"{_ambiguity_lines_for_rule(rule['rule_id'])}"
                ),
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
                f"Interpretation's recorded ambiguities: {_ambiguity_summary()}. Given the rules "
                "and these ambiguities, should the agent generate the scenario set now, or "
                "request human clarification first?"
            ),
            "criteria": {
                "GENERATE_TESTS": (
                    "Interpretation identified no critical ambiguity and every rule BR01-BR06 "
                    "states a concrete, testable outcome; generate scenarios now."
                ),
                "REQUEST_CLARIFICATION": (
                    "Interpretation lists at least one critical ambiguity, or a rule's expected "
                    "outcome cannot be asserted without a human answer."
                ),
            },
        }
        return questions

    def build_state(state: Any) -> dict:
        interpretation = _interpretation()
        ambiguities = interpretation["ambiguities"]
        if not ambiguities:
            ambiguities_state: Any = "none identified"
        else:
            ambiguities_state = [
                {
                    **a,
                    "scope_note": (
                        None
                        if a["critical"]
                        else "recorded, out of scope for S01-S05, does not change any assertion"
                    ),
                }
                for a in ambiguities
            ]
        return {
            "rules": [
                {"rule_id": r["rule_id"], "source_excerpt": r["source_excerpt"], "meaning": r["meaning"]}
                for r in _rules()
            ],
            "ambiguities": ambiguities_state,
        }

    def resolve(verdicts: dict[str, Verdict]) -> Proceed | Deny:
        flagged = [rid for rid, v in verdicts.items() if rid.startswith("ambiguity_") and v.answer is True]
        strategy = verdicts["strategy_action"].answer
        if flagged or strategy == "REQUEST_CLARIFICATION":
            controller.set_workflow_state(
                "AWAITING_CLARIFICATION", stage="AWAITING_CLARIFICATION", busy=False
            )
            reason = (
                f"AWAITING_CLARIFICATION: flagged={flagged}, strategy={strategy}; "
                "do not call further tools"
            )
            controller.set_halt(reason)
            return Deny(reason=reason)
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
        # Persisted unconditionally, on every save_scenario_set scoring call
        # -- AUTO or escalated, ACCEPT/REMEDIATE/BLOCKED -- never only on the
        # first successful remediation. A run must show WHY a row was
        # INVALID even when the outcome that follows is BLOCKED/REJECTED.
        controller.save_evidence(f"scenarios_v{scenario_set['version']}.json", scenario_set)
        controller.save_evidence(f"coverage_v{coverage['scenario_set_version']}.json", coverage)
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
            # Self-contained by design: this scenario's own Gherkin + oracle +
            # linked rule text are embedded directly in ITS instructions,
            # never left to the shared COVERAGE state alone -- a spike found
            # single-scenario grounding scored 0.88 on a correct S04 vs ~0.77
            # relying on shared state (see module docstring / review notes).
            questions[f"grounding_{row['scenario_id']}"] = {
                "type": "noul",
                "instructions": (
                    f"{row['scenario_id']} ('{scenario['title']}') is linked to rule(s): "
                    f"{rule_meanings}. Its structured oracle requires: {assertions}. Its "
                    f"Given/When/Then Gherkin is:\n{scenario['gherkin']}\n\n"
                    f"Does {row['scenario_id']}'s Given/When/Then above explicitly assert "
                    "this same outcome -- no weaker, missing, or additional claim?"
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
        """Shared COVERAGE state carries ONLY the validator matrix -- status
        per S01-S05, covered/required, missing IDs + rules. Each grounding
        noul's own instructions (see build_questions above) already carry
        that scenario's full Gherkin/oracle/rule text; duplicating it here
        in a shared blob measured worse (see build_questions' comment)."""
        coverage = holder["coverage"]
        missing = [
            {"scenario_id": r["scenario_id"], "rule_ids": r["rule_ids"]}
            for r in coverage["rows"]
            if r["status"] != "VALID"
        ]
        return {
            "validator_matrix": {
                "covered": coverage["covered"],
                "required": coverage["required"],
                "rows": [
                    {"scenario_id": r["scenario_id"], "status": r["status"]} for r in coverage["rows"]
                ],
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

        # REMEDIATE_GAP -- scenarios_vN.json/coverage_vN.json are already
        # persisted (see _compute()), on this and every other outcome alike.
        if controller.use_remediation():
            missing = [r["scenario_id"] for r in coverage["rows"] if r["status"] != "VALID"]
            holder["remediation_feedback"] = remediation_feedback(missing, coverage)
            reason = (
                "REMEDIATION_SCHEDULED: do not call further tools; "
                "the controller will start remediation"
            )
            controller.set_halt(reason)
            # Deny, never Guide: see module docstring for why.
            return Deny(reason=reason)
        controller.set_workflow_state("BLOCKED", stage="BLOCKED", busy=False)
        reason = "REMEDIATE_GAP requested a second time in this run; BLOCKED."
        controller.set_halt(reason)
        return Deny(reason=reason)

    return CheckpointSpec(build_questions=build_questions, resolve=resolve, build_state=build_state)


# ---------------------------------------------------------------------------
# execute_tests: EXECUTING -- ungated (pytest is never a Decider action,
# docs/04 Section 5). No checkpoint here; see orchestration.STAGE_TOOLS.
# ---------------------------------------------------------------------------


def _analysis_record(facts: dict, classification: str, handling_action: str) -> dict:
    return {
        "has_failure": True,
        "test_id": facts["test_id"],
        "rule_ids": facts["rule_ids"],
        "classification": classification,
        "classification_decision_ref": "decisions.json",
        "observations": [
            f"{facts['test_id']} expected {facts['expected']}, observed {facts['actual']}.",
            facts["failure_message"] or "(no failure message captured)",
        ],
        "expected_behavior": facts["rule_text"],
        "actual_behavior": f"Observed {facts['actual']} instead of {facts['expected']}.",
        "impact": [
            "Books/ledger overstate the payment for this logical charge.",
            "Reconciliation against the real gateway charge count will disagree.",
            "Risk of an incorrect refund or a duplicate shipment downstream.",
        ],
        "evidence_references": [facts["evidence_reference"]] if facts.get("evidence_reference") else [],
        "missing_evidence": [] if facts.get("evidence_reference") else ["per-test evidence file not found"],
        "handling_action": handling_action,
        "investigated": handling_action == "INVESTIGATE_FAILURE",
    }


# ---------------------------------------------------------------------------
# finalize_analysis: ANALYZING_RESULTS -- FAILURE checkpoint. Only ever
# called (per orchestration._continue_to_analysis) when execute_tests
# recorded at least one real FAIL/ERROR -- no "no failure" branch needed
# here; that case skips this checkpoint entirely and goes straight to
# REPORTING with a static analysis.json. No Bedrock call for the narrative
# (deterministic, from failure_facts only) -- only the Decider's bounded
# classify+action choice.
# ---------------------------------------------------------------------------


def build_failure_checkpoint(
    attempt_log: AttemptLog, controller: RunController, requirement_text: str, holder: dict
) -> CheckpointSpec:
    def _compute() -> dict:
        results = attempt_log.get_result("execute_tests")
        facts = failure_facts(results, requirement_text)
        holder["failure_facts"] = facts
        return facts

    def build_questions(state: Any) -> dict:
        facts = _compute()
        return {
            "failure_class": {
                "type": "choice",
                "instructions": (
                    f"{facts['test_id']} failed. Expected: {facts['expected']}. Actual: "
                    f"{facts['actual']}. Failure message: {facts['failure_message']!r}. "
                    f"Linked rule: {facts['rule_text']!r}. Classify this failure."
                ),
                "criteria": {
                    "BUSINESS_RULE_VIOLATION": (
                        f"The real recorded expected-vs-actual evidence shows the linked "
                        "rule was actually violated -- not a runner/tooling problem."
                    ),
                    "INFRASTRUCTURE_ERROR": (
                        "The failure is a runner/tooling problem (crash, missing fixture, "
                        "setup error), not a real business-rule assertion failure."
                    ),
                    "INSUFFICIENT_EVIDENCE": (
                        "The available evidence does not clearly show whether a business "
                        "rule was violated."
                    ),
                },
            },
            "failure_action": {
                "type": "choice",
                "instructions": (
                    "Given this failure, should the agent investigate further (one bounded, "
                    "read-only pass over the existing evidence) or report the defect now? "
                    "The agent never fixes code, never retries to green, and never reruns "
                    "the suite."
                ),
                "criteria": {
                    "REPORT_FAILURE": (
                        "The evidence already clearly supports the classification and "
                        "impact; report the defect now."
                    ),
                    "INVESTIGATE_FAILURE": (
                        "One bounded, read-only look at the already-captured evidence would "
                        "strengthen the report before it is finalized."
                    ),
                },
            },
        }

    def build_state(state: Any) -> dict:
        return {"failure_facts": holder["failure_facts"]}

    def resolve(verdicts: dict[str, Verdict]) -> Proceed | Deny:
        facts = holder["failure_facts"]
        failure_class = verdicts["failure_class"].answer
        action = verdicts["failure_action"].answer
        if failure_class == "BUSINESS_RULE_VIOLATION" and not facts["has_failure"]:
            # Deterministic, never overridable (docs/05 Section 11: "controller
            # rejects BUSINESS_RULE_VIOLATION when no failed assertion exists").
            return Deny(reason="BUSINESS_RULE_VIOLATION denied: no failed test recorded in results.json")
        if action == "INVESTIGATE_FAILURE":
            controller.use_investigation()
        controller.save_evidence("analysis.json", _analysis_record(facts, failure_class, action))
        return Proceed()

    return CheckpointSpec(build_questions=build_questions, resolve=resolve, build_state=build_state)


# ---------------------------------------------------------------------------
# render_report: REPORTING -- COMPLETION checkpoint. A YES never overrides a
# missing artifact (docs/04 Section 3 / docs/05 Section 6) -- resolve()'s
# readiness guard runs regardless of what the Decider/human answered.
# ---------------------------------------------------------------------------


def build_completion_checkpoint(attempt_log: AttemptLog, controller: RunController, holder: dict) -> CheckpointSpec:
    def _compute() -> dict:
        ready = {
            "results_exists": (controller.run_dir / "results.json").exists(),
            "analysis_exists": (controller.run_dir / "analysis.json").exists(),
        }
        holder["completion_ready"] = ready
        return ready

    def build_questions(state: Any) -> dict:
        ready = _compute()
        return {
            "completion_action": {
                "type": "choice",
                "instructions": (
                    f"results.json present: {ready['results_exists']}. analysis.json "
                    f"present: {ready['analysis_exists']}. Is the evidence complete and "
                    "consistent enough to finalize the report now?"
                ),
                "criteria": {
                    "FINALIZE_RESULTS": (
                        "All required evidence (results.json, and analysis.json if a test "
                        "failed) is present and consistent; finalize now."
                    ),
                    "NOT_READY": "Required evidence is missing or inconsistent; do not finalize yet.",
                },
            }
        }

    def build_state(state: Any) -> dict:
        return {"completion_ready": holder["completion_ready"]}

    def resolve(verdicts: dict[str, Verdict]) -> Proceed | Deny:
        ready = holder["completion_ready"]
        action = verdicts["completion_action"].answer
        if action != "FINALIZE_RESULTS":
            controller.set_workflow_state("BLOCKED", stage="BLOCKED", busy=False)
            reason = "COMPLETION checkpoint: evidence not ready to finalize; BLOCKED."
            controller.set_halt(reason)
            return Deny(reason=reason)
        if not ready["results_exists"]:
            return Deny(reason="FINALIZE_RESULTS denied: results.json missing")
        if not ready["analysis_exists"]:
            return Deny(reason="FINALIZE_RESULTS denied: analysis.json missing")
        from ai_test_engineer import reporting  # local import: reporting imports nothing from here

        report = reporting.render_report(controller)
        controller.save_evidence("report_metadata.json", report["metadata"])
        controller.set_workflow_state("COMPLETED", stage="COMPLETED", busy=False)
        return Proceed()

    return CheckpointSpec(build_questions=build_questions, resolve=resolve, build_state=build_state)


class StageGuard(InterventionHandler):
    """Registered ahead of HaltGuard/DeciderGuardrail: denies any tool not
    allowed at the run's current stage (docs/03 Section 3: "Tools available
    to the agent are limited per stage by the controller"), without ever
    consulting Decider. A model that calls generate_scenarios/
    save_scenario_set early -- e.g. during the INTERPRETING stage, before
    orchestration has asked for it -- is denied right here; Decider is never
    reached, so there's no verdict to escalate or resolve, and no decision
    record. The denial is recorded as a stage_violation evidence event
    instead (see RunController.record_stage_violation).
    """

    def __init__(self, controller: RunController, name: str = "stage-guard") -> None:
        self._controller = controller
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def before_tool_call(self, event: Any, **kwargs: Any) -> Proceed | Deny:
        allowed = self._controller.allowed_tools
        if allowed is None:
            return Proceed()
        tool_name = event.tool_use["name"]
        if tool_name in allowed:
            return Proceed()
        reason = (
            f"{tool_name} is not available at stage {self._controller.tool_stage}; "
            f"call only {sorted(allowed)}"
        )
        self._controller.record_stage_violation(tool_name, reason)
        self._controller.record_tool_call(
            tool_name, event.tool_use.get("toolUseId"), self._controller.tool_stage, self._name, "DENY"
        )
        return Deny(reason=reason)


class HaltGuard(InterventionHandler):
    """Registered ahead of DeciderGuardrail: denies every tool call while
    controller.halted, without ever reaching Decider.

    Strands keeps running the model within the same agent() turn after a tool
    call is denied (the model sees the error and may just retry); without
    this, a model that retries generate_scenarios/save_scenario_set after a
    decision already scheduled remediation/clarification/block gets a fresh
    Decider call and can consume the single-use remediation flag a second
    time before orchestration._drive_recheck ever runs. This handler makes
    that retry a no-op Deny instead.
    """

    def __init__(self, controller: RunController, name: str = "controller-halt") -> None:
        self._controller = controller
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def before_tool_call(self, event: Any, **kwargs: Any) -> Proceed | Deny:
        if self._controller.halted:
            reason = self._controller.halt_reason or "HALTED: do not call further tools"
            self._controller.record_tool_call(
                event.tool_use["name"], event.tool_use.get("toolUseId"), self._controller.tool_stage, self._name, "DENY"
            )
            return Deny(reason=reason)
        return Proceed()


# ---------------------------------------------------------------------------
# Assembly: DeciderGuardrail inputs + audit hooks
# ---------------------------------------------------------------------------


def _checkpoint_name(tool_name: str) -> str:
    return {
        "generate_scenarios": "AMBIGUITY+STRATEGY",
        "save_scenario_set": "COVERAGE",
        "finalize_analysis": "FAILURE",
        "render_report": "COMPLETION",
    }.get(tool_name, tool_name)


def resolve_host_label(decider_cfg: dict) -> str:
    """AITE_DECIDER_HOST_LABEL env var wins when set and non-empty; otherwise
    falls back to config/decider.json's host_label. Lets the same config file
    serve a local-cpu dev run and an EC2 GPU run (AITE_DECIDER_HOST_LABEL=ec2-g4dn-t4)
    without editing the file."""
    return os.environ.get("AITE_DECIDER_HOST_LABEL") or decider_cfg["host_label"]


def _per_question_from_verdicts(verdicts: dict[str, Verdict]) -> list[dict]:
    """Full per-question audit: every question's answer/suggestion/confidence/
    route/probabilities, not just the primary (first-escalated-or-first)
    verdict _decision_record's other fields summarize."""
    return [
        {
            "question": name,
            "answer": v.answer,
            "suggestion": v.suggestion,
            "confidence": v.confidence,
            "probabilities": v.probabilities,
            "route": v.route,
        }
        for name, v in verdicts.items()
    ]


def _per_question_from_pending(pending_verdicts: dict[str, dict]) -> list[dict]:
    """Same shape as _per_question_from_verdicts, built from the richer
    per-question dicts on_decision stores on controller.pending_decision
    (captured at escalation time, since Verdict objects themselves are not
    JSON-serializable and do not survive a pause)."""
    return [{"question": name, **fields} for name, fields in pending_verdicts.items()]


def _decision_record(
    tool_name: str,
    verdicts: dict[str, Verdict],
    action: Any,
    controller: RunController,
    decider_cfg: dict,
    host_label: str,
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
        "host_label": primary.host_label if primary else host_label,
        "model_revision": primary.model if primary else decider_cfg.get("model"),
        "rationale": f"{tool_name} verdicts: " + ", ".join(f"{n}={v.route}" for n, v in verdicts.items()),
        "rationale_origin": "CONTROLLER_EVIDENCE",
        "evidence_references": [],
        "decision_status": "VALIDATED",
        "native_response_ref": None,
        "rejection_reason": None,
        "per_question": _per_question_from_verdicts(verdicts),
    }


def _decision_record_human(
    pending: dict, human_choice: str, *, status: str, rejection_reason: str | None = None
) -> dict:
    tool_name = pending["tool_name"]
    per_question_fields = pending.get("verdicts", {})
    return {
        "checkpoint": _checkpoint_name(tool_name),
        "workflow_state": None,
        "selected_action": human_choice,
        "decider_suggestion": {name: fields["suggestion"] for name, fields in per_question_fields.items()},
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
        "per_question": _per_question_from_pending(per_question_fields),
    }


def decision_record_reused(
    tool_name: str,
    verdicts: dict[str, Verdict],
    action: Any,
    origin_tool_use_id: str | None,
    controller: RunController,
    decider_cfg: dict,
    host_label: str,
) -> dict:
    """A later toolUseId (a retry) reused a prior toolUseId's resolved decision
    -- see decider_gate.intervention's "Decision reuse across toolUseIds".
    Same shape as a fresh DECIDER/HUMAN_ESCALATION record, so decisions.json
    stays one consistent audit trail, with decision_source=REUSED and a
    pointer (the origin toolUseId -- cross-referenceable against
    tool_calls.json) to the decision this one reused instead of re-asking."""
    record = _decision_record(tool_name, verdicts, action, controller, decider_cfg, host_label)
    record["decision_source"] = "REUSED"
    record["reused_from_tool_use_id"] = origin_tool_use_id
    record["rationale"] = (
        f"reused cached decision for {tool_name} (no new Decider call); "
        f"origin toolUseId={origin_tool_use_id!r}"
    )
    return record


def build_all(
    attempt_log: AttemptLog,
    catalog: dict,
    controller: RunController,
    decider_cfg: dict,
    holder: dict,
    requirement_text: str = "",
):
    host_label = resolve_host_label(decider_cfg)
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
        "finalize_analysis": build_failure_checkpoint(attempt_log, controller, requirement_text, holder),
        "render_report": build_completion_checkpoint(attempt_log, controller, holder),
    }

    def on_decision(tool_name: str, verdicts: dict[str, Verdict], action: Any) -> None:
        controller.record_decision(
            _decision_record(tool_name, verdicts, action, controller, decider_cfg, host_label)
        )
        if isinstance(action, Confirm):
            if tool_name == "save_scenario_set":
                # decider_gate's resolve() -- where build_coverage_checkpoint's
                # own record_grounding call lives -- is never invoked once ANY
                # verdict in this checkpoint escalates (see decider_gate/
                # intervention.py: a Confirm short-circuits before resolve()).
                # Without this, grounding_vN.json would never be written for
                # an escalated COVERAGE checkpoint, silently leaving
                # unresolved_grounding_flags()/approval_blockers() empty.
                # grounding_items() already maps an ESCALATE verdict to
                # NEEDS_REVIEW; holder["coverage"] was set by build_questions'
                # _compute() before Decider was ever asked, so it's always
                # populated by the time any decision (AUTO or escalated) fires.
                coverage = holder["coverage"]
                controller.record_grounding(coverage["scenario_set_version"], grounding_items(coverage, verdicts))
            pending_ctx = (
                dict(holder) if tool_name in ("save_scenario_set", "finalize_analysis", "render_report") else {}
            )
            controller.set_pending_decision(
                {
                    "tool_name": tool_name,
                    "context": pending_ctx,
                    # A stable id for THIS escalation instance, not the run --
                    # controller.decisions already has this escalation's own
                    # DECIDER record appended (the record_decision call just
                    # above), so its length is a monotonically increasing
                    # counter that is the same across every UI refresh while
                    # this exact decision is still pending, and different for
                    # the next escalation. The UI keys its choice radio on
                    # this (not just controller.run_id, which stays constant
                    # across multiple escalations in one run) so a second
                    # escalation gets a fresh, unanswered widget instead of
                    # inheriting state left over from the first one.
                    "escalation_seq": len(controller.decisions),
                    "verdicts": {
                        name: {
                            "answer": v.answer,
                            "suggestion": v.suggestion,
                            "confidence": v.confidence,
                            "probabilities": v.probabilities,
                            "route": v.route,
                        }
                        for name, v in verdicts.items()
                    },
                }
            )
            controller.set_workflow_state(
                controller.workflow_state, stage="AWAITING_DECISION_CONFIRMATION", busy=False
            )

    def on_error(tool_name: str, exc: Exception) -> None:
        controller.save_evidence(
            "decider_error.json", {"tool_name": tool_name, "error": str(exc), "host_label": host_label}
        )
        controller.set_workflow_state("STOPPED", stage="STOPPED", busy=False)

    return client, gate, checkpoints, on_decision, on_error, host_label


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
            # scenarios_vN.json/coverage_vN.json are already persisted (see
            # build_coverage_checkpoint's _compute(), run at escalation time).
            if controller.use_remediation():
                missing = [r["scenario_id"] for r in coverage["rows"] if r["status"] != "VALID"]
                feedback = remediation_feedback(missing, coverage)
                controller.set_halt(f"REMEDIATION_SCHEDULED: {feedback}")
                return "REMEDIATION_SCHEDULED", feedback
            controller.set_workflow_state("BLOCKED", stage="BLOCKED", busy=False)
            controller.set_halt("REMEDIATE_GAP requested a second time in this run; BLOCKED.")
            return "BLOCKED", None
        raise ValueError(f"unrecognized human_choice {human_choice!r} for save_scenario_set")
    if tool_name == "generate_scenarios":
        controller.record_decision(_decision_record_human(pending, human_choice, status="VALIDATED"))
        if human_choice == "GENERATE_TESTS":
            return "PROCEED", None
        if human_choice == "REQUEST_CLARIFICATION":
            controller.set_workflow_state(
                "AWAITING_CLARIFICATION", stage="AWAITING_CLARIFICATION", busy=False
            )
            controller.set_halt("AWAITING_CLARIFICATION: do not call further tools")
            return "AWAITING_CLARIFICATION", None
        raise ValueError(f"unrecognized human_choice {human_choice!r} for generate_scenarios")
    if tool_name == "finalize_analysis":
        facts = pending["context"]["failure_facts"]
        verdict_fields = pending.get("verdicts", {})
        failure_class_fields = verdict_fields.get("failure_class", {})
        failure_class = failure_class_fields.get("answer") or failure_class_fields.get("suggestion")
        if human_choice not in ("INVESTIGATE_FAILURE", "REPORT_FAILURE"):
            raise ValueError(f"unrecognized human_choice {human_choice!r} for finalize_analysis")
        if failure_class == "BUSINESS_RULE_VIOLATION" and not facts["has_failure"]:
            controller.record_decision(
                _decision_record_human(
                    pending, human_choice, status="REJECTED", rejection_reason="no failed test recorded"
                )
            )
            return "REJECTED_RETRY", None
        controller.record_decision(_decision_record_human(pending, human_choice, status="VALIDATED"))
        if human_choice == "INVESTIGATE_FAILURE":
            controller.use_investigation()
        controller.save_evidence("analysis.json", _analysis_record(facts, failure_class, human_choice))
        return "PROCEED", None
    if tool_name == "render_report":
        ready = pending["context"].get("completion_ready", {})
        if human_choice == "FINALIZE_RESULTS":
            if not ready.get("results_exists") or not ready.get("analysis_exists"):
                controller.record_decision(
                    _decision_record_human(
                        pending, human_choice, status="REJECTED", rejection_reason="required evidence missing"
                    )
                )
                return "REJECTED_RETRY", None
            controller.record_decision(_decision_record_human(pending, human_choice, status="VALIDATED"))
            from ai_test_engineer import reporting  # local import: reporting imports nothing from here

            report = reporting.render_report(controller)
            controller.save_evidence("report_metadata.json", report["metadata"])
            controller.set_workflow_state("COMPLETED", stage="COMPLETED", busy=False)
            return "PROCEED", None
        if human_choice == "NOT_READY":
            controller.record_decision(_decision_record_human(pending, human_choice, status="VALIDATED"))
            controller.set_workflow_state("BLOCKED", stage="BLOCKED", busy=False)
            controller.set_halt("COMPLETION checkpoint: human held completion; BLOCKED.")
            return "BLOCKED", None
        raise ValueError(f"unrecognized human_choice {human_choice!r} for render_report")
    raise ValueError(f"unrecognized pending tool {tool_name!r}")
