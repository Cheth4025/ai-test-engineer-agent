# Agent Build Requirements

**Project:** AI Test Engineer Agent  
**Use case:** Payment Retry & Duplicate Charge Prevention  
**Status:** Documentation and proposed technical design; no application built or AWS deployment performed.  
**Revision 2 (Oct 13, 2026):** Decider runs via `strands-decider serve` (local for development, EC2 GPU for the demo), answers carry native confidence, low confidence escalates to the human, and Decider is wired in through a Strands `InterventionHandler` published as the reusable `decider_gate` pattern. Hackathon deliverables are planned in [10](10_Hackathon_Submission_Plan.md).

## 1. Authority and boundaries

The current AI_Test_Engineer_Agent_Requirements.md is the functional authority. Its F01–F09, BR01–BR06, S01–S05, T01–T05 and acceptance criteria prevail over this technical elaboration. The Word document supplies no requirements. [README](README.md) identifies the source fingerprint; [09](09_Traceability_and_Validation.md) preserves the original acceptance criteria for verification.

**Confirmed constraints:** Python, developed on local Windows; one Strands orchestrator with specialized tools; Decider 2B for bounded, confidence-scored decisions (served locally in development and on AWS for the demo); Amazon Bedrock for generation; Streamlit; pytest; local mock service; deterministic synthetic in-memory fixtures; Git; local artifacts; downloadable Markdown report. Exactly five executable tests; one deliberate S04 omission; maximum one automatic coverage remediation; one configurable duplicate-ledger defect; human approval before execution. No independent agents.

**Recommended technical choices:** In-process mock; validated JSON/Python-compatible records; one application process and one sequential worker; plain local JSON artifacts and a small decision/event log; a fixed pytest subprocess argument vector. Approval and Execute are separate human actions; code freeze is October 20, 2026 and hackathon submission is October 23, 2026. These elaborate implementation, not business scope. Hosting/model dependencies are gated in [Open Decisions](Open_Decisions.md).

## 2. Purpose and outcome definitions

Users are the reviewer/test engineer and the automation engineer acting as Test SME and Quality Gate. The agent exposes test-design gaps, real assertions and consequential decisions. It does not process real payments or external settlement.

Payment correctness requires original-status verification after timeout, identical-key/payload retry, conflicting-amount rejection, delayed-success reconciliation, and one gateway charge/ledger posting. BR01–BR06 are reproduced unchanged in [07](07_Test_Automation_Design.md).

**Workflow COMPLETED:** Valid scenarios; accepted coverage; current explicit approval; five complete pytest outcomes; evidence-backed failure analysis when needed; validated completion decision; report/audit saved. Defect-on expectation: T01/T02/T03/T05 PASS, T04 FAIL. This is a completed agent workflow with payment verification FAILED. Never suppress a failure, change assertions, repair code, or rerun automatically.

**Incomplete:** Critical ambiguity, unresolved coverage, rejection, invalid decision, model/tool/runner/storage failure. Retain evidence and label unperformed stages NOT RUN. A defect-off complete batch may report payment verification PASSED for this suite, never exhaustive correctness.

**Required Scenario Coverage:** Valid required scenario content and mappings divided by the fixed five S01–S05. Initial 4/5 (80%); after S04 remediation 5/5 (100%). Not code coverage, exhaustive testing or proof of defect-free behavior. BR01–BR06 traceability is separate. IDs alone do not establish coverage.

## 3. End-to-end workflow

States and transitions are canonical in [05](05_Data_Contracts.md): twelve high-level states with a readable stage label, owned by one local controller. Store artifacts before advancing; save execution_started before launching the child. Unresolved critical evidence enters BLOCKED; technical/invalid-action errors enter STOPPED; rejection enters REJECTED. These produce controller-only incomplete summaries, without a completed FINALIZE_RESULTS claim.

