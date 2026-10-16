"""decider_gate: a generic, confidence-gated guardrail for Decider 2B.

Domain-agnostic. Must never import from src/ai_test_engineer/.
"""

from decider_gate.client import DeciderClient, DeciderError
from decider_gate.gate import ConfidenceGate, Verdict
from decider_gate.intervention import CheckpointSpec, DeciderGuardrail

__all__ = [
    "DeciderClient",
    "DeciderError",
    "ConfidenceGate",
    "Verdict",
    "CheckpointSpec",
    "DeciderGuardrail",
]
