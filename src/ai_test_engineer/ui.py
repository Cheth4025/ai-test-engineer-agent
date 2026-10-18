"""Streamlit UI for the AI Test Engineer Agent (Milestone 5).

Run with: .venv/Scripts/python.exe -m streamlit run src/ai_test_engineer/ui.py

One page: a fixed dark top bar, a dark left sidebar with run status and
navigation, and a main area that renders exactly one of the same four
sections docs/06_Streamlit_UI_Specification.md Section 2 defines -- layout
only, an owner-approved deviation from 06 Section 1's "four native tabs" /
mockups README deviation #1 (see docs/mockups/README.md row 1). Navigation
is a plain st.radio in the sidebar (its value lives in st.session_state via
its key, same as any other widget) -- selecting a section never starts
work, same guarantee tabs gave. Reads the existing RunController + run
files only -- no new orchestration logic, no new agents.

Process model: Streamlit reruns this whole script on every interaction, but
the Python process (and orchestration._ACTIVE_RUNS, a module-level dict)
persists across reruns within one `streamlit run` process -- the same thing
scripts/run_initial_pass.py's loop relies on across repeated calls.
`get_ui_state()` (st.cache_resource) returns one process-wide UIState: a
lock plus which orchestration call, if any, is in flight. Start Analysis and
Confirm decision each spawn a background worker thread that calls
orchestration.run_initial_pass()/resume_pending_decision() directly and
writes the result onto UIState under the lock -- the worker never imports or
calls any st.* API. Browser reload re-renders from UIState/disk and starts
nothing (see gather_display_data's and the guard functions' tests in
tests/ui/test_ui_guards.py).

Scoped decision (not in docs/06 verbatim): doc 06 Section 3's "New Analysis"
guard ("Prior run terminal and no worker/child active") is relaxed for two
states that have no path forward in this milestone -- AWAITING_APPROVAL
(Approve is a placeholder) and AWAITING_CLARIFICATION (Submit clarification
is a placeholder) -- in addition to the four real terminal states. It is
NOT relaxed for AWAITING_DECISION_CONFIRMATION, which has a working Confirm
decision path and must not be silently abandoned. Starting a new run while
the old one sits in one of the two relaxed states marks that old run
STOPPED first (see start_analysis), so it never lingers non-terminal
forever.
"""

from __future__ import annotations

import html
import json
import statistics
import threading
import time
from pathlib import Path
from typing import Any, Callable

import altair as alt
import pandas as pd
import streamlit as st

from ai_test_engineer import checkpoints, orchestration
from ai_test_engineer.state import RunController

CONFIG_DIR = orchestration.CONFIG_DIR

# (nav id, label, Material icon name). IDs are stable identifiers used for
# st.session_state/keys; labels are what's shown. docs/mockups/README.md
# row 1 (owner-approved layout change): icon + label nav, not tabs/radio.
NAV_ITEMS = [
    ("requirements", "Overview", "description"),
    ("scenarios", "Scenarios", "list"),
    ("execution", "Execution", "play_arrow"),
    ("results", "Results", "bar_chart"),
]
NAV_LABELS = {item_id: label for item_id, label, _icon in NAV_ITEMS}

# Which page owns the Confirm decision control for a given pending
# checkpoint tool -- the sidebar points the user there when a decision is
# awaiting confirmation but they're looking at a different page.
PENDING_SECTION_FOR_TOOL = {
    "generate_scenarios": "requirements",
    "save_scenario_set": "scenarios",
}

# Doc 06's two escalation checkpoints and their fixed option sets -- same
# mapping scripts/run_initial_pass.py._options_for hardcodes. Presentation
# only (which buttons to draw), not business logic, so duplicating this tiny
# mapping here is cheaper than threading it through orchestration.py.
OPTIONS_FOR_TOOL: dict[str, list[str]] = {
    "save_scenario_set": ["ACCEPT_COVERAGE", "REMEDIATE_GAP"],
    "generate_scenarios": ["GENERATE_TESTS", "REQUEST_CLARIFICATION"],
}

TERMINAL_WORKFLOW_STATES = {"COMPLETED", "REJECTED", "BLOCKED", "STOPPED"}

# States with no path forward in this milestone (Approve/Submit clarification
# are placeholders) -- New Analysis may abandon a run sitting in one of
# these, in addition to the four real terminal states. AWAITING_DECISION_
# CONFIRMATION is deliberately excluded: Confirm decision actually works
# there, so New Analysis must not be allowed to abandon it.
RESTARTABLE_WORKFLOW_STATES = TERMINAL_WORKFLOW_STATES | {"AWAITING_APPROVAL", "AWAITING_CLARIFICATION"}


def options_for(tool_name: str) -> list[str]:
    try:
        return OPTIONS_FOR_TOOL[tool_name]
    except KeyError:
        raise ValueError(f"unrecognized pending tool {tool_name!r}") from None


