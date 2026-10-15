"""Generic example: guard one tool call with a single noul question.

Requires a running `strands-decider serve <model> --port 8000`. No
domain-specific code -- swap "delete_resource" and the question wording for
whatever tool/question your agent needs guarded.
"""

from strands.interventions import Deny, Proceed

from decider_gate import CheckpointSpec, ConfidenceGate, DeciderClient, DeciderGuardrail

client = DeciderClient(url="http://127.0.0.1:8000", timeout_s=30)
gate = ConfidenceGate(noul_upper=0.80, noul_lower=0.40)


def build_questions(state):
    return {
        "safe_to_proceed": {
            "type": "noul",
            "instructions": "Is this action safe to perform given the state?",
            "criteria": {"true": "Safe to proceed", "false": "Not safe"},
        }
    }


def resolve(verdicts):
    return Proceed() if verdicts["safe_to_proceed"].answer else Deny(reason="Decider said not safe")


guardrail = DeciderGuardrail(
    client=client,
    gate=gate,
    host_label="local-cpu",
    checkpoints={"delete_resource": CheckpointSpec(build_questions=build_questions, resolve=resolve)},
)
