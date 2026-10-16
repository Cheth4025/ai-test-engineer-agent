"""Confidence-gated routing of a raw Decider answer into a Verdict.

Confidence only routes a decision; it never overrides a caller's guard.
score-type questions are out of scope for this package (ValueError).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Verdict:
    question: str
    answer: Any  # populated only when route == "AUTO"; None on "ESCALATE"
    confidence: float  # native confidence (choice) or max(p, 1-p) (noul)
    route: str  # "AUTO" or "ESCALATE"
    suggestion: Any  # Decider's top pick/lean; always populated
    probabilities: dict | None
    latency_ms: int
    host_label: str
    model: str


AUTO = "AUTO"
ESCALATE = "ESCALATE"


class ConfidenceGate:
    def __init__(
        self,
        choice_threshold: float = 0.75,
        noul_upper: float = 0.80,
        noul_lower: float = 0.40,
    ) -> None:
        self.choice_threshold = choice_threshold
        self.noul_upper = noul_upper
        self.noul_lower = noul_lower

    def evaluate(
        self,
        question: str,
        answer: dict,
        latency_ms: int,
        host_label: str,
        model: str,
    ) -> Verdict:
        question_type = answer.get("type")
        if question_type == "choice":
            return self._evaluate_choice(question, answer, latency_ms, host_label, model)
        if question_type == "noul":
            return self._evaluate_noul(question, answer, latency_ms, host_label, model)
        raise ValueError(f"unsupported Decider answer type: {question_type!r}")

    def _evaluate_choice(
        self, question: str, answer: dict, latency_ms: int, host_label: str, model: str
    ) -> Verdict:
        choice = answer["choice"]
        confidence = answer["confidence"]
        probabilities = answer.get("probabilities")
        route = AUTO if confidence >= self.choice_threshold else ESCALATE
        return Verdict(
            question=question,
            answer=choice if route == AUTO else None,
            confidence=confidence,
            route=route,
            suggestion=choice,
            probabilities=probabilities,
            latency_ms=latency_ms,
            host_label=host_label,
            model=model,
        )

    def _evaluate_noul(
        self, question: str, answer: dict, latency_ms: int, host_label: str, model: str
    ) -> Verdict:
        p = answer["noul"]
        if p >= self.noul_upper:
            route, decided = AUTO, True
        elif p <= self.noul_lower:
            route, decided = AUTO, False
        else:
            route, decided = ESCALATE, None
        return Verdict(
            question=question,
            answer=decided,
            confidence=max(p, 1 - p),
            route=route,
            suggestion=p >= 0.5,
            probabilities={"true": p, "false": 1 - p},
            latency_ms=latency_ms,
            host_label=host_label,
            model=model,
        )
