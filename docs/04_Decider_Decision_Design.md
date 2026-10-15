# Decider Decision Design

**Project:** AI Test Engineer Agent  
**Use case:** Payment Retry & Duplicate Charge Prevention  
**Code freeze:** October 20, 2026 · **Hackathon submission:** October 23, 2026  
**Declared hackathon pattern:** `#guardrails` (see [10](10_Hackathon_Submission_Plan.md))

> **Revision 2 (Oct 13).** Decider now makes real, confidence-scored decisions instead of confirming answers the controller already knew. Native confidence is used, not omitted. Low confidence escalates to the human. Decider is wired into the Strands agent through an `InterventionHandler`, and that wiring is published as a reusable pattern (`decider_gate`). Local Decider is allowed for development. The five F05 checkpoints from the functional authority are all kept.

## 1. What Decider 2B is and how we call it

Strands Decider 2B is a ~2B-parameter open model with a pointer head instead of a text head. It answers three question types, each with a native confidence or probability:

| Type | Returns | Our use |
|---|---|---|
| `choice` | One option from a list, with per-option probabilities and a confidence (**observed Oct 14: confidence = margin between the top two probabilities**) | Next-action checkpoints (STRATEGY, COVERAGE, FAILURE) |
| `noul` (yes/no) | A number in 0–1; closer to 1 means Yes | Guardrail questions: scenario grounding, ambiguity, completion readiness |
| `score` | Position on an ordered rubric, with a confidence | Optional: failure severity |

**Serving:** `pip install strands-decider`, then `strands-decider serve <model> --port 8000`. The server exposes `POST /v1/systemone` with a `state` string and a named `questions` object; the response carries `answers` (keyed by question name), `usage` and `latency_ms`. Several questions can be asked against one `state` in one call, which is cheaper than separate calls.

**Confirmed Oct 14 (source + GPU spike, see [Open Decisions](Open_Decisions.md) progress log):** model `StrandsAgents/strands-decider-2B-hobson-v19` (base Qwen/Qwen3.5-2B-Base). `choice` options are passed as `criteria: {OPTION: description}`; `noul` accepts optional `criteria: {"true":..., "false":...}`; `state` may be structured JSON. **Always write option descriptions**: they raised coverage confidence from 0.24 to 0.81. Warm latency on a T4: 150–315 ms.

**Project guidance we adopt:** on short classification tasks, answers at confidence ≥ 0.9 were right about 95% of the time; below that, confirm or ask a person. That is exactly our escalation rule.

