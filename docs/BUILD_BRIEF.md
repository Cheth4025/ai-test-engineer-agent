# Build Brief for Claude Code

One page. Read this first; the numbered docs hold the detail.

## What we're building

An AI Test Engineer Agent for one fixed requirement: *a customer retries a payment after a gateway timeout without being charged twice* (rules BR01–BR06). The agent:

1. Interprets the requirement (Bedrock).
2. Writes test scenarios S01, S02, S03, S05 with Gherkin (Bedrock). S04 is left out on purpose — a seeded gap the UI labels openly.
3. Checks the scenarios: a deterministic validator computes coverage (4/5), and **Decider 2B** checks each scenario's grounding and chooses to remediate.
4. Adds S04 once (5/5).
5. Waits for a human to **Approve**, then a separate human **Execute** click runs five fixed pytest tests once against an in-memory mock.
6. T04 fails for real (expected 1 ledger posting, got 2). Bedrock writes the analysis; Decider classifies the failure and decides whether to investigate.
7. Writes a Markdown report: *agent workflow COMPLETED, payment verification FAILED*.

**Bedrock writes. Decider judges.** Every Decider answer has a confidence; below the threshold (0.9 to start), the human decides, with Decider's suggestion shown.

## Build order

1. **Spike (today):** `pip install strands-decider`, `strands-decider serve <model> --port 8000`, one `choice` and one `noul` call. Record the exact JSON in `docs/decider_wire_format.md` and `config/decider.json`. ([04 §7](04_Decider_Decision_Design.md))
2. `mock_payment.py` + `tests/payment/test_payment.py` (T01–T05 exactly as in [07](07_Test_Automation_Design.md)). Defect on → 4 PASS, T04 FAIL.
3. `coverage.py` validator against `config/scenario_catalog.json`.
4. `decider_gate/` (client, gate, InterventionHandler) + unit tests. No payment code inside it.
5. Strands agent + tools + Bedrock; wire the five checkpoints + grounding ([04 §3](04_Decider_Decision_Design.md)).
6. Controller (`state.py`): twelve states, one lock, three single-use flags ([05](05_Data_Contracts.md)).
7. Four-tab Streamlit UI ([06](06_Streamlit_UI_Specification.md)).
8. Report + `bench/run_bench.py` ([10 §3](10_Hackathon_Submission_Plan.md)).

## Hard rules (don't break these)

1. **Exactly five payment tests, unchanged assertions.** T04 must fail for real with the defect on. No xfail, skip, -x or reruns.
2. **Humans gate execution.** Approve records only; a separate Execute click runs the batch once. No model or tool can launch tests.
3. **No fake intelligence.** Decider answers come from the real Decider server; Bedrock output comes from Bedrock. Never hardcode, substitute or invent a decision, a confidence or evidence. Confidence routes a decision but never bypasses a guard.

## Keep it small

Plain JSON files in `runs/<run_id>/`. One process, one worker, one lock. No database, web API, event sourcing, auto-resume, login or CI/CD.

## Where things run

- **Dev:** Windows; Decider on CPU locally (or WSL2).
- **Demo (required by the Rules: agent on AWS):** one EC2 g4dn.xlarge running the app, the Strands agent and Decider on GPU, reached by SSM port forwarding. Stop it when idle.
- **Reusable pattern** goes in `patterns/scenario_grounding_guardrail/` (questions.json, wiring.py, README.md), clearly labelled.
