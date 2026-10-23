# Open Decisions and Dependencies

**Project:** AI Test Engineer Agent  
**Use case:** Payment Retry & Duplicate Charge Prevention  
**Code freeze:** October 20, 2026 · **Submission:** October 23, 2026  
**Updated:** October 13, 2026, 11:20 PM ET (Revision 2, progress log added)

The previous O01 gate (custom SageMaker GPU container, verified by Oct 12) is replaced. Decider 2B is pip-installable, runs on CPU for development, and exposes a documented local HTTP server, so the main risk is now small and can be closed in a one-hour spike.

| ID | Dependency / decision | How to close it | Target | Status |
|---|---|---|---|---|
| O01 | Decider 2B runs and answers our question types | Spike in [04 §7](04_Decider_Decision_Design.md) | Oct 14 | **Done (Oct 14, 20:31)**: wire format from source, `choice` + `noul` + described criteria tested on GPU, 150–315 ms warm, deterministic. See log. |
| O02 | AWS host for the demo | Launch one EC2 g4dn.xlarge (T4) in a Bedrock-enabled region with a GPU Deep Learning AMI; install `strands-decider`; serve on localhost; reach it by SSM port forwarding; confirm GPU in use; record latency. Owner stops the instance when idle. Check vCPU quota for G instances first (quota requests can take time). | Oct 16 | **Done (Oct 14, 20:15)**: quota 8 vCPU approved; account upgraded to Paid plan (root user); `aite-decider-gpu` running with Tesla T4, Decider served on localhost:8000. |
| O03 | Bedrock model for generation/analysis and the benchmark judge | Confirm which model IDs/inference profiles are enabled in the account and region; one structured-JSON generation call; pin `strands-agents` and `boto3` versions; confirm `strands.interventions` imports | Oct 15 | **Partly done**: Anthropic use-case form submitted; agreements AVAILABLE for Claude Haiku 5.5 and Sonnet 5.5; inference profiles found. **Working pair confirmed:** Sonnet 4.6 + Haiku 4.5 both answer. 5.5 models still return AccessDenied (retry Oct 14; support case if still blocked). Package pinning and interventions import not yet done |
| O04 | Hackathon rules check | Rules read Oct 13: "build a working agent **on AWS**"; three pass/fail parts (article, public OSI-licensed repo with a clearly labelled reusable pattern, public video < 3 min); submission window opens Oct 16, 9:00 AM PT. See [10 §1](10_Hackathon_Submission_Plan.md). | Oct 13 | **Closed** |
| O05 | Where the demo app runs | **Decided by the rules ("agent on AWS"):** develop on Windows; the submitted/demo agent (Streamlit app, Strands agent, Decider server) runs on the EC2 instance, Bedrock via instance role, viewed through SSM port forwarding. The video shows it running there. | Oct 13 | **Closed** |

## Budget guide (verify current prices)

- EC2 g4dn.xlarge on-demand is roughly $0.5–0.65/hour depending on region. ~15 hours of use for spike, integration, rehearsal and recording ≈ $10. **Stop the instance whenever not in use**; terminate after submission.
- Bedrock: a few dollars for generation, analysis and a ~40-item benchmark.

## Fixed decisions (not open)

F01–F09, BR01–BR06, S01–S05, T01–T05 unchanged. One Strands orchestrator. Bedrock for generation/analysis; Decider for the five F05 checkpoints plus scenario grounding. Confidence threshold escalates to the human. Separate Approve / Reject / Execute. One remediation, one test batch, one investigation. Configurable duplicate-ledger defect. Four-tab Streamlit UI. In-process deterministic mock. Plain local JSON evidence. Declared pattern `#guardrails`.

## If something slips

- **Decider wire format differs from the docs:** adapt `DeciderClient` only; nothing else changes.
- **No GPU quota by Oct 16:** run the AWS demo host on CPU (c7i/m7i class) and state the CPU latency honestly, or use SageMaker if quota exists there. Do not swap in another model as "Decider".
- **Interventions API missing in pinned Strands:** use the controller-call fallback in [04 §2](04_Decider_Decision_Design.md).
- **Running late on Oct 19:** cut the optional `score` question and the hybrid benchmark row before cutting the article or video.

## Progress log

### Oct 13, 2026 (evening)

