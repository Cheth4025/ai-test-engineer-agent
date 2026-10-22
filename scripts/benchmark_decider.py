"""Offline-labeled benchmark comparing the Decider server against Claude on
Bedrock, on the exact question wording/state this repo's checkpoints send.

Purpose: measure whether the Decider's frequent escalations on
ambiguity_*/grounding_* questions (see benchmarks/decider_benchmark.json and
the calibration branch's plan) reflect genuine uncertainty or a wording/
state/threshold problem -- never used to justify lowering a threshold
without measured accuracy behind it.

Design: every function except main() is pure or takes its backend client as
an argument, so the whole pipeline is testable with stubs (see
tests/unit/test_benchmark_decider.py) with no real network call. main() is
the only part that talks to a real Decider server or real Bedrock, and is
never invoked by the test suite.

Usage (live; NOT run by this commit -- the user runs this manually):
    .venv/Scripts/python.exe scripts/benchmark_decider.py
    .venv/Scripts/python.exe scripts/benchmark_decider.py --only grounding_Sxx --limit 5
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from decider_gate.client import DeciderClient
from decider_gate.gate import ConfidenceGate

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BENCHMARK_FILE = REPO_ROOT / "benchmarks" / "decider_benchmark.json"
DEFAULT_OUT_DIR = REPO_ROOT / "benchmarks"
DEFAULT_DECIDER_CONFIG_FILE = REPO_ROOT / "config" / "decider.json"

# Mirrors config/decider.json -- used as a fallback when that file can't be
# read, and as the fixed baseline tests pass to compute_summary().
DEFAULT_DECIDER_CFG: dict[str, Any] = {
    "url": "http://127.0.0.1:8000",
    "model": "StrandsAgents/strands-decider-2B-hobson-v19",
    "host_label": "local-cpu",
    "choice_threshold": 0.75,
    "noul_upper": 0.80,
    "noul_lower": 0.40,
    "timeouts": {"connect_s": 5, "call_s_cpu": 30, "call_s_gpu": 10},
}

# Approximate Bedrock pricing for us.anthropic.claude-sonnet-4-6, taken from
# the Anthropic direct-API Sonnet-tier rate (checked 2026-10). Bedrock is
# partner-operated with pricing that can differ from or lag the direct API --
# verify against https://aws.amazon.com/bedrock/pricing/ before trusting this
# for real budgeting; it is only a rough per-decision estimate here.
BEDROCK_INPUT_COST_PER_1M = 3.00
BEDROCK_OUTPUT_COST_PER_1M = 15.00

DEFAULT_BEDROCK_MODEL_ID = "us.anthropic.claude-sonnet-4-6"

# Swept offline against each item's recorded confidence -- for `choice`
# questions this is ConfidenceGate's own routed value (answer["confidence"],
# NOT the top probability -- see the module docstring and
# tests/unit/test_benchmark_decider.py's confidence-vs-probability tests).
CHOICE_THRESHOLD_SWEEP = [0.60, 0.65, 0.70, 0.75, 0.80, 0.85]
# noul_lower is held fixed at the current 0.40; only the upper bound sweeps.
NOUL_UPPER_SWEEP = [0.65, 0.70, 0.75, 0.80, 0.85]

# Above this gap between a choice question's self-reported confidence and
# its own top probability, compute_summary() surfaces a note. Observational
# only -- decider_gate.gate.ConfidenceGate is never touched by this script.
_CONFIDENCE_VS_TOP_PROBABILITY_NOTE_GAP = 0.15
_REPLAY_FIDELITY_FLAG_GAP = 0.05


@dataclass
class Outcome:
    """One question's scored outcome, from either backend.

    `route`/`probabilities` are None for a Bedrock outcome (Bedrock has no
    AUTO/ESCALATE routing -- it just answers); `answer` is None for a
    Decider outcome that routed ESCALATE (per decider_gate.gate.Verdict).
    `correct` compares `suggestion` (Decider's top pick, always populated,
    regardless of route) or Bedrock's answer against the item's label.
    """

    question_name: str
    answer: Any
    confidence: float | None
    probabilities: dict | None
    route: str | None
    suggestion: Any
    correct: bool | None


@dataclass
class DeciderResult:
    item_id: str
    question_type: str
    latency_ms: int | None
    error: str | None
    outcomes: list[Outcome] = field(default_factory=list)


@dataclass
class BedrockResult:
    item_id: str
    question_type: str
    latency_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    error: str | None
    outcomes: list[Outcome] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Benchmark file loading / validation
# ---------------------------------------------------------------------------


def load_benchmark_items(path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def validate_item(item: dict) -> None:
    """Raises ValueError on a malformed item; returns None when it's fine."""
    required = {"item_id", "item_type", "question_type", "state", "label", "label_rationale"}
    missing = required - item.keys()
    if missing:
        raise ValueError(f"item {item.get('item_id')!r} missing required keys: {sorted(missing)}")

    if item["item_type"] == "replay":
        questions = item.get("questions")
        if not isinstance(questions, dict) or not questions:
            raise ValueError(f"replay item {item['item_id']!r} must have a non-empty 'questions' dict")
        labels = item["label"]
        if not isinstance(labels, dict):
            raise ValueError(f"replay item {item['item_id']!r} label must be a dict keyed by question name")
        missing_labels = set(questions) - set(labels)
        if missing_labels:
            raise ValueError(f"replay item {item['item_id']!r} is missing labels for: {sorted(missing_labels)}")
        for name, question in questions.items():
            _validate_label_against_criteria(item["item_id"], name, question, labels[name])
    else:
        question = item.get("question")
        name = item.get("question_name")
        if not isinstance(question, dict) or not name:
            raise ValueError(f"single item {item['item_id']!r} must have 'question_name' and 'question'")
        _validate_label_against_criteria(item["item_id"], name, question, item["label"])


