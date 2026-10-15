# Streamlit UI Specification

**Project:** AI Test Engineer Agent  
**Use case:** Payment Retry & Duplicate Charge Prevention  
**Code freeze:** October 20, 2026 · **Submission:** October 23, 2026  
**Revision 2 (Oct 13):** Decider confidence, latency and host are shown; a Confirm decision control handles low-confidence escalations; grounding flags appear in Tab 2; a small Decider metrics panel appears in Tab 4. The UI is also the main surface of the demo video ([10](10_Hackathon_Submission_Plan.md)).

## 1. One page and four sections

Use one loopback-only Streamlit application page with four native tabs in the order below. Tab selection is navigation only, not a workflow action. Standard tables, metrics, expanders, code display and buttons are sufficient; light spacing/color styling only. Four visual mockups (in [mockups/](mockups/README.md)) are layout references, not functional authority; that README lists their known deviations, which must not be copied. Available dashboard images may inform card hierarchy and spacing, but their illustrative data, extra scenarios, history and settings are not requirements. Decider confidence **is** shown, from actual decision records. Pixel-perfect reproduction is not required.

The header shows AI Test Engineer Agent, the use case, run ID, current workflow state/stage, a small busy/error indicator and the Decider host label (e.g. `local-cpu`, `ec2-g4dn-t4`) read from config — not a connectivity claim. Use the canonical state and records in [05](05_Data_Contracts.md); no separate state per tab and no new agents. Exactly one active local run is supported. The defect setting comes from local run configuration, is shown read-only and is included in approval; no settings administration screen.

## 2. Four consolidated UI sections

| Section / F IDs | Display | Controls and behavior |
|---|---|---|
| 1 — Overview & Requirements / F01, F02, F05 | Payment Retry & Duplicate Charge Prevention; exact read-only bundled requirement; BR01–BR06; current state/stage; immutable source identity; critical ambiguity and clarification form when needed | Start Analysis creates one run and prepares review artifacts. No upload/editor. Conditional Submit clarification saves a separate human explanation, never changing source/version/rules. |
| 2 — Scenarios & Coverage / F03, F04, F05 | AI scenario descriptions and Gherkin; S↔T↔BR mapping; per-scenario Decider grounding verdict with confidence (GROUNDED / NOT_GROUNDED / NEEDS_REVIEW); initial Required Scenario Coverage 4/5 (80%) with a visible note that S04 is a seeded omission; missing S04/BR06; Decider remediation choice with confidence and latency; final 5/5 (100%) after content-valid addition | Review Scenarios focuses/expands existing generated artifacts only; it neither generates again nor approves/executes. Coverage values are computed from actual validator records, not constants or mockup numbers. |
| 3 — Approval & Execution / F06, F07 | Fixed T01–T05 manifest including unchanged T04; reviewed scenario-to-test mapping; final bundle and approval status; execution progress; actual pytest results/evidence | Separate Approve, Reject and Execute buttons with guards below. No raw commands or generated-test execution. |
| 4 — Results & Report / F08, F09, F05 | Actual five results; defect-on four PASS/T04 FAIL when observed; T04 expected ledger 1/actual 2, P-001/C-001 evidence and BR06; gateway charge evidence distinct from ledger postings; Decider failure class and action; complete decision audit table (checkpoint, suggestion, applied action, confidence, route AUTO/ESCALATED, latency, host); small metrics strip: number of Decider calls, median latency, number escalated | Download saved execution report.md and relevant existing JSON artifacts (results, coverage, analysis, decisions). Do not generate hypothetical evidence or rerun a model when downloading. |

Tab 4 labels **Agent workflow: COMPLETED** separately from **Payment verification: FAILED** in the intended defect-on run. Waiting/blocked/stopped/rejected outcomes retain their real status; unperformed tests/stages are NOT RUN. Before execution, show no actual PASS/FAIL totals. A 5/5 scenario set is not code coverage, exhaustive testing or proof of defect-free behavior.

## 3. Explicit control guards