| Step | Result |
|---|---|
| GPU quota | Increase to 8 vCPU requested for **Running On-Demand G and VT instances** in us-east-1 (applied value was 0). Track in Service Quotas → Quota request history. |
| Budget alarm | Monthly cost budget, $50, created via template (verify in Billing → Budgets). |
| Anthropic use-case form | Submitted from the Bedrock model catalog (us-east-1). Applies account-wide. |
| Bedrock agreements | First call to Haiku 5.5 failed: `agreementAvailability = NOT_AVAILABLE` (authorized, entitled, region available). Fixed by creating agreements manually; both now **AVAILABLE**. |
| Inference profiles | `us.anthropic.claude-sonnet-5-5`, `us.anthropic.claude-haiku-5-5` (also `global.` variants). |
| Invoke test | `us.anthropic.claude-haiku-4-5-20251001-v1:0` → **ok**. `us.anthropic.claude-sonnet-4-6` → **ok**. `us.anthropic.claude-haiku-5-5` and `us.anthropic.claude-sonnet-5-5` → **AccessDenied: "not available for this account"** despite AVAILABLE agreements. Account, payment and Anthropic form are fine; only the newest models are blocked. |

**Commands that worked (CloudShell, us-east-1):**

```bash
export AWS_PAGER=""     # avoid the pager swallowing input
MODEL=anthropic.claude-haiku-5-5
TOKEN=$(aws bedrock list-foundation-model-agreement-offers --model-id $MODEL --region us-east-1 --query "offers[0].offerToken" --output text)
aws bedrock create-foundation-model-agreement --model-id $MODEL --offer-token "$TOKEN" --region us-east-1
aws bedrock get-foundation-model-availability --model-id $MODEL --region us-east-1 --query agreementAvailability.status
aws bedrock list-inference-profiles --region us-east-1 --query "inferenceProfileSummaries[?contains(inferenceProfileId, 'claude-haiku-5-5')].inferenceProfileId" --output text
aws bedrock-runtime converse --model-id us.anthropic.claude-haiku-5-5 --region us-east-1 --messages '[{"role":"user","content":[{"text":"Say ok"}]}]' --query "output.message.content[0].text" --output text
```

**Current build pair (verified):** `us.anthropic.claude-sonnet-4-6` (generation/analysis) and `us.anthropic.claude-haiku-4-5-20251001-v1:0` (benchmark judge). Switch to 5.5 by config only if access opens. If still blocked on Oct 14, open a support case ("Bedrock model access: Claude 5.5 AccessDenied despite AVAILABLE agreement"). The design is model-version independent; only config changes.

### Oct 14, 2026 (00:00–01:00)

| Step | Result |
|---|---|
| Laptop attempt (WSL2, CPU) | Install OK (Python 3.12 via uv, torch 2.14.1+cpu, strands-decider 0.1.0). Model download filled C: (0.5 GB free) → WSL disk went read-only. Recovered: deleted caches, compacted `ext4.vhdx` with diskpart → 9.2 GB free. **Decision: no local models; Decider runs on AWS.** |
| AWS plan | Account is on the **Free plan**; `m7i.xlarge` not allowed. Upgrade to Paid plan failed ("An error occurred while processing your request") → retry / support case. **Needed before the GPU instance.** |
| IAM role | `aite-demo-ec2-role`: AmazonSSMManagedInstanceCore + inline `aite-bedrock-invoke`. |
| CPU host | `<INSTANCE_ID>` `aite-decider-cpu`, **m7i-flex.large** (free-tier eligible, 2 vCPU, 7.6 GB), Ubuntu 24.04, 30 GiB gp3, no inbound rules, Session Manager access, 8 GB swap file. |
| Model | `StrandsAgents/strands-decider-2B-hobson-v19` (4.55 GB) + base `Qwen/Qwen3.5-2B-Base` downloaded from Hugging Face. Response reports `"model": "strands-decider-0.1.0"`. |
| First decision | COVERAGE choice → **REMEDIATE_GAP**, probabilities ACCEPT 0.4527 / REMEDIATE 0.5473, **confidence 0.0946**. Correct but uncertain → would escalate under τ = 0.9. |
| Latency | `real 11m24s`, `user 29s`: dominated by model load + swapping (fp32 ≈ 8 GB on a 7.6 GB host). Not representative. Library hint: install `flash-linear-attention` for the optimized kernel (GPU). |

**Confirmed response format (CLI `--json`, same as `/v1/systemone`):**

```json
{
  "model": "strands-decider-0.1.0",
  "answers": {
    "choice_0": {
      "type": "choice",
      "choice": "REMEDIATE_GAP",
      "probabilities": {"ACCEPT_COVERAGE": 0.4527, "REMEDIATE_GAP": 0.5473},
      "confidence": 0.0946
    }
  },
  "usage": {"input_tokens": 94, "output_tokens": 1}
}
```

