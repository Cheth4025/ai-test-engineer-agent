"""Generic confidence-gated guardrail wired as a Strands InterventionHandler.

Verified against strands-agents==1.61.0: InterventionHandler.before_tool_call(
self, event: BeforeToolCallEvent, **kwargs) -> Proceed | Deny | Guide | Confirm
| Transform; event.tool_use["name"] / ["input"] carry the tool name/input.

Domain-agnostic: this module knows nothing about payments, checkpoints, or
business rules. The per-tool question builder and the AUTO-path resolver
(Proceed/Guide/Deny) are supplied by the caller via CheckpointSpec. The
ESCALATE path is always handled generically here (Confirm), regardless of
domain -- confidence routes a decision, it never decides one.

Resume replay: Strands re-runs before_tool_call for the SAME toolUseId when a
paused run resumes (the interrupted tool call is replayed, not skipped -- see
strands.event_loop.event_loop._handle_tool_execution). Once a toolUseId has
escalated once, DeciderGuardrail remembers it (_escalated) and on the next
call for that id resolves straight from the tool's own BeforeToolCallEvent.
interrupt() -- which strands has already recorded the human's answer against,
keyed by this exact (toolUseId, handler name) pair -- instead of asking
Decider again or firing on_decision a second time.

Decision reuse across toolUseIds: a model can retry a gated tool call with a
BRAND NEW toolUseId carrying the exact same effective state (e.g. the model
calls generate_scenarios again after a human already answered its escalation
-- the retry is not Strands' own resume replay above, which only recognizes
the original toolUseId). Without caching, that new toolUseId would ask
Decider again, get the same low-confidence answer, and re-escalate -- which
the caller cannot always answer a second time (see
ai_test_engineer.orchestration._unexpected_interrupt_stop). So every resolved
decision (AUTO or human-answered) is cached by a key derived from
(tool_name, canonical JSON of the decider_state + questions actually sent) --
a later call with the identical key reuses that resolution directly, with no
Decider call and no new escalation. A changed state (different questions or
decider_state -- e.g. a v2 scenario set at a recheck) hashes to a different
key and gets a fresh Decider call, same as today.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from strands.hooks.events import BeforeToolCallEvent
from strands.interventions import Confirm, Deny, Guide, InterventionHandler, Proceed
from strands.interventions.actions import default_evaluate

from decider_gate.client import DeciderClient, DeciderError
from decider_gate.gate import ConfidenceGate, Verdict


def _cache_key(tool_name: str, decider_state: Any, questions: dict) -> str:
    """A stable key for one (tool, decider_state, questions) combination.

    default=str covers values json can't natively serialize (never raises --
    this is bookkeeping, not the Decider payload itself)."""
    payload = json.dumps(
        {"state": decider_state, "questions": questions}, sort_keys=True, separators=(",", ":"), default=str
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{tool_name}:{digest}"


@dataclass
class CheckpointSpec:
    """How to guard one tool: build its Decider question(s), resolve an all-AUTO result.

    build_questions(state) -> {question_name: question_dict}. `state` is the
    guarded tool's input (event.tool_use["input"]), passed through unchanged --
    this package does not interpret or reshape it.

    resolve(verdicts) -> Proceed | Guide | Deny. Called only when every
    verdict in this checkpoint routed AUTO; an ESCALATE anywhere in the
    checkpoint is always handled generically by DeciderGuardrail instead
    (returns Confirm), so resolve never has to handle it.

    build_state(state) -> Any, optional. If supplied, its return value is sent
    to Decider as `state` instead of the raw tool input -- lets a caller whose
    guarded tool takes few/no arguments (e.g. a zero-arg tool that reads prior
    context from elsewhere) still give Decider a rich state payload. Called
    after build_questions, so it may read context build_questions populated
    in a shared closure. Defaults to None, meaning "send the raw tool input
    unchanged" -- the existing behavior.
    """

    build_questions: Callable[[Any], dict]
    resolve: Callable[[dict[str, Verdict]], Proceed | Guide | Deny]
    build_state: Callable[[Any], Any] | None = None


class DeciderGuardrail(InterventionHandler):
    """Maps guarded tool names to CheckpointSpecs; everything else Proceeds."""

    def __init__(
        self,
        client: DeciderClient,
        gate: ConfidenceGate,
        checkpoints: dict[str, CheckpointSpec],
        host_label: str,
        on_decision: Callable[[str, dict[str, Verdict], Any], None] | None = None,
        on_error: Callable[[str, Exception], None] | None = None,
        on_reuse: Callable[[str, dict[str, Verdict], Any, str | None], None] | None = None,
        on_call: Callable[[str, str | None, Any], None] | None = None,
        name: str = "decider-gate",
    ) -> None:
        self._client = client
        self._gate = gate
        self._checkpoints = checkpoints
        self._host_label = host_label
        self._on_decision = on_decision
        self._on_error = on_error
        self._on_reuse = on_reuse
        self._on_call = on_call
        self._name = name
        # toolUseId -> {"key": cache_key, "verdicts": verdicts captured at
        # escalation time} -- Strands' own resume replay for that same id.
        self._escalated: dict[str, dict[str, Any]] = {}
        # cache_key -> {"action", "verdicts", "origin_tool_use_id"} -- the
        # final resolution (AUTO or human-answered) for a given
        # (tool_name, decider_state, questions) combination. See module
        # docstring's "Decision reuse across toolUseIds".
        self._resolved: dict[str, dict[str, Any]] = {}

    @property
    def name(self) -> str:
        return self._name

    def before_tool_call(
        self, event: BeforeToolCallEvent, **kwargs: Any
    ) -> Proceed | Deny | Guide | Confirm:
        tool_name = event.tool_use["name"]
        spec = self._checkpoints.get(tool_name)
        tool_use_id = event.tool_use.get("toolUseId")

        if spec is None:
            return self._finish(tool_name, tool_use_id, Proceed())

        if tool_use_id in self._escalated:
            pending = self._escalated.pop(tool_use_id)
            response = event.interrupt(self._name, reason="replay after human resume")
            action = Proceed() if default_evaluate(response) else Deny(
                reason=f"DENIED: human rejected the escalated {tool_name} decision"
            )
            self._resolved[pending["key"]] = {
                "action": action,
                "verdicts": pending["verdicts"],
                "origin_tool_use_id": tool_use_id,
            }
            return self._finish(tool_name, tool_use_id, action)

        state = event.tool_use.get("input")
        questions = spec.build_questions(state)
        decider_state = spec.build_state(state) if spec.build_state is not None else state
        key = _cache_key(tool_name, decider_state, questions)

        cached = self._resolved.get(key)
        if cached is not None:
            if self._on_reuse is not None:
                self._on_reuse(tool_name, cached["verdicts"], cached["action"], cached["origin_tool_use_id"])
            return self._finish(tool_name, tool_use_id, cached["action"])

        try:
            response = self._client.ask(state=decider_state, questions=questions)
            verdicts = {
                question_name: self._gate.evaluate(
                    question_name,
                    answer,
                    response["latency_ms"],
                    self._host_label,
                    response["model"],
                )
                for question_name, answer in response["answers"].items()
            }
        except (DeciderError, KeyError, ValueError, TypeError) as exc:
            # A DeciderError means the server/transport failed; a KeyError/
            # ValueError/TypeError here means build_questions or gate.evaluate
            # choked on a malformed/unexpected answer. Both are treated the
            # same way: never a silent substitute answer, never let it
            # propagate out of a guardrail hook. Never cached -- a transient
            # failure should not be able to permanently poison a key.
            if self._on_error is not None:
                self._on_error(tool_name, exc)
            return self._finish(tool_name, tool_use_id, Deny(reason=f"Decider error: {exc}"))

        if any(verdict.route == "ESCALATE" for verdict in verdicts.values()):
            action = Confirm(prompt=self._build_confirm_prompt(verdicts))
            if tool_use_id is not None:
                self._escalated[tool_use_id] = {"key": key, "verdicts": verdicts}
        else:
            action = spec.resolve(verdicts)
            self._resolved[key] = {"action": action, "verdicts": verdicts, "origin_tool_use_id": tool_use_id}

        if self._on_decision is not None:
            self._on_decision(tool_name, verdicts, action)

        return self._finish(tool_name, tool_use_id, action)

    def _finish(self, tool_name: str, tool_use_id: str | None, action: Any) -> Any:
        if self._on_call is not None:
            self._on_call(tool_name, tool_use_id, action)
        return action

    @staticmethod
    def _build_confirm_prompt(verdicts: dict[str, Verdict]) -> str:
        lines = ["Decider confidence below threshold -- human review needed:"]
        for question_name, verdict in verdicts.items():
            if verdict.route != "ESCALATE":
                continue
            lines.append(
                f"- {question_name}: suggestion={verdict.suggestion!r}, "
                f"confidence={verdict.confidence:.2f}, probabilities={verdict.probabilities!r}"
            )
        return "\n".join(lines)
