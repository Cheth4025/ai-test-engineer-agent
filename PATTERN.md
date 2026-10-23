# Gating an agent's decisions with a small decider model

A reusable write-up of `decider_gate/` — the pattern this repo uses to let a small, fast model ([Strands Decider 2B](https://github.com/strands-labs/strands-decider)) make the easy calls at every key decision point in an agent's workflow, while a confidence threshold routes the hard calls to a human — with deterministic guards underneath that neither the model nor the human can bypass.

`decider_gate` is domain-agnostic: it has never imported anything from this repo's payment/test-engineer code, and it doesn't know what a checkpoint, a scenario, or a business rule is. The only domain knowledge lives in the calling application (`src/ai_test_engineer/checkpoints.py` here), supplied through a small, explicit interface.

## The shape of the problem

Most agent guardrails pick one of two extremes: a deterministic rule-check (cheap, fast, but can't handle judgement calls — "does this Gherkin actually assert what the oracle requires?") or an LLM-as-judge call (handles judgement, but is slow and costs real money on every single tool call, including the 95% that are obviously fine). This pattern sits between them: a small model handles the judgement call, a confidence score decides whether to trust it, and a deterministic guard decides whether to trust *that*.

## The components

### 1. `DeciderClient` — one HTTP call, never a fabricated answer

```python
class DeciderClient:
    """Thin HTTP wrapper around POST {url}/v1/systemone."""

    def ask(self, state: str | dict, questions: dict) -> dict:
        """POST one request with every named question against one state.
        Raises DeciderError on timeout, connection error, non-200 status,
        or a response missing an expected answer -- never returns a
        substitute/fabricated answer."""
```

Every question in one checkpoint goes in **one** request, carrying the shared `state` plus the per-question `instructions`/`criteria`. There's no retry and no fallback answer — a transport failure or a malformed response becomes a `DeciderError`, handled explicitly by the caller (see `DeciderGuardrail` below), never silently papered over.

### 2. `ConfidenceGate` — turns a raw answer into a routed `Verdict`

```python
class ConfidenceGate:
    def __init__(self, choice_threshold=0.75, noul_upper=0.80, noul_lower=0.40):
        self.choice_threshold = choice_threshold
        self.noul_upper = noul_upper
        self.noul_lower = noul_lower

    def _evaluate_choice(self, question, answer, latency_ms, host_label, model):
        choice = answer["choice"]
        confidence = answer["confidence"]          # the model's own top-two margin
        route = AUTO if confidence >= self.choice_threshold else ESCALATE
        return Verdict(question=question, answer=choice if route == AUTO else None,
                        confidence=confidence, route=route, suggestion=choice, ...)

    def _evaluate_noul(self, question, answer, latency_ms, host_label, model):
        p = answer["noul"]
        if p >= self.noul_upper:
            route, decided = AUTO, True
        elif p <= self.noul_lower:
            route, decided = AUTO, False
        else:
            route, decided = ESCALATE, None
        return Verdict(question=question, answer=decided, confidence=max(p, 1 - p),
                        route=route, suggestion=p >= 0.5, ...)
```

Two question shapes: `choice` (pick one of N named options) and `noul` (true/false). **Confidence routes a decision; it never decides one for you** — that line is the whole safety property of this pattern, worth repeating verbatim in any port of it. `suggestion` is *always* populated, even on `ESCALATE` — that's what lets a human (or an offline benchmark) see what the model *would* have picked, separately from whether it was trusted to act on it.

### 3. `CheckpointSpec` — the only place domain knowledge enters

```python
@dataclass
class CheckpointSpec:
    build_questions: Callable[[Any], dict]                       # -> {question_name: question_dict}
    resolve: Callable[[dict[str, Verdict]], Proceed | Guide | Deny]  # called only when every verdict is AUTO
    build_state: Callable[[Any], Any] | None = None               # defaults to the raw tool input
```

The calling application supplies one `CheckpointSpec` per guarded tool call. `build_questions` decides *what* to ask; `resolve` decides what an all-`AUTO` result means for the workflow (`Proceed`, redirect the model with `Guide`, or `Deny`). `resolve` is **never called on an escalation** — `decider_gate` handles that path generically, so domain code can't accidentally special-case it inconsistently.

### 4. `DeciderGuardrail` — the Strands `InterventionHandler` that wires it together

```python
class DeciderGuardrail(InterventionHandler):
    def before_tool_call(self, event, **kwargs):
        spec = self._checkpoints.get(event.tool_use["name"])
        if spec is None:
            return Proceed()                                     # ungated tool: pass through

        state = event.tool_use.get("input")
        questions = spec.build_questions(state)
        decider_state = spec.build_state(state) if spec.build_state else state
        key = _cache_key(tool_name, decider_state, questions)      # decision reuse, see below

        if cached := self._resolved.get(key):
            return self._finish(tool_name, tool_use_id, cached["action"])

        try:
            response = self._client.ask(state=decider_state, questions=questions)
            verdicts = {name: self._gate.evaluate(name, answer, ...) for name, answer in response["answers"].items()}
        except (DeciderError, KeyError, ValueError, TypeError) as exc:
            return self._finish(tool_name, tool_use_id, Deny(reason=f"Decider error: {exc}"))

        if any(v.route == "ESCALATE" for v in verdicts.values()):
            return Confirm(prompt=self._build_confirm_prompt(verdicts))   # generic human handoff
        return spec.resolve(verdicts)                                     # domain code decides
```

This is a [Strands `InterventionHandler`](https://strandsagents.com/docs/api/python/strands.interventions.handler/index.md) hooking `before_tool_call` — it runs *before* the agent's proposed tool call is allowed to execute. One checkpoint, one batched Decider call, one of exactly four outcomes: `Proceed` (go ahead), `Deny` (blocked, with a reason), `Guide` (redirect the model), or `Confirm` (pause for a human).

### 5. Escalation to a human, via Strands interrupts

`Confirm` doesn't resolve anything itself — it pauses the agent loop and surfaces the Decider's suggestion, confidence, and probabilities for every question that escalated. A human picks an option; on resume, Strands re-delivers that answer through `event.interrupt(...)`, and the guardrail applies the exact same `Proceed`/`Deny` logic a human would have gotten from an `AUTO` route — **the human's answer is never a side door around the deterministic guard**.

### 6. Deterministic guards override the model, always

```python
def resolve(verdicts: dict[str, Verdict]) -> Proceed | Deny:
    action = verdicts["coverage_action"].answer
    if action == "ACCEPT_COVERAGE":
        if not full_coverage(coverage):          # deterministic, NEVER overridable
            return Deny(reason=f"ACCEPT_COVERAGE denied: {coverage['covered']}/{coverage['required']} valid")
        return Proceed()
    ...
```

A `CheckpointSpec.resolve` can — and in this repo's own checkpoints, routinely does — reject an `AUTO` (or human-confirmed) answer outright if it contradicts evidence the controller already has. Neither the Decider's confidence nor a human's click can make `ACCEPT_COVERAGE` succeed at 4/5.

### 7. Decision reuse cache

```python
def _cache_key(tool_name: str, decider_state: Any, questions: dict) -> str:
    payload = json.dumps({"state": decider_state, "questions": questions}, sort_keys=True, ...)
    return f"{tool_name}:{hashlib.sha256(payload.encode()).hexdigest()}"
```

Strands can replay a tool call with a brand-new `toolUseId` carrying the *exact same effective state* (a retry after a human already answered an escalation for equivalent input). Keying the resolved action by a hash of `(tool, state, questions)` means that replay reuses the prior resolution instead of asking the Decider again and risking a second, possibly-inconsistent escalation for input the system has already settled.

### 8. Evidence logging, via plain callbacks

```python
DeciderGuardrail(client, gate, checkpoints, host_label,
    on_decision=lambda tool_name, verdicts, action: controller.record_decision(...),
    on_error=lambda tool_name, exc: controller.save_evidence("decider_error.json", {...}),
)
```

`decider_gate` never writes a file or knows a run ID — it just calls back. The caller owns persistence (`decisions.json`, `decider_error.json` here), which is what keeps the package reusable across completely different evidence formats.

## Lessons learned, the hard way

- **Embed concrete facts in each question's own `instructions`**, not just in a shared `state` blob. A spike in this repo measured a correct grounding call at **0.88** confidence with facts embedded directly in the question vs. **~0.77** relying on shared state alone for the identical input. Checkpoints that skipped this (`failure_action`, `strategy_action`, `completion_action`) measurably under-performed the ones that didn't (`failure_class`, `grounding_Sxx` after the fix).
- **Confidence for a `choice` question is a top-two-probability margin, not the top probability.** 0 means a coin flip between the best two options; it is *not* "how likely is the top answer." A 3-option question with probabilities 0.5/0.3/0.2 has a margin of only 0.2 — looks confident by eye, scores low by the margin that actually gates it. Benchmark against the value the gate actually routes on, never the raw top probability.
- **A checkpoint escalates on its weakest question, not its average.** Batch five questions together and the one with the lowest confidence decides whether a human sees the whole batch. Good for safety (any doubt escalates), expensive if one chronically-low-confidence question drags four confident ones along with it every time — worth watching per-question, not just per-checkpoint.
- **Benchmark with held-out items before touching thresholds.** It's easy to tune wording against the exact items you're scoring and call the improvement real. Label a few items *before* writing the wording fix, never look at their results while deciding the wording, then check afterward whether the fix generalized.
- **Prefer conservative failure modes when a question must sometimes get it wrong.** A false "not grounded" costs a remediation cycle or a human glance. A false "grounded" ships a bad test as if it were good. Design the question (and read the resulting errors) with that asymmetry in mind — which error is cheap, and which is the one you can't afford.

## Adapting this to another domain, in ~5 steps

1. **List your checkpoints** — the places in your workflow where an agent is about to do something consequential (write, delete, approve, finalize). Each becomes one guarded tool name.
2. **Write one `CheckpointSpec` per checkpoint**: `build_questions(state)` returning named `choice`/`noul` questions with **self-contained** instructions (embed the specific facts, don't rely on shared state alone — see above); `resolve(verdicts)` encoding the deterministic rules that must hold regardless of what the model or a human says.
3. **Pick thresholds you can defend**, not ones that feel safe. Start conservative (this repo's 0.75/0.80/0.40), then measure — build ~30-50 hand-labeled items per question type, hold a few out, and only move a threshold with a number behind it.
4. **Wire `DeciderGuardrail` into your agent's intervention chain**, mapping tool name → `CheckpointSpec`, with `on_decision`/`on_error` callbacks that persist whatever evidence format your system already uses.
5. **Decide what a human sees on escalation** — at minimum, the Decider's suggestion, confidence, and probabilities per question; this repo also shows the deterministic context (e.g. the coverage matrix) alongside it so the human isn't deciding blind.

See `decider_gate/examples/generic_tool_guard.py` for a ~15-line runnable example guarding one generic tool call with a single `noul` question, and `src/ai_test_engineer/checkpoints.py` for four real, non-trivial checkpoints end to end.