**Design implications:**
- `confidence` = margin between the top two option probabilities (0.5473 − 0.4527), not the top probability. τ = 0.9 therefore requires ≈ 0.95 on the winner. Calibrate τ on the benchmark set; consider τ ≈ 0.5–0.7 if 0.9 escalates almost everything.
- Wording matters: next trials should compare (a) the current `choice`, (b) a `noul` "Is any required scenario missing?", (c) a richer state listing each scenario's validator status.
- CPU host is fine for format checks only; real latency numbers come from the GPU host.

### Oct 14, 2026 (evening): GPU host and wording spike

| Step | Result |
|---|---|
| GPU quota | **Approved** (8.0, CASE_CLOSED). |
| Paid plan | Launch failed with `InvalidParameterCombination: ... not eligible for Free Tier` until the **root user** ran Upgrade plan → email "Your AWS account was upgraded to a paid plan" (20:04). IAM admin upgrade kept failing. |
| Claude 5.5 | Still AccessDenied after the upgrade; continue on Sonnet 4.6 / Haiku 4.5. |
| GPU host | `<INSTANCE_ID>` `aite-decider-gpu`, g4dn.xlarge, AMI `<AMI_ID>` (Deep Learning Base OSS Nvidia Driver GPU AMI, Ubuntu 24.04, 20261006), 100 GiB gp3, SG `<SG_ID>` (no inbound), role `aite-demo-ec2-role`. Launched via CLI. |
| Stack | Driver 595.91, CUDA 13.2, torch 2.14.1+cu130 → `True Tesla T4`; strands-decider 0.1.0; flash-linear-attention 0.5.2 (works on T4; first request compiles ~60 s); `causal_conv1d` not installed (reference fallback). |
| Server | `strands-decider serve StrandsAgents/strands-decider-2B-hobson-v19 --device cuda --port 8000` in tmux; `/health` reports base Qwen/Qwen3.5-2B-Base, 24 slots, max_length 4096, prefix cache on. |

**Wire format (from `strands_decider/schema.py`):** request `{"state": str|dict|list, "questions": {name: question}}`. `choice` → `{"type":"choice","instructions":...,"criteria":{OPTION: description}}` (2–255 options). `noul` → `{"type":"noul","instructions":...,"criteria":{"true":...,"false":...}}` (criteria optional). `score` → `criteria` = ascending list of 2–10 levels. Response: `answers[name]` = `{type, choice, probabilities, confidence}` / `{type, noul}` / `{type, score, legend, probabilities, confidence}`, plus `usage`, `latency_ms`. Choice confidence = (N·p_max − 1)/(N − 1). The CLI sends **empty** option descriptions.

**Spike results (GPU, warm, each case run twice; identical both runs):**

| Case | Answer | Confidence / p | Server ms |
|---|---|---|---|
| coverage, bare labels | REMEDIATE_GAP | 0.24 | 154 |
| coverage, described options + structured state | REMEDIATE_GAP | **0.81** | 310 |
| grounding S04 correct | p(yes) **0.88** | margin 0.75 | 245 |
| grounding S04 wrong (two postings) | p(yes) **0.37** | margin 0.26 | 245 |
| grounding S02 wrong (timeout marked FAILED) | p(yes) **0.17** | margin 0.65 | 240 |
| failure class (T04 evidence) | BUSINESS_RULE_VIOLATION | **0.86** | 304 |

**Findings:**
1. All six answers are correct. Option descriptions + structured state roughly triple choice confidence (0.24 → 0.81).
2. Warm latency 150–315 ms on a T4 for 90–300-token states; deterministic.
3. τ = 0.9 would escalate everything. Proposed starting policy (calibrate on the benchmark set): **choice τ = 0.75**; **noul bands**: p ≥ 0.80 → GROUNDED, p ≤ 0.40 → NOT_GROUNDED, otherwise NEEDS_REVIEW.
4. The S04-wrong case (0.37) is the weakest; try a rule-specific grounding question (e.g. "Does the Then-clause require exactly one ledger posting after the repeated callback?") and per-rule criteria.

### Next

1. **Stop `aite-decider-gpu` whenever idle** (~$0.53/h). Terminate `aite-decider-cpu` (no longer needed) or keep it stopped.
2. Start the build (BUILD_BRIEF): mock + T01–T05 + validator on the laptop.
3. Build the benchmark set (~40 labelled items) and calibrate thresholds; try rule-specific grounding wording.
4. Recheck Claude 5.5 access; close the billing support case.
