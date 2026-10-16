# decider_gate

A generic, confidence-gated guardrail for [Strands Decider 2B](https://github.com/strands-labs/strands-decider).
Domain-agnostic: this package knows nothing about any particular agent's
tools, checkpoints, or business rules. It never imports from `src/`.

## The pattern

```
          proposed tool call (from a Strands agent)
                         │
              DeciderGuardrail.before_tool_call
                         │
              is this tool guarded?  ──no──▶ Proceed()
                         │ yes
            DeciderClient.ask(state, questions)
                         │
              DeciderError? ──yes──▶ Deny(reason=...)   (on_error callback fires)
                         │ no
         ConfidenceGate.evaluate(...) per question ──▶ Verdict(s)
                         │
        any verdict ESCALATE? ──yes──▶ Confirm(prompt=...)   (generic, shows
                         │                                     Decider's suggestion)
                         │ no (all AUTO)
            caller-supplied CheckpointSpec.resolve(verdicts)
                         │
                 Proceed / Guide / Deny
                         │
              on_decision callback fires either way
```

## Components

| Component | Responsibility |
|---|---|
| `DeciderClient` | One HTTP POST to `{url}/v1/systemone`. One attempt, no retries. Raises `DeciderError` on timeout, connection failure, bad status, or a response missing an expected answer -- never fabricates an answer. |
| `ConfidenceGate` | Turns one raw Decider answer into a `Verdict`. For `choice`: `route = AUTO` if `confidence >= choice_threshold`, else `ESCALATE`. For `noul`: `AUTO`/`True` if `p >= noul_upper`, `AUTO`/`False` if `p <= noul_lower`, else `ESCALATE`. Confidence always routes; it never decides for you. |
| `CheckpointSpec` | Caller-supplied, per guarded tool: `build_questions(state) -> questions` and `resolve(verdicts) -> Proceed\|Guide\|Deny`, called only when every verdict in the checkpoint is `AUTO`. |
| `DeciderGuardrail` | A `strands.interventions.InterventionHandler`. Maps tool name -> `CheckpointSpec`. Handles the `ESCALATE` case generically (`Confirm`) and the error case generically (`Deny`), so domain code never has to reinvent either. |

## Thresholds

Thresholds are plain constructor arguments, never hardcoded inside the
package and never model-editable. The calling application should read them
from its own config (e.g. `config/decider.json` in this repo) and pass them
in:

```python
import json
cfg = json.loads(open("config/decider.json").read())
gate = ConfidenceGate(
    choice_threshold=cfg["choice_threshold"],
    noul_upper=cfg["noul_upper"],
    noul_lower=cfg["noul_lower"],
)
```

## Audit and error hooks

`DeciderGuardrail` takes two optional callbacks so a controller can persist
evidence without this package knowing anything about run IDs or JSON files:

- `on_decision(tool_name, verdicts, action)` -- fires for *every* checkpoint
  call, whether the route was `AUTO` or `ESCALATE`.
- `on_error(tool_name, error)` -- fires on a `DeciderError`. The guardrail
  always returns `Deny` on an error regardless of whether this is set; it is
  purely for evidence/audit, never for recovery.

## Example

See `examples/generic_tool_guard.py` for a ~15-line example guarding one
generic tool call with a single `noul` question.