def _validate_label_against_criteria(item_id: str, question_name: str, question: dict, label: Any) -> None:
    criteria = question.get("criteria") or {}
    qtype = question.get("type")
    if qtype == "noul":
        if not isinstance(label, bool):
            raise ValueError(f"{item_id!r}/{question_name!r}: noul label must be a bool, got {label!r}")
    elif qtype == "choice":
        if label not in criteria:
            raise ValueError(f"{item_id!r}/{question_name!r}: label {label!r} is not one of {list(criteria)}")
    else:
        raise ValueError(f"{item_id!r}/{question_name!r}: unsupported question type {qtype!r}")


def filter_items(items: list[dict], only: str | None, limit: int | None) -> list[dict]:
    filtered = items
    if only:
        filtered = [item for item in filtered if item["question_type"] == only]
    if limit is not None:
        filtered = filtered[:limit]
    return filtered


def _questions_for(item: dict) -> dict:
    if item["item_type"] == "replay":
        return item["questions"]
    return {item["question_name"]: item["question"]}


def _labels_for(item: dict) -> dict:
    if item["item_type"] == "replay":
        return item["label"]
    return {item["question_name"]: item["label"]}


# ---------------------------------------------------------------------------
# Decider backend
# ---------------------------------------------------------------------------


def call_decider(client: DeciderClient, gate: ConfidenceGate, item: dict, host_label: str = "benchmark") -> DeciderResult:
    """One client.ask() call for the item -- every question in a replay
    item's batch goes in that same single call, exactly like
    DeciderGuardrail.before_tool_call sends a whole checkpoint. Never raises:
    any failure is recorded on DeciderResult.error so one bad item can't
    abort the whole benchmark run (the user's explicit robustness ask)."""
    questions = _questions_for(item)
    labels = _labels_for(item)

    try:
        response = client.ask(state=item["state"], questions=questions)
    except Exception as exc:  # noqa: BLE001 -- intentionally broad, see docstring
        return DeciderResult(
            item_id=item["item_id"], question_type=item["question_type"], latency_ms=None, error=str(exc)
        )

    outcomes = []
    for name, answer in response["answers"].items():
        verdict = gate.evaluate(name, answer, response["latency_ms"], host_label, response["model"])
        label = labels.get(name)
        correct = (verdict.suggestion == label) if label is not None else None
        outcomes.append(
            Outcome(name, verdict.answer, verdict.confidence, verdict.probabilities, verdict.route, verdict.suggestion, correct)
        )

    return DeciderResult(
        item_id=item["item_id"],
        question_type=item["question_type"],
        latency_ms=response["latency_ms"],
        error=None,
        outcomes=outcomes,
    )


