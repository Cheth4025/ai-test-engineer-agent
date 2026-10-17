"""Strands tools that carry Bedrock's generated content for F02/F03.

Each tool receives the LLM-generated content as its own call arguments (the
normal Strands function-calling pattern) and does only deterministic
validation -- never patches invalid output, per docs/08 Section 6. Every
call, valid or not, is recorded in the caller's AttemptLog; a tool that fails
validation twice in one run raises ToolAttemptsExhausted, and a third attempt
is rejected before validation even runs (see _run_with_attempt_cap). Strands
itself catches every exception a @tool function raises and feeds it back to
the model as a retryable error -- the cap below does not rely on the model
deciding to stop; orchestration.py enforces the actual stop by checking
AttemptLog.get_result() after each agent call.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel
from strands import tool

from ai_test_engineer.coverage import compute_coverage

MAX_TOOL_ATTEMPTS = 2


class BedrockOutputError(ValueError):
    """Raised when Bedrock's tool-call content fails deterministic validation."""


class ToolAttemptsExhausted(BedrockOutputError):
    """Raised when a tool has used its last allowed attempt (or more) in this run."""


class AttemptLog:
    """Per-run record of every tool-call attempt, valid or not, plus the last valid result.

    Not module-level global state: a fresh instance is built per run in
    orchestration.run_initial_pass() and closed over by that run's tool
    instances, so attempts never leak between runs or tests.
    """

    def __init__(self) -> None:
        self._attempts: dict[str, list[dict[str, Any]]] = {}
        self._results: dict[str, dict] = {}

    def next_attempt_number(self, tool_name: str) -> int:
        return len(self._attempts.get(tool_name, [])) + 1

    def count(self, tool_name: str) -> int:
        return len(self._attempts.get(tool_name, []))

    def record_failure(self, tool_name: str, attempt_number: int, error: str) -> None:
        self._attempts.setdefault(tool_name, []).append(
            {"attempt": attempt_number, "valid": False, "error": error}
        )

    def record_success(self, tool_name: str, attempt_number: int, result: dict) -> None:
        self._attempts.setdefault(tool_name, []).append(
            {"attempt": attempt_number, "valid": True, "error": None}
        )
        self._results[tool_name] = result

    def get_result(self, tool_name: str) -> dict | None:
        return self._results.get(tool_name)

    def overwrite_result(self, tool_name: str, result: dict) -> None:
        """Controller-only: replace the cached last-good result without recording
        a fake attempt. Used solely by orchestration.py's remediation merge step,
        to splice the regenerated S04 into the original 4-scenario set under the
        "generate_scenarios" key so save_scenario_set's next call sees the merged set."""
        self._results[tool_name] = result

    def as_dict(self) -> dict[str, list[dict[str, Any]]]:
        return {name: list(attempts) for name, attempts in self._attempts.items()}

    def summary(self) -> dict[str, int]:
        return {name: len(attempts) for name, attempts in self._attempts.items()}


def _run_with_attempt_cap(
    attempt_log: AttemptLog, tool_name: str, validate_fn: Callable[[], dict]
) -> dict:
    attempt_number = attempt_log.next_attempt_number(tool_name)
    if attempt_number > MAX_TOOL_ATTEMPTS:
        raise ToolAttemptsExhausted(
            f"{tool_name} already used its last allowed attempt "
            f"({MAX_TOOL_ATTEMPTS}) in this run; rejecting without validating."
        )

    try:
        result = validate_fn()
    except BedrockOutputError as exc:
        attempt_log.record_failure(tool_name, attempt_number, str(exc))
        if attempt_number >= MAX_TOOL_ATTEMPTS:
            raise ToolAttemptsExhausted(
                f"{tool_name} failed validation on attempt "
                f"{attempt_number}/{MAX_TOOL_ATTEMPTS}: {exc}"
            ) from exc
        raise

    attempt_log.record_success(tool_name, attempt_number, result)
    return result


