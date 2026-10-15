# AI Test Engineer Agent — Functional Requirements
**Use case:** Payment Retry & Duplicate Charge Prevention.  
**Project name:** AI Test Engineer Agent.  
**Purpose:** Implementation requirements for user review and coding handoff.  
**Scope:** One predefined payment requirement, nine functional capabilities F01–F09, five predefined pytest tests T01–T05, one coverage-remediation cycle, one injected defect, and human approval before execution.

## 1. Use case and expected outcome

**Customer story:** As a customer, I want to retry a payment when the payment gateway times out, without being charged twice.

A timeout leaves the original payment outcome uncertain. The mock payment service must verify the original status and resolve retries using the same idempotency key, preserve the original charge, reject conflicting amounts, and handle duplicate callbacks and delayed success without duplicate financial effects.

The agent interprets this requirement, generates scenarios and Gherkin, identifies and remedies one missing scenario, obtains human approval, runs the fixed tests, analyzes the injected ledger defect, and presents results with evidence and a decision audit trail.

**Expected demonstration outcome:** Required Scenario Coverage of 5/5 (100%) after one remediation; T01, T02, T03, and T05 pass; T04 fails because a duplicate callback creates a second ledger posting. The agent workflow succeeds by detecting and explaining the defect. The payment service fails verification until that defect is resolved. These are expected outcomes, not claims of completed implementation or execution.

## 2. System-under-test business rules

These govern the **mock payment service**, separately from agent requirements. Use synthetic data with one fixed fixture: payment P-001, amount 100.00 USD, idempotency key K-001. Each test resets payment, gateway, and ledger state.

- **BR01 — Idempotency key:** Every payment request must include a unique idempotency key for its logical payment; retries reuse that key.

- **BR02 — Identical retry:** Reusing the same key with the same payload resolves the original payment and must not create another charge.

- **BR03 — Conflicting amount:** Reusing the same key with a different payment amount is rejected; the original charge and ledger remain unchanged.

- **BR04 — Uncertain timeout:** A timeout must not automatically mark the payment as failed. Retain its unresolved outcome until status verification or a callback establishes the result.

- **BR05 — Verify before retry:** Verify the original payment status before initiating another charge. If already successful, return the original result. If unresolved, do not create a new charge; retain the unresolved outcome until the scripted original success is available.

- **BR06 — Callback idempotency:** Re-delivery of the same successful callback, identified by callback ID and payment ID, must not create another financial ledger posting.

Successful completion means exactly one gateway charge and one successful ledger posting for the logical payment, with the original amount and currency. Delayed success resolves the original payment; it does not create a new payment or a second charge.

**Mock contract:** Simulate timeouts, status verification, and delayed success with deterministic scripted events, without wall-clock delays or live services. Use observable unresolved and successful states plus an explicit conflict rejection; exact enum names and response schemas are implementation details. No fixed retry count or retry-exhaustion behavior is required by this five-test suite.

**Injected defect:** A configurable mock-service mode makes repeated successful callbacks append a second ledger posting. T04 must fail its correct one-posting assertion. Gateway charge count and ledger posting count are separate evidence: duplicate ledger recording does not prove an external double charge. Never change assertions to accept the defect.

## 3. Agent constraints and shared contract

- One Strands orchestrator invokes specialized tools and a generative model. Decider 2B selects evidence-based actions at defined checkpoints; deterministic validation enforces permitted actions, structure, traceability, and coverage. Model identifiers, hosting, and invocation configuration are implementation details.

- Minimal Streamlit view: predefined requirement, interpretation/scenarios, coverage before/after, decisions, approval controls, execution results, analysis, and downloadable report. No additional product workflows.

- Only the predefined payment requirement is accepted and remains immutable throughout each run. No upload, free-text requirement intake, requirement library, or requirements discovery.

- Scenario/Gherkin generation and failure explanation may use the model. Executable pytest code is predefined and immutable during a run; Gherkin is a review artifact, not dynamically executed code.

- Approved manifest contains exactly T01–T05. Only this allowlisted suite can run against the local mock service. No arbitrary commands or generated-code execution.

- One intentional omission, **S04**, from the initial scenario set; at most one remediation cycle, followed by one coverage recheck. One test execution batch per approved run; no automatic rerun or repair loop.

- All attempts, model/tool failures, decisions, approval, and artifacts carry a run ID. Artifact versions distinguish the initial and remediated scenario sets. Streamlit refresh/rerender must not duplicate actions.