# ---------------------------------------------------------------------------
# Bedrock backend
# ---------------------------------------------------------------------------


def build_bedrock_prompt(item: dict) -> str:
    """Renders the SAME state blob the Decider call gets (fairness -- both
    backends must see identical inputs), plus every question's instructions
    and criteria, asking for JSON-only output."""
    questions = _questions_for(item)
    lines = [
        "You are evaluating one or more questions against the following shared context.",
        "",
        "Context (JSON):",
        json.dumps(item["state"], indent=2, sort_keys=True, default=str),
        "",
        "Answer every question below. Respond with JSON ONLY -- no prose, no markdown "
        "code fences -- in exactly this shape:",
        '{"<question_name>": {"answer": <one of its valid values>, "confidence": <0.0-1.0>}, ...}',
        "",
    ]
    for name, question in questions.items():
        lines.append(f"Question '{name}' ({question['type']}): {question['instructions']}")
        if question["type"] == "noul":
            # A noul question's criteria dict is keyed by the strings "true"/
            # "false" (see checkpoints.py) -- those are labels, not the wire
            # value. Listing them as "valid answers" previously told Bedrock
            # to echo the literal string "false", which never equals the
            # real bool label False (the bug this fixed: every noul item
            # scored 0% regardless of judgement -- see
            # tests/unit/test_benchmark_decider.py's coercion tests).
            lines.append(f"Answer '{name}' with the JSON boolean true or false -- not a quoted string.")
        else:
            valid_answers = list(question["criteria"].keys())
            lines.append(f"Valid answers for '{name}': {valid_answers}")
        lines.append("")
    return "\n".join(lines)


def _coerce_noul_answer(answer: Any) -> Any:
    """Accepts a real bool, or a "true"/"false" string (any case) a model
    might still echo despite build_bedrock_prompt's explicit instruction,
    and returns a real bool either way. Anything else is returned
    unchanged -- never fabricates an answer the model didn't give."""
    if isinstance(answer, bool):
        return answer
    if isinstance(answer, str) and answer.strip().lower() in ("true", "false"):
        return answer.strip().lower() == "true"
    return answer