Source: [strands-labs/strands-decider](https://github.com/strands-labs/strands-decider).

## 2. The reusable pattern: confidence-gated guardrail

This is the piece published separately in the repo (`decider_gate/`), independent of the payment use case.

```text
          proposed tool call (from Strands agent)
                         │
              InterventionHandler.before_tool_call
                         │
        ┌── deterministic guards (state, budgets, flags) ──┐
        │                                                   │ fail → Deny (stop)
        ▼
   Decider question(s) on the call + evidence
        │
   ConfidenceGate
     ├─ confidence ≥ τ_act  → act on Decider's answer: Proceed / Guide / Deny
     └─ confidence <  τ_act → escalate to human (Confirm), showing Decider's
                              top answer and probabilities as a suggestion
```

Components (domain-agnostic):

| Component | Responsibility |
|---|---|
| `DeciderClient` | HTTP call to `/v1/systemone`; one attempt; returns answers, raw response, `latency_ms` and wall-clock time. No retries. |
| `ConfidenceGate` | Turns an answer into `Verdict(answer, confidence, route=AUTO | ESCALATE)` using a threshold τ. For `noul`, confidence = max(p, 1−p) and answer = YES if p ≥ 0.5. |
| `DeciderGuardrail(InterventionHandler)` | Maps tool names to question builders and verdicts to Strands actions: `Proceed`, `Guide(feedback=…)`, `Deny(reason=…)`, `Confirm(prompt=…)`. |

Strands interventions are registered with `Agent(..., interventions=[DeciderGuardrail(...)])`; `Confirm` is only valid from `before_tool_call`, and handlers run in registration order. Verify these against the pinned `strands-agents` version in the spike. **Fallback if the pinned Strands release lacks interventions:** the controller calls the same `DeciderClient` + `ConfidenceGate` directly before each guarded tool. The pattern stays reusable; only the hook point changes.

**Threshold:** project guidance is 0.9, but the Oct 14 spike shows correct answers at 0.81–0.86 (choice) and noul margins of 0.26–0.75. Start with **choice τ = 0.75** and **noul bands p ≥ 0.80 / p ≤ 0.40** (in between → human review). Calibrate once on the benchmark set in [10](10_Hackathon_Submission_Plan.md) and record the chosen value in `config/decider.json`. Thresholds are config, never model-editable.

## 3. The five checkpoints (F05), now as real decisions

The controller still owns state, budgets and single-use flags. Decider answers questions the deterministic code **cannot** answer by itself, or chooses between actions where both are legitimately possible.

| Checkpoint / guarded tool | Decider question(s) | Allowed actions | What the controller still enforces |
|---|---|---|---|
| **AMBIGUITY** — before `generate_scenarios` | Per rule, `noul`: "Does BR0x leave a question that changes what the test must assert?" | Flag ambiguity → REQUEST_CLARIFICATION, or no flag | Clarification never edits the source. A YES with high confidence opens the clarification form; low confidence escalates to the human to decide whether to ask. |
| **STRATEGY** — before `generate_scenarios` | `choice`: GENERATE_TESTS vs REQUEST_CLARIFICATION, given interpretation + ambiguity flags | GENERATE_TESTS, REQUEST_CLARIFICATION | Generation is blocked while any critical ambiguity is open. |
| **COVERAGE** — before `save_scenario_set` | (a) Grounding, per scenario, `noul`: "Does this scenario's prose and Gherkin assert the same outcome as its structured oracle and linked rule?" (b) `choice`: ACCEPT_COVERAGE vs REMEDIATE_GAP, given the validator matrix and grounding answers | ACCEPT_COVERAGE, REMEDIATE_GAP | The deterministic validator alone computes Required Scenario Coverage (F04). Grounding can only **flag** a scenario (NEEDS_REVIEW); it never makes an invalid scenario valid. ACCEPT is rejected unless the validator says 5/5. REMEDIATE is allowed only once (`remediation_used`). |
| **FAILURE** — before `finalize_analysis` | `choice`: failure class BUSINESS_RULE_VIOLATION / INFRASTRUCTURE_ERROR / INSUFFICIENT_EVIDENCE (vocabulary of 05 §11); then `choice`: INVESTIGATE_FAILURE vs REPORT_FAILURE | INVESTIGATE_FAILURE, REPORT_FAILURE | At most one investigation (`investigation_used`). No code repair, no rerun. Class is a labelled assessment, not proof. |
| **COMPLETION** — before `render_report` | `noul`: "Is the evidence complete and consistent enough to finalize?" | FINALIZE_RESULTS (or escalate) | Deterministic readiness guard must also pass. A YES never overrides a missing artifact. |

### Why this matters for the demo

- **The grounding check fills a real hole.** Doc 07 already says free prose and Gherkin cannot certify semantics and that inconsistent prose must be "rejected or escalated", but the earlier design had no mechanism to check prose. A small, fast Decider `noul` per scenario is that mechanism.
- **The S04 gap is a seeded fault (required by F03), presented openly.** The UI and article say so. What's genuinely decided at run time: whether each generated scenario is grounded, whether to remediate, how to classify the failure, and how confident Decider was.
- **Scenario grounding is also the benchmark task** for the Decider vs Bedrock comparison (latency, cost, agreement).

### Where Guide is used

When COVERAGE selects REMEDIATE_GAP with high confidence and `remediation_used=false`, the guardrail sets `remediation_used=true`, saves the v1 set and initial coverage as evidence, and returns `Guide(feedback="S04 / BR06 missing: add a duplicate-callback scenario asserting exactly one ledger posting; keep S01–S03, S05 unchanged")`. The agent regenerates once and calls `save_scenario_set` again, which triggers the recheck. A second gap → `Deny` → BLOCKED.

## 4. Contracts

### Request (logical)

```json
{
  "run_id": "8a722ae3-8a0b-4b02-9f72-d1c234567890",
  "checkpoint": "COVERAGE",
  "workflow_state": "PREPARING",
  "state_text": "Required scenarios S01-S05. Validator: 4/5 valid, S04 (BR06 duplicate callback) missing. Remediation unused. Grounding: S01 yes 0.97, S02 yes 0.94, S03 yes 0.96, S05 yes 0.91.",
  "questions": {
    "coverage_action": {"type": "choice", "instructions": "Accept the scenario set or remediate the gap?", "options": ["ACCEPT_COVERAGE", "REMEDIATE_GAP"]}
  },
  "evidence_references": ["coverage_initial.json", "grounding_initial.json"]
}
```

`state_text` is built by the controller from saved facts only. Keep it short (the model is small and fast on short inputs).

### Normalized decision

```json
{
  "run_id": "8a722ae3-8a0b-4b02-9f72-d1c234567890",
  "checkpoint": "COVERAGE",
  "workflow_state": "PREPARING",
  "selected_action": "REMEDIATE_GAP",
  "confidence": 0.96,
  "option_probabilities": {"ACCEPT_COVERAGE": 0.04, "REMEDIATE_GAP": 0.96},
  "route": "AUTO",
  "threshold": 0.9,
  "decision_source": "DECIDER",
  "rationale": "Validator reports S04 missing; remediation unused.",
  "rationale_origin": "CONTROLLER_EVIDENCE",
  "latency_ms": 212,
  "decision_status": "VALIDATED",
  "evidence_references": ["coverage_initial.json"],
  "native_response_ref": "decider_calls/coverage_initial_response.json"
}
```

Illustrative values, not observed output. `decision_source` is `DECIDER` for AUTO routes or `HUMAN_ESCALATION` when the human picked after a low-confidence escalation (the Decider suggestion is still recorded). `decision_status` is always assigned by the controller.

## 5. Invocation rules

1. Under the controller lock, run deterministic guards first (state, flags, evidence present). Guard failure → STOPPED; Decider is not called.
2. Build `state_text` from saved facts; never include a preselected answer.
3. One HTTP call. Save sanitized request/response, model ID/revision and latency.
4. Validate: known action for this checkpoint, parseable confidence, references exist. Malformed/unknown → STOPPED with the raw response retained.
5. Route by `ConfidenceGate`. AUTO → apply the guarded transition. ESCALATE → set stage `AWAITING_DECISION_CONFIRMATION` (busy=false) and show the human the options, Decider's suggestion and probabilities. The human's pick is still subject to the same guards (e.g. a human cannot ACCEPT a 4/5 set).
6. Save the decision, then perform the bounded side effect; used-flags are saved before the side effect.

Pytest is never a Decider action. Only a human Execute click after a valid Approve runs tests (F06/F07).

**Timeouts:** connect 5 s; Decider call 10 s on GPU, 30 s on CPU dev. Timeout or connection error → STOPPED with error evidence. No silent substitute model, no fabricated answer, no hidden retry.

## 6. Where Decider may run

| Environment | Host | Notes |
|---|---|---|
| Development | Local `strands-decider serve` (WSL2 or native; CPU acceptable, ~seconds per question) | Fine for building and tests. Record "host=local-cpu" in decision evidence. |
| Demo / submission | AWS: one EC2 GPU instance (g4dn.xlarge, NVIDIA T4) running `strands-decider serve` bound to localhost, reached through SSM port forwarding | ~0.5 s per decision expected on T4 (published measurement); verify. Log GPU name at startup and refuse to serve if CUDA is unavailable (published deployments silently fell back to CPU). Stop the instance when idle. |
| Alternatives, only if needed | SageMaker real-time GPU endpoint; AgentCore Runtime GPU instances | More setup (custom container). Not required. |

The same Decider model and code run in every environment; only the host and latency change. Every decision record carries the host label so the report never presents CPU-dev evidence as the AWS demo.

## 7. Spike acceptance check (replaces the Oct 12 gate)

Target: **Oct 13–14, before orchestration work.** Not a payment test.

- [ ] `strands-decider` installed; model downloaded; ID/revision recorded.
- [ ] One `choice` call (COVERAGE, S04 gap) and one `noul` call (grounding, one correct and one mutated S04 scenario) return parseable answers with confidence.
- [ ] Exact HTTP request/response shape for `choice` and `noul` recorded in `config/decider.json` and `docs/decider_wire_format.md`.
- [ ] Latency recorded (local CPU now; EC2 GPU when up).
- [ ] Observation: does Decider separate the correct vs mutated S04 with confidence ≥ 0.9? If not, note it — it shapes the threshold and the demo narrative, and is itself an honest finding for the article.
- [ ] `strands-agents` version pinned; confirm `strands.interventions` (`InterventionHandler`, `Proceed`, `Guide`, `Deny`, `Confirm`) import.