- A failed test means payment retry and duplicate prevention is not verified, even when the agent demonstration succeeds. Required Scenario Coverage measures valid required scenario content and scenario-to-test mapping against S01–S05. It is not code coverage, exhaustive testing, or proof of defect-free behavior.

- Tool/model errors or malformed decision output terminate or block the affected stage with a visible reason. Do not fabricate successful artifacts or retry indefinitely.

### Bounded Decider 2B contract

Permitted decisions at each checkpoint:

- **Strategy:** GENERATE_TESTS selects retry/idempotency scenario generation from requirement evidence, or REQUEST_CLARIFICATION if a necessary rule is missing. GENERATE_TESTS creates review scenarios and Gherkin only, never executable test code.

- **Coverage:** ACCEPT_COVERAGE or REMEDIATE_GAP based on deterministic coverage evidence; allow one remediation only.

- **Ambiguity:** REQUEST_CLARIFICATION from the human reviewer; unresolved critical ambiguity blocks execution.

- **Failure:** INVESTIGATE_FAILURE or REPORT_FAILURE based on actual assertions and available evidence. Investigation is read-only and bounded to one analysis action; no rerun or source repair.

- **Completion:** FINALIZE_RESULTS only after the approved test batch and required evidence are available. A stopped/blocked summary is distinct from completed verification.

Decider must receive evidence and permitted alternatives, not a hardcoded chosen action. Operational stages such as intake and pytest invocation are controller/tool actions, not additional autonomous decision capabilities.

Each decision produces an application-level structured record with these required fields:

- `checkpoint`: STRATEGY, COVERAGE, AMBIGUITY, FAILURE, or COMPLETION.
- `selected_action`: one of the actions permitted at that checkpoint above.
- `rationale`: a concise explanation tied to the available evidence.
- `evidence_references`: references to existing run artifacts supporting the action.
- `decision_status`: VALIDATED or REJECTED, assigned by the deterministic controller; model output cannot declare itself validated.
- `workflow_state`: the current controller state against which the decision is evaluated.

Retain the run ID and remaining remediation/execution budgets with each record. Deterministic validation checks required fields, valid checkpoint/action pairs, evidence references, actual workflow state, prerequisites, and budgets before dispatch. Malformed output is retained with a rejection reason; missing fields cannot become a validated decision. The controller rejects execution without current human approval or with unresolved critical ambiguity/coverage. Decider cannot approve its own artifacts, grant permissions, increase budgets, bypass human approval, or alter the predefined requirement, business rules, or executable tests.

This contract describes application outputs and validation, not an undocumented Decider 2B API. The supported integration may adapt its response to this record without changing these requirements.

Allowed progression: intake → interpretation → initial scenarios → coverage check → at most one remediation → coverage recheck → approval → one execution batch → analysis → report. Unresolved coverage, rejection, invalid decisions, or infrastructure errors lead to a blocked/stopped report. Terminal states cannot resume execution without a new run and approval.

## 4. Five predefined tests

S01–S05 are AI-generated conceptual review scenarios and Gherkin. T01–T05 are predefined executable pytest tests, independent of those artifacts. The deliberate omission of S04 never removes, disables, or modifies T04; all five executable tests remain in the fixed manifest. Traceability uses S01→T01, S02→T02, S03→T03, S04→T04, and S05→T05; initially S04 is marked missing rather than represented as generated. Each assertion must be supported by captured service, gateway, and ledger evidence.

### T01 — Initial success (S01; BR01)

- **Given:** P-001 with K-001 and 100.00 USD; gateway returns success.

- **When:** Submit the payment.

- **Then:** Exactly one gateway charge and one successful ledger posting; payment is successful for 100.00 USD.

- **Defect-on expectation:** PASS.

### T02 — Timeout and identical retry (S02; BR01, BR02, BR04, BR05)

- **Given:** The original request with K-001 commits one gateway charge but its response times out; status verification subsequently returns original success.

- **When:** Retry with K-001 and the identical payload.

- **Then:** Timeout was recorded as unresolved; status verification precedes any possible new charge; retry returns the original successful payment; exactly one gateway charge and one ledger posting exist.

- **Defect-on expectation:** PASS.

### T03 — Same key with conflicting amount (S03; BR01, BR03)

- **Given:** K-001 already identifies a successful 100.00 USD payment.

- **When:** Submit K-001 with amount 125.00 USD.

- **Then:** Reject the conflicting request; the original amount, successful payment, single gateway charge, and single ledger posting remain unchanged.

- **Defect-on expectation:** PASS.

### T04 — Duplicate successful callback (S04; BR06)

