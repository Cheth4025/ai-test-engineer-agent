# AI Test Engineer Agent — Design Package (Revision 2)

**Use case:** Payment Retry & Duplicate Charge Prevention  
**Hackathon:** AWS Agent Experience Hackathon — Strands Decider (pattern: `#guardrails`)  
**Code freeze:** October 20, 2026 · **Submission:** October 23, 2026 (11:59 PM PDT)  
**Updated:** October 13, 2026  
**Verdict:** Ready for Claude Code to start with the build brief. Integration needs O01–O03 (Decider spike, EC2 GPU host, Bedrock access), all closable this week. Hackathon Rules reviewed Oct 13: the agent runs on AWS for the demo; three pass/fail submission parts; nothing published before Oct 16.

## 1. Authority

`AI_Test_Engineer_Agent_Requirements.md` remains the functional authority (SHA-256 below) and is **not** in this zip — supply it beside the package. Its F01–F09, BR01–BR06, S01–S05, T01–T05 and acceptance criteria are unchanged; the original stories and criteria are still reproduced verbatim in 09 §3, and BR01–BR06 and T01–T05 in 07.

```text
f846df6edb7e23ea70396a4a4f9e6f6b81a12101bb8c9eaccfa64db5cfe734da
```

## 2. Reading order

| Document | Purpose |
|---|---|
| [BUILD_BRIEF](BUILD_BRIEF.md) | **Start here.** One page: what to build, build order, three hard rules. |
| [Open Decisions](Open_Decisions.md) | O01–O03 open (Decider spike, EC2 host, Bedrock access); O04–O05 closed by the Rules. |
| [02 Build Requirements](02_Agent_Build_Requirements.md) | Fifteen activities, components, safeguards. |
| [03 Architecture](03_Agent_Architecture.md) | Strands wiring, eight diagrams, hosting facts. |
| [04 Decider Design](04_Decider_Decision_Design.md) | Confidence-gated guardrail pattern, five checkpoints + grounding, spike checklist. |
| [05 Data Contracts](05_Data_Contracts.md) | JSON records, twelve states, flags. |
| [06 Streamlit UI](06_Streamlit_UI_Specification.md) | Four tabs, Approve / Reject / Execute / Confirm decision. |
| [07 Test Automation](07_Test_Automation_Design.md) | Mock, five unchanged tests, T04 real failure. |
| [08 Claude Code Handoff](08_Claude_Code_Handoff.md) | Repo layout, dependencies, config, Oct 13–23 milestones. |
| [09 Traceability](09_Traceability_and_Validation.md) | Original acceptance criteria, seventeen checks, final checklist. |
| [10 Submission Plan](10_Hackathon_Submission_Plan.md) | Rubric mapping, benchmark, article outline, video script, repo checklist. |

## 3. What changed in Revision 2 and why

| Area | Before | Now | Why |
|---|---|---|---|
| Decider's role | Mostly confirmed answers the controller already knew; two one-option checkpoints | Real decisions at all five F05 checkpoints plus a per-scenario **grounding** check of Bedrock's prose/Gherkin | "Effective use of Decider" is 30% of judging; grounding fills a real gap in the validator |
| Confidence | Omitted | Used: ≥ threshold acts automatically; below escalates to the human with Decider's suggestion | Confidence is Decider's core feature; it ties into the human gate |
| Strands integration | Hand-built controller only | Decider wired through a Strands `InterventionHandler` (Proceed / Guide / Deny / Confirm); controller-call fallback | The hackathon names InterventionHandler as the intended pattern |
| Reusable pattern | Not packaged | `decider_gate/` package with its own README and generic example | Required submission item |
| Hosting | Custom SageMaker GPU container; local Decider forbidden; Oct 12 gate | `strands-decider serve`: local for dev, one EC2 g4dn.xlarge (T4) for the demo via SSM | Much lower risk and cost; documented HTTP API |
| Measurements | None | Benchmark: Decider vs Bedrock judge vs hybrid — accuracy, latency, cost | Bonus points for measured gains |
| Submission | Not planned | Article, < 3 min video, public repo, tags, schedule Oct 21–23 | Communication is 25%; all are mandatory |
| S04 gap | Presented as something the AI did | Labelled as a seeded omission (required by F03); runtime decisions are real | Avoids looking rigged |
| Readability | Ten dense documents | Added a one-page build brief; new sections written in plain language | Easier for both reviewers and Claude Code |

**Unchanged:** business rules, five tests and assertions, the T04 real failure, separate Approve/Execute, one remediation / one batch / one investigation, twelve states, four tabs, plain JSON evidence, no extra infrastructure beyond the single demo host.

**Not simplified (required by the authority):** the clarification path (F02) and bundle invalidation on change (F06) stay, kept as small as possible.

## 4. Scope at a glance

- One fixed requirement; one Strands agent; Bedrock writes, Decider judges; local Python/Streamlit/pytest/mock.
- Required Scenario Coverage 4/5 (80%) → 5/5 (100%) after one S04 remediation. Not code coverage or proof of correctness.
- Approve records; a separate Execute runs T01–T05 once. Defect on: four PASS, T04 FAIL (ledger expected 1, actual 2).
- Report: **Agent workflow COMPLETED**, **Payment verification FAILED** (BR06).
- Excluded: databases, web APIs, event sourcing, auto-resume, login, CI/CD, extra tests or use cases, generated-code execution.

## 5. Validation of this package

Document checks only: BR/test/story text compared with Revision 1, JSON examples parse, internal links resolve, Mermaid blocks fenced. Not runtime proof — see the 09 checklist.