def parse_bedrock_json_answer(raw_text: str) -> dict:
    """Never fabricates a substitute answer -- raises ValueError on anything
    unparseable, matching decider_gate.client.DeciderClient's own
    never-fabricate philosophy."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"no JSON object found in Bedrock response: {raw_text!r}")

    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"Bedrock response was not valid JSON: {exc}") from exc


def call_bedrock(client, model_id: str, item: dict) -> BedrockResult:
    """One boto3 bedrock-runtime converse() call for the item. Never raises:
    any failure (transport, or an unparseable response) is recorded on
    BedrockResult.error so the run continues to the next item."""
    prompt = build_bedrock_prompt(item)
    labels = _labels_for(item)
    questions = _questions_for(item)

    start = time.monotonic()
    try:
        response = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 1024, "temperature": 0},
        )
    except Exception as exc:  # noqa: BLE001 -- intentionally broad, see docstring
        return BedrockResult(
            item_id=item["item_id"],
            question_type=item["question_type"],
            latency_ms=None,
            input_tokens=None,
            output_tokens=None,
            error=str(exc),
        )
    latency_ms = int((time.monotonic() - start) * 1000)

    usage = response.get("usage") or {}
    input_tokens, output_tokens = usage.get("inputTokens"), usage.get("outputTokens")

    try:
        raw_text = response["output"]["message"]["content"][0]["text"]
        parsed = parse_bedrock_json_answer(raw_text)
    except (ValueError, KeyError, IndexError) as exc:
        return BedrockResult(
            item_id=item["item_id"],
            question_type=item["question_type"],
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            error=str(exc),
        )

    outcomes = []
    for name, label in labels.items():
        entry = parsed.get(name)
        if entry is None:
            outcomes.append(Outcome(name, None, None, None, None, None, None))
            continue
        answer = entry.get("answer")
        if questions[name]["type"] == "noul":
            answer = _coerce_noul_answer(answer)
        confidence = entry.get("confidence")
        correct = (answer == label) if label is not None else None
        outcomes.append(Outcome(name, answer, confidence, None, None, answer, correct))

    return BedrockResult(
        item_id=item["item_id"],
        question_type=item["question_type"],
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        error=None,
        outcomes=outcomes,
    )


# ---------------------------------------------------------------------------
# Summary computation
# ---------------------------------------------------------------------------


def _accuracy(outcomes: list[Outcome]) -> float | None:
    scored = [o for o in outcomes if o.correct is not None]
    if not scored:
        return None
    return sum(1.0 for o in scored if o.correct) / len(scored)


def _escalation_rate(outcomes: list[Outcome]) -> float | None:
    routed = [o for o in outcomes if o.route is not None]
    if not routed:
        return None
    return sum(1.0 for o in routed if o.route == "ESCALATE") / len(routed)


def _percentile(values: list[int], pct: float) -> float | None:
    if not values:
        return None
    sorted_values = sorted(values)
    idx = max(0, min(len(sorted_values) - 1, int(round(pct / 100 * (len(sorted_values) - 1)))))
    return sorted_values[idx]


def compute_summary(
    decider_results: list[DeciderResult],
    bedrock_results: list[BedrockResult],
    items: list[dict],
    decider_cfg: dict,
) -> dict:
    item_by_id = {item["item_id"]: item for item in items}

    def is_replay(result) -> bool:
        item = item_by_id.get(result.item_id)
        return bool(item and item["item_type"] == "replay")

    def is_held_out(result) -> bool:
        item = item_by_id.get(result.item_id)
        return bool(item and item.get("held_out", False))

    non_replay_decider = [r for r in decider_results if not is_replay(r) and not is_held_out(r)]
    non_replay_bedrock = [r for r in bedrock_results if not is_replay(r) and not is_held_out(r)]
    replay_decider = [r for r in decider_results if is_replay(r)]
    held_out_decider = [r for r in decider_results if not is_replay(r) and is_held_out(r)]
    held_out_bedrock = [r for r in bedrock_results if not is_replay(r) and is_held_out(r)]

    def per_type_table(decider_rows: list[DeciderResult], bedrock_rows: list[BedrockResult]) -> dict:
        types = sorted({r.question_type for r in decider_rows} | {r.question_type for r in bedrock_rows})
        table = {}
        for qtype in types:
            d_outcomes = [o for r in decider_rows if r.question_type == qtype for o in r.outcomes]
            b_outcomes = [o for r in bedrock_rows if r.question_type == qtype for o in r.outcomes]
            table[qtype] = {
                "n": len(d_outcomes) or len(b_outcomes),
                "decider_accuracy": _accuracy(d_outcomes),
                "bedrock_accuracy": _accuracy(b_outcomes),
                "decider_escalation_rate": _escalation_rate(d_outcomes),
            }
        return table

    question_types = sorted({r.question_type for r in non_replay_decider} | {r.question_type for r in non_replay_bedrock})
    per_question_type = per_type_table(non_replay_decider, non_replay_bedrock)
    # Reported separately from per_question_type so wording decisions can be
    # checked for overfitting against the exact (training) items used to
    # justify them -- never folded back into the training-set numbers above.
    held_out_per_question_type = per_type_table(held_out_decider, held_out_bedrock)

    decider_latencies = [r.latency_ms for r in decider_results if r.latency_ms is not None]
    bedrock_latencies = [r.latency_ms for r in bedrock_results if r.latency_ms is not None]
    latency = {
        "decider_p50_ms": _percentile(decider_latencies, 50),
        "decider_p95_ms": _percentile(decider_latencies, 95),
        "bedrock_p50_ms": _percentile(bedrock_latencies, 50),
        "bedrock_p95_ms": _percentile(bedrock_latencies, 95),
    }

    costed = [r for r in bedrock_results if r.error is None and r.input_tokens is not None and r.output_tokens is not None]
    if costed:
        per_item_costs = [
            (r.input_tokens / 1_000_000) * BEDROCK_INPUT_COST_PER_1M
            + (r.output_tokens / 1_000_000) * BEDROCK_OUTPUT_COST_PER_1M
            for r in costed
        ]
        cost = {
            "bedrock_avg_input_tokens": statistics.mean(r.input_tokens for r in costed),
            "bedrock_avg_output_tokens": statistics.mean(r.output_tokens for r in costed),
            "bedrock_cost_per_decision_usd": statistics.mean(per_item_costs),
        }
    else:
        cost = {"bedrock_avg_input_tokens": None, "bedrock_avg_output_tokens": None, "bedrock_cost_per_decision_usd": None}

    type_by_question_type = {
        item["question_type"]: item["question"]["type"] for item in items if item["item_type"] == "single"
    }

    threshold_sweep: dict[str, Any] = {}
    notes: list[str] = []
    for qtype in question_types:
        q_kind = type_by_question_type.get(qtype)
        outcomes = [o for r in non_replay_decider if r.question_type == qtype for o in r.outcomes]
        if q_kind is None or not outcomes:
            continue

        thresholds = CHOICE_THRESHOLD_SWEEP if q_kind == "choice" else NOUL_UPPER_SWEEP
        auto_rate, auto_accuracy = [], []
        for t in thresholds:
            qualifying = [o for o in outcomes if o.confidence is not None and o.confidence >= t]
            auto_rate.append(len(qualifying) / len(outcomes))
            auto_accuracy.append(_accuracy(qualifying))

        entry: dict[str, Any] = {
            "type": q_kind,
            "thresholds": thresholds,
            "auto_rate": auto_rate,
            "auto_accuracy": auto_accuracy,
            "current_threshold": decider_cfg.get("choice_threshold") if q_kind == "choice" else decider_cfg.get("noul_upper"),
        }

        top_probs = [max(o.probabilities.values()) for o in outcomes if o.probabilities]
        if q_kind == "choice":
            entry["top_probability_avg"] = statistics.mean(top_probs) if top_probs else None

        confidences = [o.confidence for o in outcomes if o.confidence is not None]
        if q_kind == "choice" and confidences and top_probs:
            avg_conf, avg_top = statistics.mean(confidences), statistics.mean(top_probs)
            gap = abs(avg_conf - avg_top)
            if gap > _CONFIDENCE_VS_TOP_PROBABILITY_NOTE_GAP:
                notes.append(
                    f"{qtype}: self-reported confidence (avg {avg_conf:.2f}) diverges from its top "
                    f"probability (avg {avg_top:.2f}) by {gap:.2f} -- not corrected; "
                    "decider_gate.gate.ConfidenceGate is unchanged."
                )

        threshold_sweep[qtype] = entry

    replay_fidelity = []
    for result in replay_decider:
        item = item_by_id.get(result.item_id)
        if item is None:
            continue
        live_confidence = (item.get("live_reference") or {}).get("live_confidence", {})
        for outcome in result.outcomes:
            live = live_confidence.get(outcome.question_name)
            if live is None or outcome.confidence is None:
                continue
            diff = abs(outcome.confidence - live)
            replay_fidelity.append(
                {
                    "item_id": result.item_id,
                    "question_name": outcome.question_name,
                    "benchmark_confidence": outcome.confidence,
                    "live_confidence": live,
                    "diff": diff,
                    "flagged": diff > _REPLAY_FIDELITY_FLAG_GAP,
                }
            )

    return {
        "per_question_type": per_question_type,
        "held_out_per_question_type": held_out_per_question_type,
        "threshold_sweep": threshold_sweep,
        "latency": latency,
        "cost": cost,
        "replay_fidelity": replay_fidelity,
        "notes": notes,
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def render_markdown_summary(summary: dict) -> str:
    lines = ["# Decider calibration benchmark summary", "", "## Per-question-type accuracy", ""]
    lines.append("| question_type | n | decider_accuracy | bedrock_accuracy | decider_escalation_rate |")
    lines.append("|---|---|---|---|---|")
    for qtype, row in summary["per_question_type"].items():
        lines.append(
            f"| {qtype} | {row['n']} | {_fmt(row['decider_accuracy'])} | "
            f"{_fmt(row['bedrock_accuracy'])} | {_fmt(row['decider_escalation_rate'])} |"
        )

    if summary["held_out_per_question_type"]:
        lines += ["", "## Held-out accuracy", "", (
            "Held-out items were labelled before any wording change this round and are never "
            "used to adjust wording -- this checks whether a fix generalizes past the exact "
            "items used to justify it."
        ), ""]
        lines.append("| question_type | n | decider_accuracy | bedrock_accuracy | decider_escalation_rate |")
        lines.append("|---|---|---|---|---|")
        for qtype, row in summary["held_out_per_question_type"].items():
            lines.append(
                f"| {qtype} | {row['n']} | {_fmt(row['decider_accuracy'])} | "
                f"{_fmt(row['bedrock_accuracy'])} | {_fmt(row['decider_escalation_rate'])} |"
            )

    lines += ["", "## Threshold sweep", ""]
    for qtype, sweep in summary["threshold_sweep"].items():
        lines.append(f"### {qtype} ({sweep['type']}, current threshold: {_fmt(sweep['current_threshold'])})")
        if "top_probability_avg" in sweep:
            lines.append(f"top probability avg (informational, not swept): {_fmt(sweep['top_probability_avg'])}")
        lines.append("| threshold | auto_rate | auto_accuracy |")
        lines.append("|---|---|---|")
        for t, rate, acc in zip(sweep["thresholds"], sweep["auto_rate"], sweep["auto_accuracy"]):
            lines.append(f"| {t} | {_fmt(rate)} | {_fmt(acc)} |")
        lines.append("")

    lines += ["## Latency (ms)"]
    lines += [f"- {k}: {v}" for k, v in summary["latency"].items()]

    lines += ["", "## Bedrock cost"]
    lines += [f"- {k}: {v}" for k, v in summary["cost"].items()]

    if summary["replay_fidelity"]:
        lines += ["", "## Replay fidelity (benchmark vs live)", ""]
        lines.append("| item_id | question | benchmark_confidence | live_confidence | diff | flagged |")
        lines.append("|---|---|---|---|---|---|")
        for row in summary["replay_fidelity"]:
            lines.append(
                f"| {row['item_id']} | {row['question_name']} | {row['benchmark_confidence']:.4f} | "
                f"{row['live_confidence']:.4f} | {row['diff']:.4f} | {row['flagged']} |"
            )

    if summary["notes"]:
        lines += ["", "## Notes", ""]
        lines += [f"- {note}" for note in summary["notes"]]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# main() -- the only part that makes real network calls. Never invoked by
# the test suite.
# ---------------------------------------------------------------------------


def _load_decider_cfg(path: Path = DEFAULT_DECIDER_CONFIG_FILE) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return DEFAULT_DECIDER_CFG


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-file", type=Path, default=DEFAULT_BENCHMARK_FILE)
    parser.add_argument("--decider-url", default=None, help="Overrides config/decider.json's url")
    parser.add_argument("--model-id", default=DEFAULT_BEDROCK_MODEL_ID)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--timeout-s", type=float, default=None, help="Per-call timeout; defaults to decider.json's call_s_cpu")
    parser.add_argument("--only", default=None, help="Run only items of this question_type")
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of items run")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    import boto3

    args = _parse_args(argv)
    decider_cfg = _load_decider_cfg()
    if args.decider_url:
        decider_cfg["url"] = args.decider_url
    timeout_s = args.timeout_s or decider_cfg["timeouts"]["call_s_cpu"]

    items = filter_items(load_benchmark_items(args.benchmark_file), args.only, args.limit)
    for item in items:
        validate_item(item)

    decider_client = DeciderClient(
        url=decider_cfg["url"], timeout_s=timeout_s, connect_timeout_s=decider_cfg["timeouts"]["connect_s"]
    )
    gate = ConfidenceGate(
        choice_threshold=decider_cfg["choice_threshold"],
        noul_upper=decider_cfg["noul_upper"],
        noul_lower=decider_cfg["noul_lower"],
    )
    bedrock_client = boto3.client("bedrock-runtime")

    decider_results = [call_decider(decider_client, gate, item) for item in items]
    bedrock_results = [call_bedrock(bedrock_client, args.model_id, item) for item in items]

    summary = compute_summary(decider_results, bedrock_results, items, decider_cfg)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    json_path = args.out_dir / f"results_{timestamp}.json"
    md_path = args.out_dir / f"results_{timestamp}.md"

    json_path.write_text(
        json.dumps(
            {
                "decider_results": [vars(r) | {"outcomes": [vars(o) for o in r.outcomes]} for r in decider_results],
                "bedrock_results": [vars(r) | {"outcomes": [vars(o) for o in r.outcomes]} for r in bedrock_results],
                "summary": summary,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    md_path.write_text(render_markdown_summary(summary), encoding="utf-8")
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")


if __name__ == "__main__":
    main()
