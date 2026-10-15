# Claude Code Implementation Handoff

**Project:** AI Test Engineer Agent  
**Use case:** Payment Retry & Duplicate Charge Prevention  
**Revision 2 (Oct 13, 2026):** local/EC2 Decider server, `decider_gate` reusable package, confidence escalation, benchmark, new milestones through submission on Oct 23.

## 1. Builder instructions and readiness

Start with [BUILD_BRIEF.md](BUILD_BRIEF.md) — it is the one-page version of this package for Claude Code. Use this document and 02–07 for detail and 09 for acceptance. The functional authority (`AI_Test_Engineer_Agent_Requirements.md`) prevails; supply it beside the package.

Local component design is ready. Integration depends on O01–O03 in [Open Decisions](Open_Decisions.md), all closable this week. One builder works part-time with Claude Code; dates are targets, not guarantees.

Use F01–F09, BR01–BR06, S01–S05 and T01–T05. Don't infer business scope from a tutorial. Surface real blockers; never fabricate model output or evidence.

## 2. Repository layout

```text
ai-test-engineer-agent/
  README.md                     # Pitch, architecture image, quickstart, benchmark table
  LICENSE                       # Apache-2.0 (compatible with Decider)
  pyproject.toml                # Pinned after the spike
  .env.example                  # Names only
  docs/                         # Authority + this package + decider_wire_format.md
  config/
    requirement.json            # Immutable story and six rules
    scenario_catalog.json       # Fixed five-scenario semantic oracle
    test_manifest.json          # Exactly five node IDs + code fingerprints
    decider.json                # Model ID/revision, URL, threshold, timeouts, host label
  patterns/
    scenario_grounding_guardrail/  # CLEARLY LABELLED REUSABLE PATTERN (hackathon requirement)
      questions.json            # Typed questions + option sets (noul grounding; choice coverage action)
      wiring.py                 # InterventionHandler placing the decision in the agent's path
      README.md                 # What it does, how to use it, threshold guidance
  decider_gate/                 # Generic library used by the pattern — no payment-specific code
    __init__.py
    client.py                   # DeciderClient: POST /v1/systemone, one attempt, latency
    gate.py                     # ConfidenceGate: Verdict(answer, confidence, AUTO|ESCALATE)
    intervention.py             # DeciderGuardrail(InterventionHandler)
    README.md                   # Pattern explanation + generic 15-line example
    examples/generic_tool_guard.py
  src/ai_test_engineer/
    ui.py                       # One page, four tabs
    orchestration.py            # Strands agent, stage tools, checkpoint question builders
    state.py                    # Controller: current run, lock, flags, JSON save
    bedrock.py                  # Bedrock model setup for generation/analysis
    tools.py                    # Intake/interpretation/scenario/analysis tools
    coverage.py                 # Deterministic content validation
    mock_payment.py             # In-process deterministic synthetic service
    execution.py                # Approved fixed pytest subprocess and results
    reporting.py                # Report/audit rendering
  tests/payment/
    conftest.py                 # Fresh fixtures, evidence writer
    test_payment.py             # Exactly T01–T05
  tests/unit/                   # Controller/gate/validator unit tests (not payment tests)
  bench/
    grounding_set.jsonl         # ~40 SME-labelled correct/mutated scenarios
    run_bench.py                # Decider vs Bedrock judge vs hybrid
    results.md                  # Generated table for README/article
  deploy/
    ec2_setup.md                # Instance, AMI, quota, SSM port forward, GPU check, stop/terminate
  runs/                         # Ignored
```

`tests/unit/` holds ordinary developer unit tests for controller guards, `ConfidenceGate` and the validator. They are not payment tests and are never part of the approved batch. No plugin platform, microservices, event sourcing, auto-resume or container orchestration.

## 3. Dependencies

Pin exact versions after the spike; record them in `pyproject.toml`.

| Dependency | Purpose | Notes |
|---|---|---|
| Python 3.12 | App, fixtures | Windows for dev; same on EC2 |
| strands-agents | Orchestrator, Bedrock provider, `strands.interventions` | Confirm `InterventionHandler`, `Proceed`, `Guide`, `Deny`, `Confirm` exist in the pinned version |
| strands-decider | Decider server (`strands-decider serve`) | Runs where Decider is hosted; app talks HTTP only. On Windows, WSL2 is the documented GPU path; CPU works natively if the install succeeds |
| httpx or requests | DeciderClient HTTP | Pick one |
| boto3 / botocore | Bedrock credentials/session | Standard credential chain; instance role on EC2 |
| streamlit | UI | Pin |
| pytest | Five-test runner, JUnit, unit tests | No reporting plugins |

## 4. Configuration

