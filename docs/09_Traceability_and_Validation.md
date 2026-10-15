# Requirements Traceability and Validation

**Project:** AI Test Engineer Agent  
**Use case:** Payment Retry & Duplicate Charge Prevention  
**Revision 2 (Oct 13, 2026):** traceability for confidence-scored Decider decisions, grounding and the hackathon deliverables. Section 3 (original stories and acceptance criteria) is unchanged.

## 1. Validation boundaries

Agent functional validation checks F01–F09 controls and artifacts. Payment business-rule validation observes the fixed T01–T05 assertions against BR01–BR06. Generated Required Scenario Coverage checks review content S01–S05. Actual pytest outcomes are separate execution evidence. A 5/5 scenario set cannot turn T04's real failure into a pass.

The functional Markdown prevails. AC indices below label its existing bullet order solely for traceability; they do not add requirements or edit acceptance criteria. The source text is preserved in section 3, including every acceptance bullet. Technical design choices are separately described in documents 02–08.

## 2. Functional traceability matrix

| F ID / component | Input | Output | Acceptance criterion reference | Verification method | Evidence artifact |
|---|---|---|---|---|---|
| F01 Intake/state | Bundled immutable text/version | Unique snapshot/run | All F01 bullets | Compare displayed/source/audit text; missing input blocks; explicit new run clears approval; refresh preserves run | requirement.json, events.json, run_state.json |
| F02 Interpreter | Snapshot and BR01–BR06 | Meaning/ambiguity records | All F02 bullets | Source-linked rules; human clarification recorded separately; unresolved critical ambiguity cannot execute; hash/version unchanged | interpretation.json, events.json |
| F03 Generator | Valid interpretation/catalog | v1 scenarios/Gherkin, omission event | All F03 bullets | S01/S02/S03/S05 only initially; inspect semantic content/Gherkin; T04 executable remains in manifest | scenarios_initial.json (embedded Gherkin), fixed test manifest |
| F04 Validator | Generated content and independent catalog | Matrix/gap/v2/recheck | All F04 bullets | 4/5 S04 gap; valid one-cycle addition; invalid oracle/duplicate ID does not count; persistent gap blocks | coverage_initial/final.json (gap findings), decisions.json, events.json |
| F05 Decider guardrail (`decider_gate`) | Actual checkpoint evidence/options | Decisions with confidence, route, latency, host; state trace | All F05 bullets | Real Decider answers control dispatch at all five checkpoints; low confidence escalates and the human pick is guarded identically; rejected action/stale output/budget exhaustion exercised with unit-test fixtures, never fabricated integrated decisions | decider_calls/, decisions.json, grounding_*.json, events.json |
| F06 Approval | Final reviewed hash bundle | Human approve/reject record | All F06 bullets | Approve causes no child; separate Execute launches once; reject/no response/stale hash/repeat click/reload cannot launch | approval.json, events.json, execution_started flag |
| F07 Execution tool | Valid authorization/five manifest nodes | Five actual outcomes | All F07 bullets | Count node IDs/results; defect-on four PASS/T04 FAIL; simulate interrupted/error capture without claiming successful suite | JUnit, stdout/stderr, results, fixture records |
| F08 Analysis | Actual failed assertions/evidence | BR06 explanation/handling choice | All F08 bullets | Compare expected1/actual2 and callback identities; unsupported source hypotheses labeled; missing evidence explicit; one handling action | analysis.json, failure decision, raw fixture data |
| F09 Reporting | All collected artifacts | Downloadable report/metadata/audit | All F09 bullets | Compare report against saved facts; completed agent vs FAILED payment; incomplete states NOT RUN; no secrets | report.md, report_metadata.json, events.json, decisions.json |

Agent/controller validation can use deterministic isolated contract inputs for review. Such fixtures are not additional payment tests and cannot stand in for the genuine Decider integration or five-test execution. No sixth executable payment test is authorized.

## 3. Original functional stories and acceptance criteria

The following stories, preconditions, acceptance criteria, edge cases and outputs are reproduced from the authoritative Markdown, unchanged. They are a verification reference, not a second editable functional authority.

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

## 4. Payment-test and rule traceability

| Test / scenario | Business-rule mapping | Verification | Required evidence | Defect-on outcome |
|---|---|---|---|---|
| T01 / S01 | BR01 | Valid key, original success, one charge/posting | Submit/key/payment/charge/ledger | PASS |
| T02 / S02 | BR01, BR02, BR04, BR05 | Timeout unresolved; same payload/key; original-status verification; no second charge | Ordered timeout/retry/verification and final records | PASS |
| T03 / S03 | BR01, BR03 | Conflicting 125.00 request rejected; original 100.00 records unchanged | Before/after records and rejection | PASS |
| T04 / S04 | BR06 | Repeat C-001; assert ledger count one with original state/data preserved | Both deliveries and two defect-on entries; pytest assertion | FAIL |
| T05 / S05 | BR01, BR02, BR04, BR05 | Unresolved verification; delayed original callback; no second charge; retry returns original | Script sequence and identity/count evidence | PASS |