| Activity / trigger | Inputs | Processing responsibility | Outputs | Success / failure condition | Next state |
|---|---|---|---|---|---|
| 1 Load / Start | Bundled source, catalog | Intake validates and snapshots (F01) | Source hash/version, run ID | Exact nonempty source / missing input or storage error | PREPARING or STOPPED |
| 2 Interpret / snapshot ready | Source, six rules | Bedrock interpretation plus deterministic source comparison (F02) | Meaning, rule links, ambiguity records | Correct interpretation / malformed output or conflict | PREPARING for strategy or ambiguity checkpoint; STOPPED on malformed output |
| 3 Strategy / interpretation ready | Interpretation and action criteria | Decider ambiguity (`noul` per rule) and strategy (`choice`) with confidence; low confidence escalates to the human (F05) | Decision | Valid choice / rejected or unavailable inference | PREPARING, AWAITING_CLARIFICATION or STOPPED |
| 4 Generate / valid strategy | Source, fixed catalog | Generate S01/S02/S03/S05 and Gherkin, omit S04 (F03) | Set v1, omission event | Four valid artifacts / invalid output | PREPARING or STOPPED |
| 5 Validate / set v1 saved | Content, rules, manifest | Deterministic content validation plus Decider grounding check per scenario (flags only) (F04) | Initial matrix, grounding flags | Valid comparison / absent or malformed inputs | PREPARING or STOPPED |
| 6 Detect / initial matrix ready | Matrix, expected catalog | Present S04/BR06 gap without removing T04 (F04) | Gap, coverage 4/5 | Missing content established / inconsistent evidence | PREPARING or STOPPED |
| 7 Decide remediation / gap ready | Matrix/gap, grounding, unused remediation | Decider `choice` with confidence and controller guard; REMEDIATE_GAP returns Guide to the agent (F05) | Selected action, reservation | Valid REMEDIATE_GAP / invalid acceptance or call failure | REMEDIATING or STOPPED |
| 8 Add scenario / reservation | Gap and immutable original set | Generate S04 only, preserve other four (F03/F04) | Set/Gherkin v2 | Correct addition / failure or unrelated changes | REMEDIATING or STOPPED |
| 9 Revalidate / v2 ready | Content and catalog | Recheck and coverage decision (F04/F05) | Final matrix/decision | 5/5 accepted / persistent gap or invalid decision | AWAITING_APPROVAL, BLOCKED or STOPPED |
| 10 Review / accepted coverage | Full review bundle | Wait for explicit human approve/reject (F06) | Bound approval record only; no launch | Current approval only / reject or stale bundle; no reply stays waiting | APPROVED, REJECTED or AWAITING_APPROVAL |
| 11 Execute / separate human Execute click | Approval/hash, fixed manifest | Recheck authorization on Execute; reserve sole batch; launch fixed pytest (F07) | Execution ID/process record | Five scheduled / authorization or launch failure | EXECUTING or STOPPED |
| 12 Capture / child exits | JUnit, stdout/stderr, fixture logs | Normalize actual five outcomes (F07) | Results, return code, evidence | Complete batch / skip, extra/missing test, setup error or crash | ANALYZING_RESULTS or STOPPED |
| 13 Analyze / results saved | Actual assertions/events | Bedrock-assisted factual analysis with deterministic evidence checks (F08) | Findings or no-failure note | Supported observations / missing required evidence or tool error | ANALYZING_RESULTS for failure checkpoint if failures; REPORTING if none; STOPPED on error |
| 14 Failure decision / findings ready | Findings and available evidence | Decider failure class, then INVESTIGATE_FAILURE once or REPORT_FAILURE (F05/F08) | Decision and final findings | Valid choice / invalid or analysis error | Remain ANALYZING_RESULTS for one investigation then REPORTING; direct REPORTING for reporting choice; STOPPED |
| 15 Finalize / evidence ready | Required artifacts, audit, approval | Decider completion `noul` plus deterministic readiness guard; report renderer (F09) | Report.md, metadata, audit | Saved consistent report / missing artifacts or write error | REPORTING then COMPLETED; STOPPED |

Any Decider answer below the configured confidence threshold pauses at stage AWAITING_DECISION_CONFIRMATION (no new workflow state): the human sees the options, Decider's suggestion and probabilities, and picks; the same controller guards apply to the human's pick. Activities 5–6 share one validation pass. Recheck acceptance and completion are separate Decider checkpoints. At the AMBIGUITY checkpoint, REQUEST_CLARIFICATION is the only permitted action. Human replies are separate records explaining immutable source meaning. A resolving reply triggers deterministic revalidation and returns to the pending checkpoint; it cannot change source/version/rules or authorize another generation cycle. If existing scenarios require further correction after the sole remediation, BLOCKED. No reply leaves the run waiting without inference polling.

## 4. Component specifications