| Variable | Default | Notes |
|---|---|---|
| AWS_PROFILE | — | Dev only; EC2 uses instance role |
| AWS_REGION | us-east-1 | Bedrock and EC2 host in the same region |
| AITE_BEDROCK_MODEL_ID | us.anthropic.claude-sonnet-4-6 | Verified Oct 13. Switch to us.anthropic.claude-sonnet-5-5 if account access opens (O03) |
| AITE_BENCH_JUDGE_MODEL_ID | us.anthropic.claude-haiku-4-5-20251001-v1:0 | Verified Oct 13. Switch to us.anthropic.claude-haiku-5-5 if access opens |
| AITE_DECIDER_URL | http://127.0.0.1:8000 | Local server, or SSM-forwarded port |
| AITE_DECIDER_MODEL | — | Exact model ID/revision downloaded in the spike |
| AITE_DECIDER_HOST_LABEL | local-cpu | e.g. ec2-g4dn-t4 for the demo |
| AITE_DECIDER_THRESHOLD | 0.75 | Choice-question confidence cutoff (matches 04). Calibrated once on the benchmark |
| AITE_DECIDER_TIMEOUT_SECONDS | 30 (CPU) / 10 (GPU) | One attempt |
| AITE_ARTIFACT_ROOT | ./runs | |
| AITE_DUPLICATE_LEDGER_DEFECT | true | In approval bundle |
| AITE_GENERATION_TIMEOUT_SECONDS | 120 | |
| AITE_PYTEST_TIMEOUT_SECONDS | 60 | |

Never commit `.env`, credentials, account IDs or `runs/`. Do not pass AWS credentials to the pytest child.

## 5. Milestones (Oct 13 → Oct 23)

| Date | Increment | Done when |
|---|---|---|
| **Oct 13–14** | Decider spike (O01) + rules check (O04); mock, T01–T05, validator; GPU quota request (O02) | Spike checklist in [04 §7](04_Decider_Decision_Design.md) complete; wire format pinned; T01–T05 deterministic with defect-on four PASS / T04 FAIL; S04 missing → 4/5, valid addition → 5/5; content-invalid S ID rejected. |
| **Oct 15** | `decider_gate` package + unit tests; Bedrock access (O03); Strands agent with interpret/generate tools | `DeciderClient` + `ConfidenceGate` + `DeciderGuardrail` work against the local server; generic example runs; Bedrock structured generation works. |
| **Oct 16** | All five checkpoints + grounding wired; one real remediation via Guide; failure class/handling; EC2 GPU host up (O02) | Real Decider decisions with confidence drive the flow; escalation pauses correctly; GPU confirmed in use and latency recorded. |
| **Oct 17** | Four-tab Streamlit UI | Tabs per 06; Approve launches nothing; Execute once; Confirm decision and grounding resolution work; reload/double-click safe. |
| **Oct 18** | End-to-end integration, report, benchmark run | Full demo path on EC2; `bench/results.md` produced; threshold calibrated and recorded. |
| **Oct 19** | SME review, fixes, rehearsal | Independent check per §7; fixes don't change assertions or the deliberate defect. |
| **Oct 20** | **Code freeze** | 09 checklist passes; demo runs end-to-end on EC2. |
| Oct 21–23 | Submission work | See [10](10_Hackathon_Submission_Plan.md): repo/README, video (Oct 21), article (Oct 22), submit Oct 23. Stop/terminate EC2. |

### Oct 13–14 test automation instruction (unchanged)

While building the five pytest tests, implement the reusable payment primitives in 07 as simple helpers or fixture methods. Keep BR → S → T traceability; keep business assertions in the predefined tests; fresh fixtures and deterministic scripts. No BDD framework.

1. T01–T05 remain the only executable payment tests.
2. All five use shared primitives where practical.
3. Business-rule assertions and synthetic inputs unchanged.
4. T04 still fails a real ledger-count assertion with the defect enabled.
5. Generated Gherkin is review-only.
6. Human Approve, separate Execute and one batch per run remain.

## 6. Claude Code guardrails

- Implement the authority and this package; record conflicts instead of changing scope.
- One Strands agent; tools are not agents.
- `decider_gate/` must not import anything from `src/ai_test_engineer/`.
- Decider answers come only from the real Decider server. No substitute model labelled as Decider, no hardcoded answers, no default-to-remediate.
- Confidence routes decisions; it never bypasses a controller guard. Thresholds come from config.
- Never execute generated Python/Gherkin, model-supplied commands or paths. `run_tests` is not an agent tool.
- Exactly five payment tests; no xfail/skip/-x; no assertion loosening.
- Respect `remediation_used`, `execution_started`, `investigation_used`.
- Never fabricate execution, evidence, model availability, SDK functions or confidence. Evidence from a CPU dev host is labelled as such.

## 7. Human responsibilities

**Before implementation (Oct 14):** Test SME confirms BR01–BR06, the five test sequences and oracles, and labels the benchmark set.

**End-to-end check (Oct 19):** Test SME reviews actual Decider choices and confidences, escalations, approval binding, all five pytest records, defect evidence, report/audit, and refresh/rejection behavior. A clean UI or 5/5 coverage does not substitute for this check.