# ---------------------------------------------------------------------------
# F02 -- requirement interpretation (docs/05_Data_Contracts.md Section 3)
# ---------------------------------------------------------------------------

REQUIRED_RULE_IDS = {f"BR0{n}" for n in range(1, 7)}


class RuleItem(BaseModel):
    rule_id: str
    source_excerpt: str
    meaning: str
    expected_observations: list[str]


class AmbiguityItem(BaseModel):
    id: str
    rule_ids: list[str]
    question: str
    critical: bool
    status: str
    human_response: str | None = None


def _validate_interpretation(
    rules: list[dict], ambiguities: list[dict], requirement_text: str
) -> dict:
    # NOTE: Strands' @tool decorator validates/coerces each list item against
    # RuleItem/AmbiguityItem for schema purposes, but hands the tool function
    # plain dicts (FunctionToolMetadata.validate_input() calls .model_dump()
    # on its way back out) -- confirmed by a live run: accessing rule.rule_id
    # here raised AttributeError: 'dict' object has no attribute 'rule_id'.
    # So this validator, and _validate_scenario_set below, work on dicts.
    rule_ids = [rule["rule_id"] for rule in rules]
    if len(rules) != 6 or set(rule_ids) != REQUIRED_RULE_IDS:
        raise BedrockOutputError(
            f"expected exactly six rules BR01-BR06 with no duplicates, got {rule_ids!r}"
        )

    for rule in rules:
        if rule["source_excerpt"] not in requirement_text:
            raise BedrockOutputError(
                f"{rule['rule_id']} source_excerpt is not a literal substring of the "
                f"requirement text: {rule['source_excerpt']!r}"
            )

    return {
        "rules": [dict(rule) for rule in rules],
        "ambiguities": [dict(ambiguity) for ambiguity in ambiguities],
    }


def build_interpret_requirement_tool(attempt_log: AttemptLog, requirement_text: str):
    @tool
    def interpret_requirement(
        rules: list[RuleItem], ambiguities: list[AmbiguityItem]
    ) -> dict:
        """Record the requirement interpretation: BR01-BR06 linked to source text, plus ambiguities.

        Args:
            rules: Exactly six entries, one per BR01-BR06, each with rule_id,
                source_excerpt (must be an exact substring of the requirement
                text you were given), meaning, and expected_observations.
            ambiguities: Any ambiguities found in the requirement (may be
                empty), each with id, rule_ids, question, critical, status,
                and an optional human_response.
        """

        def _validate() -> dict:
            return _validate_interpretation(rules, ambiguities, requirement_text)

        return _run_with_attempt_cap(attempt_log, "interpret_requirement", _validate)

    return interpret_requirement


# ---------------------------------------------------------------------------
# F03 -- scenario set + Gherkin (docs/05_Data_Contracts.md Section 4)
# ---------------------------------------------------------------------------


class Assertion(BaseModel):
    observation: str
    operator: str
    expected: str | int | bool


class ScenarioItem(BaseModel):
    scenario_id: str
    title: str
    rule_ids: list[str]
    test_id: str
    preconditions: list[str]
    actions: list[str]
    action_codes: list[str]
    expected_assertions: list[Assertion]
    gherkin: str


def _build_omission_event(target_ids: list[str]) -> dict | None:
    if "S04" in target_ids:
        return None
    return {
        "event_type": "SCENARIO_OMISSION",
        "scenario_id": "S04",
        "reason": "deliberately excluded from this generation pass (F03 seeded gap)",
        "target_ids": list(target_ids),
    }


def _validate_scenario_set(
    target_ids: list[str],
    scenarios: list[dict],
    expected_target_ids: list[str],
) -> dict:
    if set(target_ids) != set(expected_target_ids) or len(target_ids) != len(expected_target_ids):
        raise BedrockOutputError(
            f"target_ids must be exactly {expected_target_ids!r}, got {target_ids!r}"
        )

    scenario_ids = [scenario["scenario_id"] for scenario in scenarios]
    if set(scenario_ids) != set(target_ids) or len(scenario_ids) != len(target_ids):
        raise BedrockOutputError(
            f"scenarios must cover exactly target_ids {target_ids!r}, got {scenario_ids!r}"
        )

    return {
        "version": 1,
        "scenarios": [dict(scenario) for scenario in scenarios],
        "omission_event": _build_omission_event(target_ids),
    }