| Component / F IDs | Purpose and inputs | Outputs | Dependencies | Error behavior |
|---|---|---|---|---|
| Requirement Intake Controller / F01 | Load approved source/catalog and create run | Snapshot/hash | Source, state manager | Missing input stops before inference |
| Requirement Interpreter / F02 | Extract meaning/rules/critical ambiguity | Interpretation/source links | Bedrock, validator | Malformed output stops; ambiguity requests human reply |
| Scenario and Gherkin Generator / F03 | Initial four or missing S04 review artifacts | Versioned scenarios/Gherkin | Bedrock, fixed catalog | Invalid output stops; executable-code output rejected |
| Coverage Validator / F04 | Compare content/assertions/Gherkin/mappings | Matrix/gap | Independent catalog | Persistent post-remediation gap blocks execution |
| Decider guardrail (`decider_gate`) / F05 | Ask Decider at each checkpoint via the Strands InterventionHandler; apply confidence threshold; map to Proceed/Guide/Deny/Confirm | Decision with confidence, latency, host label | Decider server (local or EC2), controller guards | Fail closed; no substitute model or fabricated decision; low confidence escalates to the human |
| Human Approval Controller / F06 | Bind explicit approval to current bundle | Approval/rejection | State manager, UI | Stale/model approval rejected |
| Test Execution Tool / F07 | Execute fixed suite once | Five results/pytest/fixture evidence | Python/pytest, mock | Preserve partial failure; no rerun |
| Failure Analysis Tool / F08 | Summarize facts; one bounded deeper read after the failure choice | Findings/rule links/uncertainty | Results, Bedrock generation/analysis | Unavailable evidence labeled; technical failure stops |
| Report Generator / F09 | Render saved facts, states and decisions | Markdown/metadata | Artifacts | No invented results; save failure disables download |
| Audit and Workflow State Manager / F01–F09 | Own state, one local lock, used flags and simple file writes | Run state and decision/event log | Filesystem, worker | Stop before side effects when persistence fails |

These are components/tools, not agents. The single Strands agent is restricted to the current operation. Guard every tool at invocation; its generative tool loop cannot bypass the deterministic state manager or become an autonomous workflow loop. Approval and execution entry points are controller-owned, not unrestricted model tools: only the real human Execute UI event can create/reserve the execution request. A model-created request or model tool call cannot launch tests even in APPROVED state.

## 5. Lightweight safeguards and targets

One local state manager and one sequential worker are sufficient. No command ledger, generic event-sourcing framework, artifact-manifest service or automatic restart recovery is required.

| Area | Required check / simple implementation |
|---|---|
| Approval and single execution | Approve records the current bundle only. Execute separately checks it; under one lock set execution_started=true and save run_state.json before child launch. Never reset that flag in the run. |
| Remediation | Set remediation_used=true before generating S04. Persistent gap blocks execution; no second cycle. |
| UI refresh/repeated clicks | One process-owned current-run object survives browser reload. Rerenders read it only; state/used flags reject duplicate Start Analysis, Approve or Execute. |
| Model/tool failures | One attempt per operation; malformed output/timeout stops visibly and retains evidence. No hidden SDK retry loop, fake response or substitute decision. Low Decider confidence is not a failure: it escalates to the human. |
| Deadlines | Connect 5 seconds; Decider call 10 seconds on GPU (30 on CPU dev); Bedrock generation/analysis 120 seconds; pytest batch 60 seconds. These are failure bounds, not performance promises. |
| Evidence | Preserve actual results and source/scenario versions; basic JSON files and chronological events. Save JSON with a temporary-file replacement; no multi-file transaction platform. |
| Credentials | Named short-lived AWS profile, no secrets in source/logs, loopback-only UI, no AWS credentials in payment-test child. |
| Stability | One active run, sequential worker, fixed subprocess, fresh in-memory fixture per test. Child timeout retains partial evidence and consumes execution opportunity. |
| Restart | Never replay an interrupted operation. On process restart display stored facts and mark unfinished work STOPPED; new work needs a new run and approval. No restart/resume framework. |
| Repeatability | Independently approved fresh runs yield the same fixture assertions; expected defect-on result remains four PASS/T04 FAIL. No automatic rerun. |

Detailed guards and minimal records are in 05; four UI sections and separate controls are in 06. Local artifact writes failing before execution stop rather than proceeding without a saved authorization/used flag.

## 6. Scope and dependency gates

Only synthetic content crosses inference boundaries. Generated Gherkin is displayed, never executed. Execution takes validated records, not model-supplied commands. No uploaded arbitrary requirements, extra tests, independent agents, dynamic imports/execution, real gateways, databases, production application deployment, enterprise integrations, authentication or autonomous repairs.

Approve one bundle hash binding the immutable source, interpreted meaning, final scenario/Gherkin content, accepted coverage, fixed five-test manifest and defect setting; source/test-code fingerprints are checked before launch. Runtime clarification cannot alter that immutable requirement. A changed bundle invalidates approval rather than permitting silent execution.

Integrated readiness depends on the Decider spike (wire format, confidence fields, model revision), a Bedrock generation model enabled in the account, and — for the demo — the EC2 GPU host. See [04](04_Decider_Decision_Design.md), [08](08_Claude_Code_Handoff.md) and [Open Decisions](Open_Decisions.md). Provisioning the single demo EC2 instance needs the product owner's go-ahead under O02; no other infrastructure is in scope.
