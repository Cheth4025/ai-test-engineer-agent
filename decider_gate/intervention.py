"""Generic confidence-gated guardrail wired as a Strands InterventionHandler.

Verified against strands-agents==1.61.0: InterventionHandler.before_tool_call(
self, event: BeforeToolCallEvent, **kwargs) -> Proceed | Deny | Guide | Confirm
| Transform; event.tool_use["name"] / ["input"] carry the tool name/input.

Domain-agnostic: this module knows nothing about payments, checkpoints, or
business rules. The per-tool question builder and the AUTO-path resolver
(Proceed/Guide/Deny) are supplied by the caller via CheckpointSpec. The
ESCALATE path is always handled generically here (Confirm), regardless of
domain -- confidence routes a decision, it never decides one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from strands.hooks.events import BeforeToolCallEvent
from strands.interventions import Confirm, Deny, Guide, InterventionHandler, Proceed

from decider_gate.client import DeciderClient, DeciderError
from decider_gate.gate import ConfidenceGate, Verdict


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
    """

    build_questions: Callable[[Any], dict]
    resolve: Callable[[dict[str, Verdict]], Proceed | Guide | Deny]


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
        name: str = "decider-gate",
    ) -> None:
        self._client = client
        self._gate = gate
        self._checkpoints = checkpoints
        self._host_label = host_label
        self._on_decision = on_decision
        self._on_error = on_error
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def before_tool_call(
        self, event: BeforeToolCallEvent, **kwargs: Any
    ) -> Proceed | Deny | Guide | Confirm:
        tool_name = event.tool_use["name"]
        spec = self._checkpoints.get(tool_name)
        if spec is None:
            return Proceed()

        state = event.tool_use.get("input")

        try:
            questions = spec.build_questions(state)
            response = self._client.ask(state=state, questions=questions)
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
            # propagate out of a guardrail hook.
            if self._on_error is not None:
                self._on_error(tool_name, exc)
            return Deny(reason=f"Decider error: {exc}")

        if any(verdict.route == "ESCALATE" for verdict in verdicts.values()):
            action = Confirm(prompt=self._build_confirm_prompt(verdicts))
        else:
            action = spec.resolve(verdicts)

        if self._on_decision is not None:
            self._on_decision(tool_name, verdicts, action)

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