- **Given:** Callback C-001 for P-001 has established success with one ledger posting and one gateway charge.

- **When:** Deliver C-001 again for P-001.

- **Then:** Exactly one ledger posting remains; payment amount, currency, successful state, and gateway charge count remain unchanged.

- **Defect-on expectation:** FAIL on ledger count, expected 1 and actual 2. Retain callback/payment identity and both ledger entries as evidence.

### T05 — Delayed success after timeout (S05; BR01, BR02, BR04, BR05)

- **Given:** Original request with K-001 commits one charge but times out; first status verification remains unresolved. Original success callback is queued.

- **When:** Request an identical retry, deliver the scripted delayed success once, then resolve the retry against the original payment.

- **Then:** Timeout and unresolved verification do not mark failure or create another charge; delayed success reconciles P-001; the retry resolves the same payment; exactly one gateway charge and one ledger posting exist.

- **Defect-on expectation:** PASS.

## 5. Functional user stories

### F01 — Predefined requirement intake
**User story:** As a test engineer, I want to load the predefined payment retry and duplicate prevention requirement so that the demonstration starts from a consistent, reviewable input.

**Preconditions:** The approved payment retry and duplicate prevention text and version are bundled with the application.

**Acceptance criteria:**

- Starting a run creates a unique run ID and displays the requirement identifier, version, and exact bundled text.

- The same source text/version is retained in the audit artifacts and supplied to interpretation.

- Missing/empty requirement data blocks the run with a visible reason; no interpretation or execution begins.

**Key rules/edge cases:** Only the predefined requirement is selectable. Refresh preserves the active run; an explicit new run clears prior approval and creates new artifacts.

**Outputs:** Requirement snapshot, requirement version, run ID, intake event.

### F02 — Requirement interpretation
**User story:** As a test engineer, I want the agent to extract payment retry and duplicate prevention behaviors and expected outcomes so that scenarios remain traceable to the requirement.

**Preconditions:** F01 succeeded; the predefined requirement includes BR01–BR06.

**Acceptance criteria:**

- Interpretation identifies BR01–BR06, identical-payload retry, conflicting-amount rejection, uncertain timeout, original-status verification, delayed success, and the duplicate-callback ledger invariant.

- Each interpreted rule links to the source requirement text and distinguishes stated rules from assumptions.

- Record ambiguities with affected rule IDs, evidence, and whether they are critical to scenario correctness, coverage, or execution. Request human clarification when necessary and retain the response as a separate audit record. The predefined requirement, its version, and BR01–BR06 remain immutable during execution; clarification may explain their meaning but cannot change them. Unresolved critical ambiguities block approval and test execution; no response leaves the run awaiting clarification, and an unresolved conflict stops the run. No requirement-version modification workflow is performed.

**Key rules/edge cases:** A model-generated interpretation cannot override the approved source. An unresolved timeout must not be interpreted as a confirmed failure.

**Outputs:** Structured interpretation with rule IDs, expected outcomes, source references, assumptions/ambiguities.

### F03 — Test scenario and Gherkin generation
**User story:** As a test engineer, I want readable payment retry and duplicate prevention scenarios and Gherkin so that I can review intended behavior before execution.

**Preconditions:** F02 produced a valid interpretation.

**Acceptance criteria:**

- Initial artifact contains S01, S02, S03, and S05 with title, linked rule IDs, concrete setup, action, observable expected results, and Given/When/Then text.

- S04 is deliberately absent from the initial artifact; the audit trail records the demo omission. The expected coverage catalog still contains S04; executable T04 remains present and unchanged in the fixed manifest.

- Each present scenario maps to its predefined test ID; generation and remediation produce scenario/Gherkin artifacts only and do not create, modify, remove, or disable executable tests.

**Key rules/edge cases:** Use the correct business oracle despite the injected defect. Invalid/incomplete generated scenario content cannot be marked valid solely because its ID exists.

**Outputs:** Version 1 scenario set and Gherkin, S01/T01, S02/T02, S03/T03, and S05/T05 mapping, omission event.

### F04 — Coverage assurance and one remediation cycle
**User story:** As a test engineer, I want the agent to detect the omitted duplicate-callback scenario and repair it once so that the review set covers the agreed payment retry and duplicate prevention behaviors.

**Preconditions:** Initial scenarios exist; expected catalog independently defines S01–S05 and their required behaviors.

**Acceptance criteria:**

- Initial check reports **Required Scenario Coverage: 4/5 (80%)** and missing S04/BR06 with a concrete gap description. A BR01 mapping elsewhere cannot conceal the BR06 gap.