| Control | Enabled when | Accepted effect |
|---|---|---|
| Start Analysis | Bundled source/config ready; no active run | Create new run ID; start preparation once. Duplicate clicks while active do not create a second run. |
| Review Scenarios | A saved scenario set exists | Navigate to/expand that set only; no side effect or authorization. |
| Confirm decision | Stage AWAITING_DECISION_CONFIRMATION; not busy | Shown in the tab of the pending checkpoint. Lists the allowed options with Decider's suggestion and probabilities preselected; the human picks one. Saved as decision_source=HUMAN_ESCALATION; the controller applies the same guards as for an AUTO decision. |
| Resolve grounding flag | A scenario is NOT_GROUNDED or NEEDS_REVIEW; before Approve | Human marks CONFIRMED_GROUNDED or CONFIRMED_NOT_GROUNDED; the latter makes the coverage row INVALID. |
| Submit clarification | AWAITING_CLARIFICATION; nonempty reply; controller not busy | Save human response; deterministic source-consistency recheck; return to pending stage only if meaning resolved. No response waits; critical unresolved conflict blocks. |
| Approve | AWAITING_APPROVAL; accepted 5/5; no critical ambiguity; no unresolved grounding flag; exact current bundle; not busy | Save LOCAL_HUMAN authorization and set APPROVED. **Does not execute tests or consume execution opportunity.** |
| Reject | AWAITING_APPROVAL or APPROVED; execution_started=false; not busy | Record rejection and enter REJECTED. Prevent execution for that run; no automatic rework. |
| Execute | APPROVED; current valid human approval matches bundle/source/test code; execution_started=false; not busy | Under the controller lock reserve sole batch by setting/saving execution_started=true, then launch fixed T01–T05. |
| Download Markdown / JSON | Corresponding real saved artifact exists | Download stored bytes only; unavailable artifacts have disabled controls. |
| New Analysis | Prior run terminal and no worker/child active | Explicit fresh run, fresh flags and new approval; no implicit execution replay. |

Execute stays disabled permanently for that run after reservation, including launch failure, timeout or completion. Repeated clicks/reloads cannot start another child. Approve is disabled once recorded; changing the bundle invalidates it and requires current review rather than silently retaining authorization. Unauthorized source/test changes stop the run.

## 4. Loading, failures and simple state ownership

One process-owned controller/current-run object and lock are shared by the UI and one worker. Streamlit session state holds display choices only. Browser reload reads the same current run; it never starts work. No generic command-key store, multi-user registry, query-parameter recovery system or distributed locks.

Streamlit's WebSocket session state resets on browser reload, so session state alone cannot enforce execution-once. [Session state](https://docs.streamlit.io/develop/api-reference/caching-and-state/st.session_state). A cached process resource/current controller plus run_state.json provides the local guard. Use a native read-only refresh fragment if already supported by the pinned Streamlit release, or a simple Refresh Status button; no additional polling library. Worker does not call UI APIs.

Show spinner/current stage while busy, readable failure reason, partial evidence and NOT RUN for unperformed stages. A missing source, invalid model selection, persistent gap, unresolved critical ambiguity, stale approval, runner error or storage failure never produces a fabricated success. Process restart does not resume side effects; unfinished prior work is STOPPED and can be reviewed read-only.

## 5. Exclusions and acceptance checks

No extra pages, login, users, administration, advanced dashboard, historical analytics, predictive insights, charting library or generated-code download/execution requirement. Benchmark results live in the repo/article, not in the app. Four-section layout and actual records take precedence over image labels/data. Do not display a connection-success badge without verified access.

Verify Start Analysis once, review without approval, Approve with zero child launches, separate Execute with one launch, Reject with none, double Execute/refresh with no second launch, missing evidence as unavailable, and truthful four-PASS/T04-FAIL reporting. Test the source-preserving clarification path, current-bundle invalidation, a forced low-confidence escalation (set the threshold to 0.999 in a dev run) and a grounding flag blocking Approve. These are manual UI/control checks around the fixed five payment tests, not additional executable payment tests.