BR01–BR06 all have content/test mappings. Mapping is not exhaustive proof: the suite does not execute every hypothetical missing-key/key-expiration/concurrency behavior. No scope expansion is implied.

## 5. Seventeen cross-document checks

| Check | Location / document verification |
|---|---|
| 1 Four UI sections | 06's one native page/four tabs, matched by 02/03/08/README |
| 2 One orchestrator | One local Strands orchestrator; specialized functions/tools, no independent agents |
| 3 Genuine Decider | 04 native response retained; real choices with native confidence; AUTO vs ESCALATED route recorded; guarded transition and honest provenance |
| 4 Omission/remediation | Initial S04 absent, one addition, unchanged other four; no T04 removal |
| 5 Five executable tests | 07 preserves T01–T05 original sequences/assertions and fixed node allowlist |
| 6 Separate approval/execution | Approve records only; separate Execute after current authorization; Reject ends run |
| 7 One execution batch | execution_started saved before launch and never reset, including errors/timeouts |
| 8 Accurate coverage | Actual content matrix computes Required Scenario Coverage 4/5 (80%)→5/5 (100%); six-rule traceability separate |
| 9 T04 evidence | Real expected1/actual2 assertion; P-001/C-001 ledger records; one charge distinct from two postings |
| 10 Deployment | Dev on Windows (local Decider); demo on one EC2 GPU host via SSM; Bedrock for generation; no other infrastructure |
| 11 Evidence honesty | Every decision carries a host label; CPU-dev evidence never presented as the AWS demo; benchmark states dataset size and limits |
| 12 No excluded implementation | No database, event-sourcing/recovery platform, microservices, container orchestration, CI/CD, login, extra tests/use cases or generated execution |
| 13 Traceability preserved | All nine source stories/AC below unchanged, six BR statements and five test definitions unchanged in 07 |
| 14 Dates | Oct 13–20 build milestones and Oct 20 freeze in 08; Oct 21–23 submission plan in 10; targets, not guarantees |
| 15 Reusable pattern | `decider_gate/` has no payment imports, its own README and a generic example (08 §2, 10 §6) |
| 16 Grounding is flag-only | Grounding can block Approve or (after human confirmation) mark INVALID; never upgrades coverage (04, 05, 07) |
| 17 Hackathon deliverables | Pass/fail parts per the Rules: public OSI-licensed repo with labelled pattern (questions + option set, wiring, usage note), Builder Center article, public video < 3 min without third-party music/trademarks; agent running on AWS; tags; nothing published before Oct 16 (10) |

These are document checks, not claims of model invocation, runtime tests, deployment or working code. The available image references do not override scope/content metrics. Original source story blocks in section 3 remain verification excerpts, not a second editable authority.

## 6. Implementation-readiness and final validation checklist

- [ ] Test SME approves the preserved business rules/scenario oracles and this technical elaboration.
- [ ] Decider spike complete: wire format, confidence fields, model revision pinned (O01); EC2 GPU host verified in use with latency recorded (O02).
- [ ] Bedrock profile/model generation verified with approved AWS access and package versions.
- [ ] Source/test hashes and immutable source snapshot retained.
- [ ] Initial content coverage 4/5; sole valid S04 remediation produces 5/5 and six-rule traceability.
- [ ] Wrong content with a correct ID is rejected; unresolved post-remediation gap blocks execution.
- [ ] Critical ambiguity produces separate human clarification; source/version unchanged; no reply does not execute.
- [ ] Actual Decider choices at all five checkpoints and grounding are audited with confidence, route, latency and host; invalid outputs do not cause fabricated fallback.
- [ ] Forced low-confidence escalation pauses at AWAITING_DECISION_CONFIRMATION; human pick is guarded (ACCEPT at 4/5 rejected).
- [ ] A NOT_GROUNDED/NEEDS_REVIEW scenario blocks Approve until resolved.
- [ ] `decider_gate` generic example runs without the payment app.
- [ ] Benchmark run; results table with accuracy, p50/p95 latency, cost per 1k, escalation rate.
- [ ] Demo agent runs on AWS (EC2) for the video; `patterns/scenario_grounding_guardrail/` contains questions.json, wiring.py, README.md.
- [ ] Approve records authorization with zero launches; separate Execute starts the sole fixed batch; rejection, stale hash, double-click and refresh cannot duplicate it.
- [ ] All five actual results retained; T04 real FAIL, four PASS in defect-on run, no skip/xfail suppression.
- [ ] Analysis cites actual assertions/ledger/callbacks and separates infrastructure errors/hypotheses.
- [ ] Completed vs failed verification and terminal NOT RUN behavior match artifacts.
- [ ] Downloaded Markdown and evidence filenames agree with real saved records.
- [ ] Browser refresh preserves the current run without dispatch; process restart never replays work; report/events contain no credentials.
- [ ] Automation engineer independently verifies complete workflow with fresh explicitly approved runs.

**Readiness conclusion:** Scope, four-tab UI, contracts and the Decider guardrail design are ready for Claude Code, starting with the build brief. Integration depends on O01–O03 in Open_Decisions.md, all closable this week; submission work follows the Oct 20 freeze. Never label the application runtime-verified before the 09 checklist passes. No business-scope decision is delegated to the coding agent.