class UIState:
    """Process-wide singleton (via get_ui_state()/st.cache_resource): the
    UI's own concurrency guard over the two existing orchestration entry
    points. Not persisted -- a process restart loses it exactly as it loses
    orchestration._ACTIVE_RUNS (see state.py's module docstring)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.worker_busy = False
        self.current_run_id: str | None = None
        self.last_result: dict | None = None
        self.last_error: str | None = None


@st.cache_resource
def get_ui_state() -> UIState:
    return UIState()


# ---------------------------------------------------------------------------
# Guards -- pure functions, no Streamlit dependency, directly unit-testable.
# ---------------------------------------------------------------------------


def can_start_analysis(state: UIState, controller: RunController | None) -> bool:
    if state.worker_busy:
        return False
    if controller is None:
        return True
    return controller.workflow_state in RESTARTABLE_WORKFLOW_STATES


def can_confirm_decision(state: UIState, controller: RunController | None) -> bool:
    return (
        controller is not None
        and not state.worker_busy
        and controller.stage == "AWAITING_DECISION_CONFIRMATION"
        and controller.pending_decision is not None
    )


def can_approve(
    state: UIState, controller: RunController | None, reviewer: str, scenario_set_version: int | None
) -> bool:
    """Mirrors RunController.record_approval's own guards (the real
    enforcement) so the UI can disable a doomed click -- never the only
    guard."""
    if controller is None or state.worker_busy:
        return False
    if controller.workflow_state != "AWAITING_APPROVAL":
        return False
    if (controller.run_dir / "approval.json").exists():
        return False
    if not reviewer or not reviewer.strip():
        return False
    if scenario_set_version is None:
        return False
    return controller.approval_blockers(scenario_set_version) == []


def can_reject(state: UIState, controller: RunController | None, reviewer: str) -> bool:
    """Unlike can_approve, never gated on approval_blockers() -- Reject is
    allowed at AWAITING_APPROVAL even with open blockers (RunController.
    record_approval enforces the same)."""
    if controller is None or state.worker_busy:
        return False
    if controller.workflow_state != "AWAITING_APPROVAL":
        return False
    if (controller.run_dir / "approval.json").exists():
        return False
    return bool(reviewer and reviewer.strip())


# ---------------------------------------------------------------------------
# Worker thread plumbing. _run_worker and everything it calls must never
# import or call any st.* API.
# ---------------------------------------------------------------------------


def _stop_on_exception(controller: RunController | None, exc: Exception) -> dict:
    """Mirrors scripts/run_initial_pass.py's _stop_on_exception: never let an
    exception surface as a raw traceback -- mark the run STOPPED with a
    readable reason instead (docs/06 Section 4)."""
    if controller is not None:
        controller.set_workflow_state("STOPPED", stage="STOPPED", busy=False)
        controller.save_evidence(
            "exception_stop.json", {"error_type": type(exc).__name__, "message": str(exc)}
        )
    return {
        "run_id": controller.run_id if controller is not None else None,
        "workflow_state": "STOPPED",
        "stage": "STOPPED",
        "error": f"{type(exc).__name__}: {exc}",
    }


def _controller_for_error(state: UIState, known_before: frozenset[str]) -> RunController | None:
    """The controller to blame for a worker exception. Prefers a run newly
    registered in orchestration._ACTIVE_RUNS since this worker started --
    that catches a Start Analysis failure against the NEW run it was
    actually trying to create, which state.current_run_id may not have
    adopted yet (or may still hold the PREVIOUS run's id). Falls back to
    state.current_run_id for a Confirm decision failure, which resumes an
    already-known run and never creates a new run_id."""
    new_ids = set(orchestration._ACTIVE_RUNS.keys()) - known_before
    if new_ids:
        return orchestration._ACTIVE_RUNS[next(iter(new_ids))]["controller"]
    if state.current_run_id is not None:
        run = orchestration._ACTIVE_RUNS.get(state.current_run_id)
        return run["controller"] if run else None
    return None


def _run_worker(state: UIState, fn: Callable[[], dict], known_before: frozenset[str] = frozenset()) -> None:
    try:
        result = fn()
        error = None
    except Exception as exc:  # mirrors scripts/run_initial_pass.py's top-level guard
        try:
            controller = _controller_for_error(state, known_before)
            result = _stop_on_exception(controller, exc)
        except Exception as secondary:  # never leave worker_busy stuck True forever
            result = {"run_id": None, "workflow_state": "STOPPED", "stage": "STOPPED", "error": str(secondary)}
        error = str(exc)
    with state.lock:
        state.last_result = result
        state.last_error = error
        if result.get("run_id"):
            state.current_run_id = result["run_id"]
        state.worker_busy = False


def _adopt_new_run_id(state: UIState, known_before: frozenset[str], timeout: float = 1.0) -> None:
    """Picks up the new run's id as soon as orchestration.run_initial_pass
    registers it in _ACTIVE_RUNS -- which happens immediately, before any
    blocking model call -- so the header reflects the NEW run while it is
    still in progress rather than a stale previous run. A short bounded
    wait on the calling (render) thread; typically resolves in well under
    a millisecond since it's a dict assignment, not network I/O."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        new_ids = set(orchestration._ACTIVE_RUNS.keys()) - known_before
        if new_ids:
            with state.lock:
                state.current_run_id = next(iter(new_ids))
            return
        time.sleep(0.005)


def start_analysis(state: UIState) -> bool:
    """Returns True if a worker was started, False if the guard denied it --
    a duplicate click while a worker is already active does nothing. If the
    current run sits in a dead-end state (AWAITING_APPROVAL/
    AWAITING_CLARIFICATION), marks it STOPPED before starting the new one so
    it never lingers non-terminal forever."""
    controller = current_controller(state)
    with state.lock:
        if not can_start_analysis(state, controller):
            return False
        state.worker_busy = True
        state.last_error = None
    if controller is not None and controller.workflow_state not in TERMINAL_WORKFLOW_STATES:
        controller.save_evidence("abandoned.json", {"reason": "superseded by New Analysis"})
        controller.set_workflow_state("STOPPED", stage="STOPPED", busy=False)

    known_before = frozenset(orchestration._ACTIVE_RUNS.keys())
    thread = threading.Thread(
        target=_run_worker, args=(state, orchestration.run_initial_pass, known_before), daemon=True
    )
    thread.start()
    _adopt_new_run_id(state, known_before)
    return True


def confirm_decision(state: UIState, choice: str) -> bool:
    with state.lock:
        controller = current_controller(state)
        if not can_confirm_decision(state, controller):
            return False
        state.worker_busy = True
        state.last_error = None
        run_id = state.current_run_id
    known_before = frozenset(orchestration._ACTIVE_RUNS.keys())
    thread = threading.Thread(
        target=_run_worker,
        args=(state, lambda: orchestration.resume_pending_decision(run_id, choice), known_before),
        daemon=True,
    )
    thread.start()
    return True


# ---------------------------------------------------------------------------
# Read-only data access. Every function here is safe to call on every
# rerun/reload -- none of them ever calls orchestration.run_initial_pass or
# orchestration.resume_pending_decision.
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> Any:
    """Tolerant of both a missing file and one the worker is partway through
    writing (plain write_text, no temp-file replace -- see state.py's
    docstring): either way, treat it as unavailable this refresh, never
    raise and never show stale/corrupt data."""
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def current_controller(state: UIState) -> RunController | None:
    if state.current_run_id is None:
        return None
    run = orchestration._ACTIVE_RUNS.get(state.current_run_id)
    if run is None:
        return None
    return run["controller"]


def decisions(run_dir: Path) -> list[dict]:
    return _load_json(run_dir / "decisions.json") or []


def metrics_strip(records: list[dict]) -> dict:
    """"Decider calls"/median latency/escalated count real model calls
    only -- decision_source == "DECIDER" rows. A HUMAN_ESCALATION row is a
    human's answer to an already-counted DECIDER row, and a REUSED row
    explicitly made no new Decider call (decision_record_reused's
    docstring); counting either would double- (or triple-) count one real
    call."""
    decider_rows = [r for r in records if r.get("decision_source") == "DECIDER"]
    latencies = [r["latency_ms"] for r in decider_rows if r.get("latency_ms") is not None]
    return {
        "decider_calls": len(decider_rows),
        "median_latency_ms": statistics.median(latencies) if latencies else None,
        "escalated": sum(1 for r in decider_rows if r.get("route") == "ESCALATED"),
    }


def available_versions(run_dir: Path, stem: str) -> list[int]:
    """Sorted scenario_set_versions present on disk as {stem}_v{N}.json."""
    versions = []
    for path in run_dir.glob(f"{stem}_v*.json"):
        suffix = path.stem.removeprefix(f"{stem}_v")
        if suffix.isdigit():
            versions.append(int(suffix))
    return sorted(versions)


# Short BR labels: no field for these exists in any runtime-loaded file
# (requirement.json has only one long text blob; interpretation.json's
# "meaning" is a run-specific paraphrase, not a stable short label) --
# resolved with the user: a static lookup copied verbatim from
# docs/AI_Test_Engineer_Agent_Requirements.md's own "BR0N -- Label"
# headings, never invented wording. The Description column below still
# comes from the real per-BR sentence in requirement.json's text.
BR_SHORT_LABELS = {
    "BR01": "Idempotency key",
    "BR02": "Identical retry",
    "BR03": "Conflicting amount",
    "BR04": "Uncertain timeout",
    "BR05": "Verify before retry",
    "BR06": "Callback idempotency",
}


def parse_business_rules(text: str | None) -> list[dict]:
    """Real per-BR sentences parsed from requirement.json's text, one
    "BR0N: ..." line each -- never hardcoded. Only BR01-BR06 lines are
    recognized (BR_SHORT_LABELS is the membership check), so the opening
    user-story line and anything else is skipped."""
    rules = []
    for line in (text or "").splitlines():
        line = line.strip()
        rule_id, sep, description = line.partition(":")
        rule_id = rule_id.strip()
        if not sep or rule_id not in BR_SHORT_LABELS:
            continue
        rules.append({"rule_id": rule_id, "label": BR_SHORT_LABELS[rule_id], "description": description.strip()})
    return rules


def added_by_remediation(previous_scenarios: list[dict] | None, current_scenarios: list[dict]) -> set[str]:
    """scenario_ids present in this version but absent from the previous
    one -- the "Added by remediation" badge, derived purely by diffing real
    scenario_id sets between consecutive scenarios_vN.json files. No new
    field needed. Empty (not an error) when there is no previous version."""
    if previous_scenarios is None:
        return set()
    previous_ids = {s["scenario_id"] for s in previous_scenarios}
    current_ids = {s["scenario_id"] for s in current_scenarios}
    return current_ids - previous_ids


def _assertion_key(assertion: dict) -> tuple:
    return (assertion["observation"], assertion["operator"], assertion["expected"])


def split_shared_assertions(
    per_scenario_assertions: dict[str, list[dict]],
) -> tuple[list[dict], dict[str, list[dict]]]:
    """Splits each scenario's real required_assertions (from
    config/scenario_catalog.json) into the tuple-exact subset shared by
    every scenario and each scenario's own remainder -- no short-phrase
    field exists anywhere for this, so the UI shows the real tuples
    (resolved with the user) rather than inventing prose. The shared list
    puts ledger_success_count first: that's the assertion T04 is about."""
    if not per_scenario_assertions:
        return [], {}

    key_sets = [{_assertion_key(a) for a in assertions} for assertions in per_scenario_assertions.values()]
    shared_keys = set.intersection(*key_sets)

    shared_by_key: dict[tuple, dict] = {}
    for assertions in per_scenario_assertions.values():
        for assertion in assertions:
            key = _assertion_key(assertion)
            if key in shared_keys and key not in shared_by_key:
                shared_by_key[key] = assertion
    shared = list(shared_by_key.values())
    for index, assertion in enumerate(shared):
        if assertion["observation"] == "ledger_success_count":
            shared.insert(0, shared.pop(index))
            break

    remainder = {
        scenario_id: [a for a in assertions if _assertion_key(a) not in shared_keys]
        for scenario_id, assertions in per_scenario_assertions.items()
    }
    return shared, remainder


def format_assertion(assertion: dict) -> str:
    """A real required_assertions tuple rendered compactly -- real data
    only, no invented short phrase (resolved with the user)."""
    operator = "precedes" if assertion["operator"] == "PRECEDES" else "=="
    return f"{assertion['observation']} {operator} {assertion['expected']}"


def coverage_action_confidence(record: dict) -> float | None:
    """The coverage_action question's OWN confidence (per_question), not
    the top-level record["confidence"] -- that top-level number is the
    lowest-scoring verdict across the whole escalated batch (grounding
    flags included), compared against noul_upper (0.8, the gate's own
    route-level escalate/auto threshold) -- a different number for a
    different purpose than the per-choice accept/escalate gate
    (choice_threshold, 0.75) coverage_action is actually compared against.
    Live-run evidence (decisions.json) confirms the two can disagree: one
    COVERAGE record had a 0.65 batch-low confidence vs the 0.8 route gate,
    while its own coverage_action question scored 0.87 vs the 0.75 choice
    gate and resolved AUTO."""
    for question in record.get("per_question") or []:
        if question.get("question") == "coverage_action":
            return question.get("confidence")
    return None


def round4(value: float | None) -> float | None:
    """Confidence/probability values round-trip through JSON as raw floats
    (e.g. 0.41879999999999995) -- rounds for display only, never for
    comparison/storage. Non-numeric/None values pass through unchanged."""
    return round(value, 4) if isinstance(value, (int, float)) else value


def round4_probabilities(probabilities: dict | None) -> dict:
    return {key: round4(value) for key, value in (probabilities or {}).items()}


def decider_decision_value(record: dict) -> str:
    """The real bounded-action choice (e.g. ACCEPT_COVERAGE/REMEDIATE_GAP)
    for one decisions.json COVERAGE record, as the Decision line should
    read it -- never the gate's own internal "Confirm" action name, and
    never a normal-looking choice for a pick a guard actually rejected
    (live-run evidence: a human's "ACCEPT_COVERAGE" pick while coverage was
    4/5 was REJECTED by the 5/5 guard, but the old version of this function
    would have shown it as a plain, accepted "ACCEPT_COVERAGE" decision).

    - decision_status == "REJECTED": "Rejected by guard: <reason>".
    - decision_source == "HUMAN_ESCALATION": the human's literal choice
      (selected_action already IS that string) with " (human)" appended.
    - DECIDER/REUSED whose coverage_action question is still unanswered
      (per_question's "answer" is None -- the escalation-moment record,
      where selected_action is just the gate's "Confirm" action name, not
      a real choice): "Awaiting human", not "Confirm".
    - DECIDER/REUSED with an answered coverage_action (resolved AUTO, no
      escalation needed for that question even though the batch escalated
      overall): that real answer.
    """
    if record.get("decision_status") == "REJECTED":
        return f"Rejected by guard: {record.get('rejection_reason') or 'deterministic guard'}"
    if record.get("decision_source") == "HUMAN_ESCALATION":
        return f"{record.get('selected_action') or '—'} (human)"
    for question in record.get("per_question") or []:
        if question.get("question") == "coverage_action" and question.get("answer") is not None:
            return question["answer"]
    return "Awaiting human"


def _host_label() -> str:
    decider_cfg = _load_json(CONFIG_DIR / "decider.json") or {}
    try:
        return checkpoints.resolve_host_label(decider_cfg)
    except KeyError:
        return "unknown"


def _choice_threshold() -> float | None:
    """config/decider.json's choice_threshold (0.75) -- the per-question
    accept/escalate gate a bounded choice like coverage_action is actually
    compared against. Distinct from a record's own top-level "threshold"
    field (noul_upper, 0.8), which gates the overall route, not any single
    answer -- see coverage_action_confidence's docstring."""
    decider_cfg = _load_json(CONFIG_DIR / "decider.json") or {}
    return decider_cfg.get("choice_threshold")


def gather_display_data(state: UIState) -> dict:
    """Everything the tabs need for the current run, read-only. Never calls
    orchestration.run_initial_pass/resume_pending_decision -- a browser
    reload must never start work (tests/ui/test_ui_guards.py proves this by
    monkeypatching both to raise and calling this function anyway)."""
    data: dict[str, Any] = {
        "requirement": _load_json(CONFIG_DIR / "requirement.json"),
        "host_label": _host_label(),
    }
    controller = current_controller(state)
    data["controller"] = controller
    if controller is None:
        return data

    run_dir = controller.run_dir
    data["run_dir"] = run_dir
    data["interpretation"] = _load_json(run_dir / "interpretation.json")
    data["decisions"] = decisions(run_dir)
    data["metrics"] = metrics_strip(data["decisions"])

    versions = []
    for version in available_versions(run_dir, "scenarios"):
        recorded_coverage = _load_json(run_dir / f"coverage_v{version}.json")
        effective_coverage = recorded_coverage
        unresolved: list[str] = []
        if recorded_coverage is not None:
            # Both RunController methods read raw JSON directly (not through
            # _load_json); a worker mid-write on grounding_v{N}.json raises
            # JSONDecodeError here exactly like a missing file would --
            # treat it the same way: unavailable this refresh, never raise.
            try:
                effective_coverage = controller.effective_coverage(version)
            except (FileNotFoundError, json.JSONDecodeError):
                effective_coverage = recorded_coverage
            try:
                unresolved = controller.unresolved_grounding_flags(version)
            except json.JSONDecodeError:
                unresolved = []
        versions.append(
            {
                "version": version,
                "scenarios": _load_json(run_dir / f"scenarios_v{version}.json"),
                "recorded_coverage": recorded_coverage,
                "effective_coverage": effective_coverage,
                "grounding": _load_json(run_dir / f"grounding_v{version}.json"),
                "unresolved_grounding": unresolved,
            }
        )
    data["versions"] = versions
    data["scenarios_final"] = _load_json(run_dir / "scenarios_final.json")
    data["coverage_final"] = _load_json(run_dir / "coverage_final.json")
    return data


# ---------------------------------------------------------------------------
# Rendering -- Streamlit-dependent; exercised by tests/ui/test_ui_apptest.py.
# ---------------------------------------------------------------------------

# Top-bar text/chips and Approve/Reject's fixed colors have no [theme]/
# [theme.sidebar] config equivalent (theme options style the main area and
# sidebar, never Streamlit's own header chrome, and there's no per-button
# color option, nor a way to put real-time text into the header without
# JavaScript) -- all explicitly requested as scoped CSS plus a real
# Streamlit-rendered, position:fixed markdown block layered on top of the
# header (still zero JS: just HTML/CSS via st.markdown(unsafe_allow_html=
# True), re-rendered fresh every fragment tick with real Python values).
# Vertical .block-container padding stays native: theme.paddingTop/
# theme.sidebar.paddingTop/theme.paddingBottom in .streamlit/config.toml
# (reduced there for the tightened layout) are the officially-recommended
# mechanism and already reserve space for the header's height
# automatically -- stacking CSS padding-top on top of that would double it.
# Side padding and max-width have no native theme equivalent, so those are
# CSS here, per the explicit "CSS only" layout-tightening request.
_BASE_CSS = """
<style>
[data-testid="stHeader"] {
    background-color: #0F1B2D;
}
[data-testid="stHeader"]::before {
    content: "AI Test Engineer Agent";
    position: absolute;
    left: 1rem;
    top: 50%;
    transform: translateY(-50%);
    color: #F5F5F5;
    font-size: 1.15rem;
    font-weight: 600;
    white-space: nowrap;
}
.aite-status-chips {
    position: fixed;
    top: 0.6rem;
    right: 1rem;
    z-index: 999999;
    display: flex;
    gap: 0.5rem;
}
.aite-chip {
    background-color: rgba(255, 255, 255, 0.12);
    color: #F5F5F5;
    font-family: monospace;
    font-size: 0.75rem;
    padding: 0.25rem 0.6rem;
    border-radius: 999px;
    white-space: nowrap;
}
.st-key-approve_btn button {
    background-color: #16A34A !important;
    color: #FFFFFF !important;
    border-color: #16A34A !important;
}
.st-key-reject_btn button {
    background-color: #DC2626 !important;
    color: #FFFFFF !important;
    border-color: #DC2626 !important;
}
/* Disabled must still look clearly disabled (grey) -- declared after the
   rules above and :disabled is more specific, so this wins over the fixed
   green/red whenever the button is actually disabled. */
.st-key-approve_btn button:disabled,
.st-key-reject_btn button:disabled {
    background-color: #E5E7EB !important;
    color: #9CA3AF !important;
    border-color: #D1D5DB !important;
}

/* -- Sidebar: narrow, tight nav (width/min/max pinned to disable the drag
   handle at a size wider than the nav labels need) -- */
[data-testid="stSidebar"] {
    width: 240px !important;
    min-width: 240px !important;
    max-width: 240px !important;
}
[data-testid="stSidebar"] [data-testid="stVerticalBlock"] {
    gap: 6px !important;
}
[class*="st-key-nav_"] button {
    height: 40px !important;
}
[class*="st-key-nav_"] button > div {
    justify-content: flex-start !important;
}

/* -- Main content: fill the page. Vertical padding is native (config.toml);
   side padding/max-width have no theme equivalent. -- */
.block-container {
    padding-left: 1.5rem !important;
    padding-right: 1.5rem !important;
    max-width: 100% !important;
}

/* -- Typography -- */
.block-container h1 {
    font-size: 30px !important;
}
[data-testid="stCaptionContainer"] p {
    font-size: 15px !important;
}
/* :not() excludes .aite-card-heading's own <p> -- without it this rule's
   higher specificity (class + attribute + tag) would win over the
   heading's single-class rule below despite both using !important. */
.block-container [data-testid="stMarkdownContainer"] p:not(.aite-card-heading) {
    font-size: 15px !important;
}
.aite-card-heading {
    font-size: 17px !important;
    font-weight: 600 !important;
    margin: 0 0 0.5rem 0 !important;
}
.block-container [data-testid="stTable"] * {
    font-size: 14px !important;
}

/* -- Cards: tighter inner padding. No official data-testid distinguishes a
   bordered st.container from an unbordered one in this Streamlit build
   (1.66.0) -- verified directly that every st.container(border=True) here
   renders with this exact emotion-cache class; unbordered wrapper blocks
   use a different one and are unaffected. Version-pinned and somewhat
   fragile (an internal, auto-generated class name): re-verify this
   selector by inspecting a card's computed class if Streamlit is ever
   upgraded. -- */
.block-container .st-emotion-cache-1te8eqs {
    padding: 10px !important;
}
/* Vertical gap between stacked blocks in the main content column (title
   row -> card row -> card row, same spirit as the sidebar's 6px gap
   above) -- helps the Overview page fit on one 1080p screen. */
.block-container > [data-testid="stVerticalBlock"] {
    gap: 10px !important;
}
</style>
"""


def _nav_css(current_nav_id: str) -> str:
    """Highlights the currently-selected sidebar nav button: lighter navy
    background + 3px teal left bar. Uses the .st-key-<key> hook Streamlit
    documents for exactly this (same mechanism as Approve/Reject's colors
    above) -- current_nav_id is read from st.session_state before this is
    injected, so it's always correct for the render about to happen."""
    return f"""
    <style>
    .st-key-nav_{current_nav_id} button {{
        background-color: #1D3150 !important;
        border-left: 3px solid #2DD4BF !important;
    }}
    </style>
    """


def _inject_css(current_nav_id: str) -> None:
    st.markdown(_BASE_CSS, unsafe_allow_html=True)
    st.markdown(_nav_css(current_nav_id), unsafe_allow_html=True)


def _render_top_bar_chips(data: dict) -> None:
    """Three monospace pill chips, top-right of the header: run id,
    state · stage, Decider host. Real Streamlit-rendered HTML (not JS),
    position:fixed so it visually layers onto the header bar, re-rendered
    fresh every fragment tick with current values. Every run-derived value
    is html.escape()'d before going into this unsafe_allow_html block."""
    controller: RunController | None = data["controller"]
    if controller is not None:
        run_chip = f"run {controller.run_id}"
        state_chip = f"{controller.workflow_state} · {controller.stage}"
    else:
        run_chip = "run (none)"
        state_chip = "no run"
    host_chip = f"Decider · {data['host_label']}"
    spans = "".join(
        f'<span class="aite-chip">{html.escape(text)}</span>' for text in (run_chip, state_chip, host_chip)
    )
    st.markdown(f'<div class="aite-status-chips">{spans}</div>', unsafe_allow_html=True)


def _render_sidebar(state: UIState, data: dict, current_nav_id: str) -> str:
    """Nav, Refresh status, and the pending-decision notice -- nothing else
    (docs/mockups/README.md row 1, owner-approved layout change -- see
    module docstring). Run status moved to the top-bar chips. Navigation is
    st.button per item (no radio buttons): selecting a page is read-only
    and never calls start_analysis/confirm_decision, same guarantee the
    former tabs/radio gave (tests/ui/test_ui_apptest.py's reload-safety
    test covers this directly)."""
    controller: RunController | None = data["controller"]
    with st.sidebar:
        for item_id, label, icon in NAV_ITEMS:
            if st.button(label, key=f"nav_{item_id}", icon=f":material/{icon}:", width="stretch"):
                st.session_state["nav_id"] = item_id
                current_nav_id = item_id

        st.divider()
        if st.button(
            "Refresh status", type="secondary", icon=":material/refresh:", width="stretch"
        ):
            st.rerun()
        if state.last_error:
            st.error(f"Last run failed: {state.last_error}")

        if controller is not None and controller.pending_decision is not None:
            target_id = PENDING_SECTION_FOR_TOOL.get(controller.pending_decision.get("tool_name"))
            if target_id is not None:
                st.warning(f"A decision needs your confirmation — see '{NAV_LABELS[target_id]}'.")
    return current_nav_id


def _render_page_header(title: str) -> None:
    st.title(title)
    st.caption("Payment Retry & Duplicate Charge Prevention")


def _card_heading(text: str, *, prefix: str = "") -> None:
    """A section heading inside a bordered card, styled via the
    .aite-card-heading CSS class rather than plain "**bold**" markdown --
    several pages also use inline bold field labels (e.g. "**Checkpoint:**")
    at body-text size, so a card heading needs its own distinct class to be
    sized independently without a blanket rule touching those too."""
    prefix_html = f"{html.escape(prefix)} " if prefix else ""
    st.markdown(f'<p class="aite-card-heading">{prefix_html}{html.escape(text)}</p>', unsafe_allow_html=True)


def _render_confirm_decision(state: UIState, controller: RunController) -> None:
    pending = controller.pending_decision
    st.subheader("Confirm decision")
    st.caption(f"checkpoint tool: {pending['tool_name']}")
    for name, fields in pending["verdicts"].items():
        st.write(
            f"**{name}** -- suggestion: `{fields['suggestion']}`, "
            f"confidence: {round4(fields['confidence'])}, route: {fields['route']}, "
            f"probabilities: {round4_probabilities(fields['probabilities'])}"
        )

    options = options_for(pending["tool_name"])

    result = state.last_result or {}
    if result.get("rejected_choice") and controller.decisions:
        last_decision = controller.decisions[-1]
        st.error(
            f"'{result['rejected_choice']}' was rejected: "
            f"{last_decision.get('rejection_reason') or 'deterministic guard'} -- pick again."
        )

    # Root cause of the live BLOCKED run (20261018T191604Z_0ad068bc): this
    # radio used to default-preselect the Decider's suggestion via
    # index=default_index, recomputed and re-passed on every single
    # st.fragment(run_every=2) tick (every 2s) -- and the widget's key was
    # f"confirm_{run_id}", constant for the WHOLE run, not per escalation.
    # Between the human choosing an option and clicking Confirm, an
    # auto-refresh tick silently reverted the visible selection back to
    # that default; the human clicked Confirm believing their real pick
    # was still showing, and the reverted-to-default option was what
    # actually got submitted (the run's own evidence: human selected
    # REMEDIATE_GAP, ACCEPT_COVERAGE was submitted and rejected by the 5/5
    # guard). Two independent fixes, both required:
    #  1. index=None -- no default is ever re-asserted, so a stable key's
    #     session_state value (the human's real, still-pending pick) is
    #     never overridden by a periodic refresh.
    #  2. A key scoped to THIS escalation instance (checkpoints.py's
    #     escalation_seq), not just this run -- a second escalation in the
    #     same run (e.g. RESCORING after a REMEDIATE_GAP) gets a fresh,
    #     unanswered widget instead of silently inheriting whatever the
    #     FIRST escalation's radio last held.
    # Not wrapped in st.form: st.form widgets only commit on submit, so a
    # submit button's disabled= can never react live to an unsubmitted
    # in-form radio value -- it would stay permanently disabled before the
    # first submit. Confirm needing to go from disabled to enabled the
    # moment a choice is made is the more important guarantee here, and a
    # periodic refresh was never able to "submit" by itself regardless (only
    # an explicit Confirm click does, see below) -- index=None plus the
    # escalation-scoped key already stop a refresh from silently changing
    # what's selected, which was the actual defect.
    widget_key = f"confirm_{controller.run_id}_{pending.get('escalation_seq', 0)}"
    choice = st.radio("Your choice", options, index=None, key=widget_key)
    if st.button(
        "Confirm decision",
        type="primary",
        disabled=choice is None or not can_confirm_decision(state, controller),
    ):
        confirm_decision(state, choice)
        st.rerun()


# ---------------------------------------------------------------------------
# Page "requirements" (Overview) -- static UI copy (explicitly requested
# verbatim, not run data) plus the real BR table.
# ---------------------------------------------------------------------------

_USE_CASE_PROBLEM = (
    # Escaped \$ -- a bare "$100 ... $200" is parsed as inline LaTeX math by
    # st.markdown (caught visually: rendered as a code/math block instead
    # of plain text).
    "A customer pays \\$100 once. The gateway re-sends its success callback, "
    "the store records it twice, and the books show \\$200."
)
_USE_CASE_IMPACT = "Inflated revenue, failed reconciliations, and wrong refunds or orders shipped twice."
_USE_CASE_MITIGATION = (
    "The AI Test Engineer Agent builds and runs the tests; the Strands Decider "
    "checks each key decision so no scenario is missed. Test T04 catches the "
    "duplicate (expected 1 ledger entry, found 2) before it reaches production."
)
_ACCEPTANCE_CRITERIA = [
    "Handle payment timeouts and retries",
    "Use idempotency to prevent duplicate charges",
    "Reject conflicting requests with the same key",
    "Process delayed confirmations",
    "Duplicate callbacks never create extra ledger entries",
]
_AGENT_STEPS = [
    "Reads the requirement and extracts the rules (BR01–BR06).",
    "Generates Gherkin test scenarios (initial set).",
    "Checks coverage and finds the missing scenario.",
    "Asks the Strands Decider (2B) to pick a bounded action — REMEDIATE_GAP adds S04.",
    "Waits for human approval, runs T01–T05 once, and reports the result. It never fixes code.",
]


def _render_page_requirements(state: UIState, data: dict) -> None:
    controller: RunController | None = data["controller"]

    header_col, button_col = st.columns([4, 1], vertical_alignment="center")
    with header_col:
        _render_page_header("Overview")
    with button_col:
        label = "New Analysis" if state.current_run_id is not None else "Start analysis"
        if st.button(
            label,
            type="primary",
            key="start_or_new_analysis_btn",
            disabled=not can_start_analysis(state, controller),
        ):
            start_analysis(state)
            st.rerun()

    col1, col2 = st.columns(2)
    with col1, st.container(border=True):
        _card_heading("The use case")
        st.markdown(f"**PROBLEM**  \n{_USE_CASE_PROBLEM}")
        st.markdown(f"**IMPACT**  \n{_USE_CASE_IMPACT}")
        st.markdown(f"**MITIGATION**  \n{_USE_CASE_MITIGATION}")
    with col2, st.container(border=True):
        _card_heading("Business rules (BR01 – BR06)")
        requirement = data["requirement"] or {}
        rules = parse_business_rules(requirement.get("text"))
        st.table([{"ID": r["rule_id"], "Rule": r["label"], "Description": r["description"]} for r in rules])
        st.caption("Loaded from the requirements catalog.")

    col3, col4 = st.columns(2)
    with col3, st.container(border=True):
        _card_heading("Key acceptance criteria")
        for item in _ACCEPTANCE_CRITERIA:
            st.markdown(f"- {item}")
    with col4, st.container(border=True):
        _card_heading("What the agent does")
        for index, step in enumerate(_AGENT_STEPS, start=1):
            st.markdown(f"{index}. {step}")

    if controller is not None and controller.stage == "AWAITING_CLARIFICATION":
        st.warning("Run is paused awaiting clarification on a critical ambiguity.")
        st.text_area("Submit clarification", disabled=True, placeholder="Not implemented in this milestone.")
        st.button("Submit clarification", disabled=True)

    if (
        controller is not None
        and controller.stage == "AWAITING_DECISION_CONFIRMATION"
        and controller.pending_decision is not None
        and controller.pending_decision.get("tool_name") == "generate_scenarios"
    ):
        _render_confirm_decision(state, controller)

    if controller is not None and controller.workflow_state in {"BLOCKED", "STOPPED"}:
        st.error(f"Run {controller.workflow_state}: {controller.halt_reason or '(no reason recorded)'}")


# ---------------------------------------------------------------------------
# Page "scenarios" (Scenarios)
# ---------------------------------------------------------------------------


def _render_coverage_donut(coverage: dict, caption: str) -> None:
    covered = coverage["covered"]
    required = coverage["required"]
    missing = max(required - covered, 0)
    frame = pd.DataFrame({"category": ["Covered", "Missing"], "count": [covered, missing]})
    arc = (
        alt.Chart(frame)
        .mark_arc(innerRadius=55, outerRadius=85)
        .encode(
            theta="count:Q",
            color=alt.Color(
                "category:N",
                scale=alt.Scale(domain=["Covered", "Missing"], range=["#0D9488", "#FCA5A5"]),
                legend=alt.Legend(title=None, orient="bottom"),
            ),
        )
    )
    center_text = (
        alt.Chart(pd.DataFrame({"text": [f"{coverage['percentage']:g}%"]}))
        .mark_text(size=22, fontWeight="bold")
        .encode(text="text:N")
    )
    st.altair_chart(alt.layer(arc, center_text).properties(width=170, height=170))
    st.caption(f"{caption} · {covered} of {required}")


def _render_scenarios_table(scenarios: list[dict], grounding_items: dict, added_ids: set[str]) -> None:
    rows = []
    for scenario in scenarios:
        grounding_item = grounding_items.get(scenario["scenario_id"])
        status = "—"
        if grounding_item is not None:
            status = grounding_item.get("human_resolution") or grounding_item.get("verdict") or "—"
        scenario_label = scenario["title"]
        if scenario["scenario_id"] in added_ids:
            scenario_label += "  —  Added by remediation"
        rows.append(
            {
                "ID": scenario["scenario_id"],
                "Scenario": scenario_label,
                "Rules": ", ".join(scenario.get("rule_ids", [])),
                "Status": status,
            }
        )
    st.table(rows)


def _render_page_scenarios(state: UIState, data: dict) -> None:
    _render_page_header("Scenarios")
    controller: RunController | None = data["controller"]
    versions_list = data.get("versions", [])
    if controller is None or not versions_list:
        st.info("Start analysis to generate scenarios.")
        return

    latest_version = versions_list[-1]["version"]
    by_version = {entry["version"]: entry for entry in versions_list}

    col_left, col_right = st.columns(2)

    with col_left, st.container(border=True):
        _card_heading("Generated scenarios")
        version_ids = [entry["version"] for entry in versions_list]
        selected_version = st.segmented_control(
            "Scenario set version",
            version_ids,
            format_func=lambda v: f"v{v} · {'initial' if v == 1 else 'remediated'}",
            default=latest_version,
            label_visibility="collapsed",
            key="scenarios_version_select",
        )
        if selected_version is None:
            selected_version = latest_version
        entry = by_version[selected_version]
        scenario_set = entry["scenarios"] or {}
        scenarios = scenario_set.get("scenarios", [])
        grounding_items = {item["scenario_id"]: item for item in (entry["grounding"] or {}).get("items", [])}
        previous_entry = by_version.get(selected_version - 1)
        previous_scenarios = (previous_entry["scenarios"] or {}).get("scenarios") if previous_entry else None
        added_ids = added_by_remediation(previous_scenarios, scenarios)

        _render_scenarios_table(scenarios, grounding_items, added_ids)
        if selected_version == 1:
            st.caption("From scenarios_v1.json. S04 is deliberately omitted from this pass (seeded omission).")
        else:
            st.caption(f"From scenarios_v{selected_version}.json.")

        if selected_version == latest_version:
            for scenario_id in entry["unresolved_grounding"]:
                with st.form(key=f"resolve_{selected_version}_{scenario_id}"):
                    st.write(f"Resolve grounding flag for {scenario_id}")
                    # index=None (same fix as the Confirm decision radio
                    # above, same pattern/bug class) -- no default is
                    # silently carried as "the" resolution before a human
                    # has actually picked one. st.form_submit_button's
                    # disabled= can't react live to resolution's value (form
                    # widgets only commit on submit), so the guard against an
                    # empty pick is a post-submit check instead of a
                    # pre-disabled button.
                    resolution = st.radio(
                        "Resolution",
                        ["CONFIRMED_GROUNDED", "CONFIRMED_NOT_GROUNDED"],
                        index=None,
                        key=f"res_{selected_version}_{scenario_id}",
                    )
                    note = st.text_input("Note (optional)", key=f"note_{selected_version}_{scenario_id}")
                    if st.form_submit_button("Resolve", type="secondary", disabled=state.worker_busy):
                        if resolution is None:
                            st.error("Pick a resolution before submitting.")
                        else:
                            with state.lock:  # serialize against a worker thread writing the same run's files
                                controller.resolve_grounding(selected_version, scenario_id, resolution, note or None)
                            st.rerun()

    with col_right, st.container(border=True):
        _card_heading("Coverage")
        donut_entries = [entry for entry in versions_list if entry["effective_coverage"] is not None]
        if len(donut_entries) >= 2:
            before_entry, after_entry = donut_entries[0], donut_entries[-1]
            donut_cols = st.columns([1, 0.3, 1])
            with donut_cols[0]:
                _render_coverage_donut(
                    before_entry["effective_coverage"], f"Before · coverage_v{before_entry['version']}"
                )
            with donut_cols[1]:
                st.markdown(
                    "<div style='text-align:center; padding-top:4rem; font-size:1.5rem;'>→</div>",
                    unsafe_allow_html=True,
                )
            with donut_cols[2]:
                _render_coverage_donut(
                    after_entry["effective_coverage"], f"After · coverage_v{after_entry['version']}"
                )
        elif len(donut_entries) == 1:
            _render_coverage_donut(
                donut_entries[0]["effective_coverage"], f"coverage_v{donut_entries[0]['version']}"
            )

    gap_entry = next(
        (
            entry
            for entry in versions_list
            if entry["effective_coverage"] and entry["effective_coverage"]["covered"] < entry["effective_coverage"]["required"]
        ),
        None,
    )
    coverage_decisions = [d for d in (data.get("decisions") or []) if d.get("checkpoint") == "COVERAGE"]
    if gap_entry is not None or coverage_decisions:
        gap_col, decider_col = st.columns(2)
        if gap_entry is not None:
            with gap_col, st.container(border=True):
                _card_heading("Gap found by coverage check", prefix="⚠️")
                coverage = gap_entry["effective_coverage"]
                catalog = (_load_json(CONFIG_DIR / "scenario_catalog.json") or {}).get("scenarios", {})
                for row in coverage["rows"]:
                    if row["status"] == "VALID":
                        continue
                    title = catalog.get(row["scenario_id"], {}).get("title", "—")
                    st.markdown(f"**Missing:** {row['scenario_id']} — {title}")
                    st.markdown(f"**Uncovered rule:** {', '.join(row['rule_ids'])}")
                st.markdown(
                    f"**Coverage:** {coverage['covered']}/{coverage['required']} — "
                    "below the 5/5 acceptance guard"
                )
        if coverage_decisions:
            with decider_col, st.container(border=True):
                _card_heading("Strands Decider (2B) decision", prefix="✅")
                choice_threshold = round4(_choice_threshold())
                for record in coverage_decisions:
                    source = record.get("decision_source")
                    st.markdown(f"Checkpoint: `{record.get('checkpoint')}`")
                    st.markdown(f"Decision: `{decider_decision_value(record)}`")
                    # The coverage_action question's OWN confidence vs the
                    # per-choice gate (τ 0.75), not the record's top-level
                    # confidence/threshold (the batch's lowest-scoring
                    # verdict vs the route-level gate, 0.8 -- a different
                    # number for a different question, see
                    # coverage_action_confidence's docstring). A
                    # HUMAN_ESCALATION record's top-level confidence/
                    # threshold are always None (the human wasn't scored),
                    # so that line is dropped entirely rather than showing
                    # a dash-dash placeholder.
                    if source != "HUMAN_ESCALATION":
                        confidence = round4(coverage_action_confidence(record))
                        st.markdown(
                            f"Confidence: {confidence if confidence is not None else '—'} "
                            f"vs τ {choice_threshold if choice_threshold is not None else '—'}"
                        )
                    st.markdown(f"Route: {record.get('route')}")
                    st.markdown(f"Source: `{source}`")
                    st.divider()

    with st.container(border=True):
        _card_heading("Gherkin")
        if scenarios:
            tabs = st.tabs([f"{s['scenario_id']} · {s['title']}" for s in scenarios])
            for tab, scenario in zip(tabs, scenarios):
                with tab:
                    gherkin = scenario.get("gherkin")
                    if gherkin:
                        st.code(gherkin, language="gherkin")
                    else:
                        st.code("(no gherkin available)", language="text")

    if (
        controller.stage == "AWAITING_DECISION_CONFIRMATION"
        and controller.pending_decision is not None
        and controller.pending_decision.get("tool_name") == "save_scenario_set"
    ):
        _render_confirm_decision(state, controller)


# ---------------------------------------------------------------------------
# Page "execution" (Execution)
# ---------------------------------------------------------------------------


def _render_page_execution(state: UIState, data: dict) -> None:
    _render_page_header("Execution")
    st.caption("Approve, then execute the five fixed tests.")
    controller: RunController | None = data["controller"]

    col1, col2 = st.columns(2)

    with col1, st.container(border=True):
        _card_heading("Test manifest (fixed)")
        manifest = (_load_json(CONFIG_DIR / "test_manifest.json") or {}).get("tests", [])
        catalog = (_load_json(CONFIG_DIR / "scenario_catalog.json") or {}).get("scenarios", {})
        per_scenario_assertions = {
            test["scenario_id"]: catalog.get(test["scenario_id"], {}).get("required_assertions", [])
            for test in manifest
        }
        shared, remainder = split_shared_assertions(per_scenario_assertions)
        rows = []
        for test in manifest:
            scenario_id = test["scenario_id"]
            title = catalog.get(scenario_id, {}).get("title", "—")
            specific = remainder.get(scenario_id, [])
            expected = "; ".join(format_assertion(a) for a in specific) if specific else "shared checks only"
            rows.append(
                {
                    "ID": test["test_id"],
                    "Scenario": title,
                    "Expected": expected,
                    "Rules": ", ".join(test.get("rule_ids", [])),
                }
            )
        st.table(rows)
        if shared:
            st.caption("All tests also check: " + ", ".join(format_assertion(a) for a in shared) + ".")

    with col2, st.container(border=True):
        _card_heading("Human approval")
        approval = _load_json(controller.run_dir / "approval.json") if controller is not None else None

        if approval is not None:
            decision_label = "Approved" if approval["decision"] == "APPROVE" else "Rejected"
            (st.success if approval["decision"] == "APPROVE" else st.error)(
                f"{decision_label} by {approval['reviewer']} at {approval['timestamp']}"
            )
            if approval.get("comment"):
                st.caption(f"Comment: {approval['comment']}")
            button_cols = st.columns(3)
            button_cols[0].button("Reject", key="reject_btn", icon=":material/lock:", disabled=True)
            button_cols[1].button(
                "Approve", type="primary", key="approve_btn", icon=":material/lock:", disabled=True
            )
            button_cols[2].button(
                "Execute", type="primary", key="execute_btn", icon=":material/lock:", disabled=True
            )
            st.caption("Test execution arrives in Milestone 6.")
        else:
            versions_list = data.get("versions", [])
            latest_version = versions_list[-1]["version"] if versions_list else None
            # Buttons were already gated on workflow_state == AWAITING_APPROVAL
            # via can_approve/can_reject; the inputs weren't -- they stayed
            # editable even before a run reached that state, which doesn't
            # match the buttons' own lock. Same reason, same caption, now
            # for both.
            execution_unlocked = controller is not None and controller.workflow_state == "AWAITING_APPROVAL"
            if controller is None:
                blockers = ["No run yet."]
            elif not execution_unlocked:
                blockers = [f"Locked until the run reaches AWAITING_APPROVAL (currently {controller.workflow_state})."]
            else:
                blockers = controller.approval_blockers(latest_version) if latest_version is not None else []

            if not blockers:
                st.success("Ready for approval — coverage 5/5, no open grounding flags.")
            else:
                for blocker in blockers:
                    st.warning(blocker)

            reviewer = st.text_input("Reviewer name", key="reviewer_name", disabled=not execution_unlocked)
            comment = st.text_input("Comment (optional)", key="approval_comment", disabled=not execution_unlocked)
            st.caption("Approval time is stamped automatically and saved to approval.json.")

            approve_ready = can_approve(state, controller, reviewer, latest_version)
            reject_ready = can_reject(state, controller, reviewer)
            button_cols = st.columns(3)
            with button_cols[0]:
                reject_clicked = st.button(
                    "Reject",
                    key="reject_btn",
                    icon=None if reject_ready else ":material/lock:",
                    disabled=not reject_ready,
                )
            with button_cols[1]:
                approve_clicked = st.button(
                    "Approve",
                    type="primary",
                    key="approve_btn",
                    icon=None if approve_ready else ":material/lock:",
                    disabled=not approve_ready,
                )
            with button_cols[2]:
                st.button("Execute", type="primary", key="execute_btn", icon=":material/lock:", disabled=True)
            st.caption("Test execution arrives in Milestone 6.")

            if reject_clicked and controller is not None and latest_version is not None:
                with state.lock:
                    controller.record_approval("REJECT", reviewer, latest_version, comment or None)
                st.rerun()
            if approve_clicked and controller is not None and latest_version is not None:
                with state.lock:
                    controller.record_approval("APPROVE", reviewer, latest_version, comment or None)
                st.rerun()

    with st.container(border=True):
        _card_heading("Test run")
        st.info("Results appear here after execution (Milestone 6).")


# ---------------------------------------------------------------------------
# Page "results" (Results) -- whole page is a Milestone 6 placeholder.
# ---------------------------------------------------------------------------


def _render_page_results(state: UIState, data: dict) -> None:
    _render_page_header("Results")
    st.info("Results appear after the tests run (Milestone 6).")


_NAV_RENDERERS = {
    "requirements": _render_page_requirements,
    "scenarios": _render_page_scenarios,
    "execution": _render_page_execution,
    "results": _render_page_results,
}


def main() -> None:
    st.set_page_config(page_title="AI Test Engineer Agent", layout="wide")
    state = get_ui_state()

    # One fragment for the whole page (top-bar chips + sidebar + the
    # selected page): a worker finishing must refresh everything on the
    # next tick, not just part of it, or the Confirm decision control /
    # Start button's disabled state would stay stale until a manual
    # Refresh status click.
    @st.fragment(run_every=2)
    def _app_fragment() -> None:
        current_nav_id = st.session_state.get("nav_id", NAV_ITEMS[0][0])
        _inject_css(current_nav_id)
        data = gather_display_data(state)
        _render_top_bar_chips(data)
        current_nav_id = _render_sidebar(state, data, current_nav_id)
        _NAV_RENDERERS[current_nav_id](state, data)

    _app_fragment()


if __name__ == "__main__":
    main()
