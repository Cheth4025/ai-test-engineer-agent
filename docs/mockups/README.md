# UI mockups: layout reference only

These four images show **layout, card hierarchy, spacing and visual style** for the four tabs. They are **not** functional authority.

If a mockup disagrees with `AI_Test_Engineer_Agent_Requirements.md` or `06_Streamlit_UI_Specification.md`, the documents prevail. Every number on screen must come from real run records, never from these images.

| File | Tab |
|---|---|
| `01_overview_requirements.png` | 1: Overview & Requirements |
| `02_scenarios_coverage.png` | 2: Scenarios & Coverage |
| `03_approval_execution.png` | 3: Approval & Execution |
| `04_results_report.png` | 4: Results & Report |

## Known deviations: do NOT copy these from the images

| # | What the mockup shows | What to build instead |
|---|---|---|
| 1 | Sidebar with 5–6 pages and a 7-step stepper; the navigation differs between images | One Streamlit page with **four native tabs** (06 §1). A simple stage indicator is fine. |
| 2 | A single **"Approve & Execute"** button | **Separate** Approve, Reject and Execute buttons. Approve launches nothing (F06/F07, 06 §3). |
| 3 | BR wording on the Overview, e.g. BR04 = "check status before retrying" | Show the **exact** BR01–BR06 text from the requirements file (BR04 = a timeout is not marked failed; it stays unresolved until verified). |
| 4 | Scenario→rule mappings: S01/T01 = BR01, BR05; S02/T02 = BR01, BR02, BR05; S05/T05 = BR01, BR04, BR05 | Authority mappings: S01/T01 = BR01; S02/T02 and S05/T05 = BR01, BR02, BR04, BR05; S03/T03 = BR01, BR03; S04/T04 = BR06. |
| 5 | Decider "ADD_SCENARIO (S04)" with a written **Rationale** | Decider is a 2B classifier: it returns an option (`REMEDIATE_GAP` / `ACCEPT`), a confidence and latency. It writes **no** rationale. Bedrock writes the S04 scenario. Never label the scenario "added by Decider". |
| 6 | Hard-coded confidence 0.87 and coverage 80% → 100% | Show values from decision and validator records. Add route (AUTO / ESCALATED), latency and host label. |
| 7 | No low-confidence escalation or grounding verdicts | Add the **Confirm decision** control for escalations and per-scenario grounding (GROUNDED / NEEDS_REVIEW / NOT_GROUNDED) in Tab 2. |
| 8 | An external "Payment Gateway", HTTP `200 OK` / `Rejected (400)`, `TXN123`, `ABC123`, `EVT-456` | An **in-process mock** with fixture P-001 / K-001 / C-001, 100.00 USD. There is no HTTP and no external gateway. |
| 9 | Per-test times of about 1 s, a total of 5.3 s, and fixed timestamps | Show only real pytest timings and timestamps. |
| 10 | Evidence tabs for "Screenshots" and "evidence.zip" | Evidence is plain **JSON + Markdown** in the run folder. Downloads: report.md plus the existing JSON artifacts. |
| 11 | Root cause stated as fact ("idempotency key was not checked") | Separate **proven** facts (BR06, C-001/P-001, ledger expected 1 / actual 2) from **hypothesised** causes (F08). |
| 12 | "Connected to AWS Bedrock" badge | Header shows the Decider **host label** from config, not a connectivity claim (06 §1). |
| 13 | Missing the "Agent workflow: COMPLETED" vs "Payment verification: FAILED" labels, NOT RUN labels and the Decider metrics strip | Add these to Tab 4 (06 §2). |

Worth keeping: the overall look, the card layout, the coverage donuts (driven by real data), the 4 PASS / 1 FAIL results table with T04 highlighted, the live log panel and the downloads list.