def build_generate_scenarios_tool(attempt_log: AttemptLog, expected_target_ids: list[str]):
    @tool
    def generate_scenarios(
        target_ids: list[str], scenarios: list[ScenarioItem]
    ) -> dict:
        """Write the initial scenario set with embedded, display-only Gherkin.

        Args:
            target_ids: The scenario IDs to generate this pass -- must
                exactly match the IDs the controller requested.
            scenarios: One ScenarioItem per target_id, grounded in the fixed
                catalog's preconditions/action_codes/assertions for that ID,
                with a readable title, ordered actions, and Given/When/Then
                Gherkin. Gherkin is review-only and is never executed.
        """

        def _validate() -> dict:
            return _validate_scenario_set(target_ids, scenarios, expected_target_ids)

        return _run_with_attempt_cap(attempt_log, "generate_scenarios", _validate)

    return generate_scenarios


def build_generate_missing_scenarios_tool(attempt_log: AttemptLog, expected_target_ids: list[str]):
    """A separate tool, under its own name, for the remediation regeneration pass.

    Deliberately not a second configuration of generate_scenarios: reusing
    that tool name would make DeciderGuardrail re-run the AMBIGUITY+STRATEGY
    checkpoint on the remediation call (checkpoints are keyed by tool name) --
    a pointless, risky extra Decider call. This tool is never a checkpoints
    key, so it is always ungated; the COVERAGE recheck on the next
    save_scenario_set call still gates the merged result.
    """

    @tool
    def generate_missing_scenarios(
        target_ids: list[str], scenarios: list[ScenarioItem]
    ) -> dict:
        """Regenerate only the missing/invalid scenario(s) named in the remediation feedback.

        Args:
            target_ids: The scenario IDs to regenerate -- must exactly match
                the IDs the controller requested (the ones the coverage
                validator flagged as missing or invalid).
            scenarios: One ScenarioItem per target_id, same shape as generate_scenarios.
        """

        def _validate() -> dict:
            return _validate_scenario_set(target_ids, scenarios, expected_target_ids)

        return _run_with_attempt_cap(attempt_log, "generate_missing_scenarios", _validate)

    return generate_missing_scenarios


# ---------------------------------------------------------------------------
# F04 -- coverage scoring + persistence (docs/05_Data_Contracts.md Section 5)
# ---------------------------------------------------------------------------


def build_save_scenario_set_tool(attempt_log: AttemptLog, catalog: dict):
    """Zero-argument tool: scores and persists whatever generate_scenarios last
    produced, read straight from the shared AttemptLog. Avoids having the
    model re-transcribe the whole scenario JSON as a second tool call's
    arguments (risk of truncation/paraphrase drift). The COVERAGE checkpoint's
    build_questions/build_state read the same AttemptLog, so both see
    identically the same data by construction.
    """

    @tool
    def save_scenario_set() -> dict:
        """Score and persist the scenario set most recently produced by generate_scenarios.

        Takes no arguments. Call this immediately after generate_scenarios succeeds.
        """

        def _validate() -> dict:
            scenario_set = attempt_log.get_result("generate_scenarios")
            if scenario_set is None:
                raise BedrockOutputError(
                    "save_scenario_set called before generate_scenarios succeeded"
                )
            coverage = compute_coverage(
                scenario_set["scenarios"], catalog, scenario_set_version=scenario_set["version"]
            )
            return {"scenario_set": scenario_set, "coverage": coverage}

        return _run_with_attempt_cap(attempt_log, "save_scenario_set", _validate)

    return save_scenario_set