- One remediation adds a valid S04 with duplicate-callback action, expected ledger count of one, Gherkin, and T04 mapping; S01, S02, S03, and S05 remain available.

- One recheck reports **Required Scenario Coverage: 5/5 (100%)** only when every required scenario has valid preconditions, action, observable expected results consistent with its linked business rules, Gherkin, and a predefined test mapping; retain both check results. Deterministic validation examines this content, not only scenario identifiers.

- If the gap remains, escalate to human review with the unresolved coverage evidence and stop execution; no second automatic remediation occurs.

**Key rules/edge cases:** Count unique required scenario IDs; duplicates do not increase coverage. Unsupported scenarios do not expand the denominator. Presence alone is insufficient. Required Scenario Coverage equals valid required scenarios divided by the fixed five required scenarios; it is not code coverage, exhaustive testing, or proof of defect-free behavior.

**Outputs:** Rule-to-scenario coverage matrix for BR01–BR06, initial gap report, version 2 scenarios/Gherkin, remediation record, final coverage report. Report Required Scenario Coverage separately from BR01–BR06 traceability; its denominator is five scenarios, not six business rules.

### F05 — Decider 2B bounded decision orchestration
**User story:** As a test engineer, I want Decider 2B to select permitted next actions within fixed bounds so that the agent demonstrates controlled decision making.

**Preconditions:** Active run has a validated state and action budgets.

**Acceptance criteria:**

- At strategy, coverage, ambiguity, failure, and completion checkpoints, Decider chooses among permitted actions using current evidence. Each output includes checkpoint, selected action, rationale, evidence references, decision status, and workflow state under the structured contract in Section 3. The chosen validated action must materially control the workflow.

- The controller deterministically validates output structure, checkpoint/action permissions, evidence references, workflow state, prerequisites, and budgets before each action. A request to execute without human approval or with unresolved critical ambiguity or coverage is rejected without invoking pytest. Decider cannot change business rules or executable tests.

- At most one remediation and one approved test batch occur. Repeated, unknown, malformed, or out-of-order decisions stop the run with a recorded reason.

- Decision events make the complete path from intake to terminal report reviewable.

**Key rules/edge cases:** The controller enforces bounds even if the model requests otherwise. Awaiting human input does not repeatedly invoke the model or consume execution budget.

**Outputs:** Decision log, state transitions, budget usage, terminal/block reason when applicable.

### F06 — Human approval
**User story:** As a test engineer, I want to approve the final scenarios and fixed test manifest so that execution occurs only after my explicit review.

**Preconditions:** Coverage recheck passes; final interpretation, scenarios, mapping, and T01–T05 manifest are visible.

**Acceptance criteria:**

- Before approval, no pytest process runs; the UI exposes Approve and Reject controls with clear run/artifact version context.

- Approval records a human action, timestamp, run ID, and approved artifact version plus manifest; only that approved set becomes executable.

- Rejection ends the run without execution and records the outcome. No response leaves the run awaiting approval.

- Artifact/manifest changes invalidate approval; stale approval and repeated UI clicks cannot authorize a different or duplicate batch.

**Key rules/edge cases:** No model-generated approval or implicit approval from viewing the page. Authentication/enterprise approval workflows are outside scope.

**Outputs:** Approval/rejection record and execution authorization bound to the reviewed artifacts.

### F07 — Five predefined pytest executions
**User story:** As a test engineer, I want the approved five pytest tests to run against the mock payment service so that the report contains real execution evidence.

**Preconditions:** F06 approved the current artifacts; fixed T01–T05 suite and mock fixture are available.

**Acceptance criteria:**

- Execute the five predefined executable pytest tests T01–T05, including unchanged T04, exactly once each in one batch against isolated mock state; generated scenarios and Gherkin are not executable inputs. Maintain the S01–S05 to T01–T05 traceability; capture per-test status, assertions, duration, and relevant gateway/ledger evidence.

- In the injected-defect fixture, the observed result is T01, T02, T03, and T05 PASS and T04 FAIL; preserve the failing pytest outcome.

- Runner errors, missing tests, unexpected extra tests, skipped tests, or incomplete results are reported as execution errors/incomplete verification, never as five successful executions.

- Persist pytest output and structured results under the run ID; a UI refresh or repeat click does not rerun the suite.

**Key rules/edge cases:** No live gateway, dynamic pytest generation, test mutation, arbitrary command execution, or automatic rerun. Preserve evidence from successful and failing tests.

**Outputs:** T01–T05 execution results, pytest output/exit status, mock evidence, execution summary.

### F08 — Injected-defect failure analysis
**User story:** As a test engineer, I want an evidence-based explanation of the duplicate-callback failure so that I can distinguish the violated requirement from a runner problem.

**Preconditions:** F07 has captured test outcomes and available failure evidence.

**Acceptance criteria:**

- For the expected T04 failure, analysis identifies BR06, duplicate delivery of C-001 for P-001, expected ledger count 1, and observed count 2.

- Explain that a second ledger write violates callback idempotency; distinguish proven observations from a hypothesized code-level cause.

- Cite the failing assertion and relevant ledger/callback evidence. Missing evidence is labeled unavailable rather than fabricated.

- Unexpected failures and infrastructure errors are reported separately; if no test fails, state that no failure was observed instead of inventing the injected-defect result.

- Decider selects INVESTIGATE_FAILURE or REPORT_FAILURE from the captured evidence; record the choice and perform only its bounded permitted action before completion.

**Key rules/edge cases:** No automatic service patch, test patch, remediation deployment, or execution rerun. A suggested cause is not a confirmed source-code location without supporting evidence.

**Outputs:** Failure analysis, failure-handling decision, violated-rule mapping, evidence references, uncertainty/infrastructure notes as applicable.

### F09 — Final reporting and audit trail
**User story:** As a test engineer, I want a downloadable final report and audit trail so that I can review what the agent decided, what I approved, and what execution actually proved.

**Preconditions:** Run is completed, rejected, blocked, or stopped; collected artifacts remain available.

**Acceptance criteria:**

- Final report identifies run/source versions, BR-to-scenario-to-test traceability, initial/final Required Scenario Coverage, the one remediation, decisions, approval outcome, execution results, and analysis.

- For the expected demo, report explicitly states Required Scenario Coverage 4/5 (80%) → 5/5 (100%), tests 4 PASS/1 FAIL, and payment retry and duplicate prevention verification FAILED due to BR06. Agent workflow completion is a separate status.

- Blocked/rejected/error reports show the stopping reason and label unperformed stages NOT RUN; never imply execution or approval occurred.

- Streamlit offers a downloadable Markdown report; local run artifacts retain ordered timestamps and evidence references sufficient to follow each reported finding.

**Key rules/edge cases:** Reports reflect captured evidence, not anticipated outcomes. Required Scenario Coverage is not code coverage, exhaustive testing, or proof of defect-free behavior. Do not include credentials or model secrets. No external reporting integration is required.

**Outputs:** Final Markdown report, local audit events, linked run artifacts.

## 6. Out of scope

- Additional requirements, functional stories, scenario families, or executable payment tests beyond this fixed set.

- Multiple collaborating agents, autonomous planning loops, additional remediation cycles, self-healing code, or automatic failure reruns.

- Real payment gateways, external settlement reconciliation, refunds, partial payments, asynchronous race/concurrency testing beyond the scripted delayed-success case, or differing callback payloads/IDs.

- Production deployment, production security/compliance, user management, enterprise approval, persistent external databases, CI/CD, or third-party test/reporting integrations.

- Generating executable tests from Gherkin, importing existing test suites, arbitrary tools, performance/security/load testing, or code-coverage targets.

## 7. Final outcome and implementation checks

The delivered application must show the predefined payment use case, generated scenarios and Gherkin, Required Scenario Coverage and business-rule traceability before and after remediation, an explicit human approval, five real pytest outcomes, evidence of the duplicate-ledger defect, and Decider decisions that control the workflow.

**Implementation checks:**

- [ ] Exactly F01–F09 are implemented with the outputs and acceptance criteria above.

- [ ] BR01–BR06 remain separate from agent controls and are reflected in the scenarios and test assertions.

- [ ] Initial S04 omission is detected; one permitted remediation restores Required Scenario Coverage from 4/5 (80%) to 5/5 (100%) and covers all six business rules.

- [ ] Human approval gates the fixed T01–T05 suite; no refresh, stale approval, or model decision bypasses it.

- [ ] Defect-on execution produces four passes and an evidence-backed T04 failure.

- [ ] Decider chooses remediation and failure handling using evidence and valid alternatives.

- [ ] Final report distinguishes completed agent work from failed payment-service verification.

**Coding handoff:** Implement only this specification. Keep business logic, generated artifacts, deterministic validators, decision orchestration, and predefined pytest code separate. Choose concrete schemas and model/invocation configuration within these bounds; flag any business-rule conflict instead of adding scope. Preserve the controlled omission and configurable duplicate-ledger defect for repeatable demonstration.


