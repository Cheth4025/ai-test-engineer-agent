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
import io
import json
import statistics
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

import altair as alt
import pandas as pd
import streamlit as st

from ai_test_engineer import checkpoints, orchestration, reporting, ui_components
from ai_test_engineer.decision_trace import (
    build_decision_trace,
    decider_decision_value,
    decision_icon,
    primary_choice_confidence,
    round4,
    round4_probabilities,
)
from ai_test_engineer.state import RunController

CONFIG_DIR = orchestration.CONFIG_DIR

APP_TITLE = "Assurance Agent Experience"

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
    "finalize_analysis": "execution",
    "render_report": "execution",
}

# Doc 06's escalation checkpoints and their fixed option sets -- same mapping
# scripts/run_initial_pass.py._options_for hardcodes. Presentation only
# (which buttons to draw), not business logic, so duplicating this tiny
# mapping here is cheaper than threading it through orchestration.py.
OPTIONS_FOR_TOOL: dict[str, list[str]] = {
    "save_scenario_set": ["ACCEPT_COVERAGE", "REMEDIATE_GAP"],
    "generate_scenarios": ["GENERATE_TESTS", "REQUEST_CLARIFICATION"],
    "finalize_analysis": ["INVESTIGATE_FAILURE", "REPORT_FAILURE"],
    "render_report": ["FINALIZE_RESULTS", "NOT_READY"],
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


def can_execute(state: UIState, controller: RunController | None) -> bool:
    """Mirrors RunController.use_execution()/execute_approved_run's own
    guard (the real, data-layer enforcement) so the UI can disable a doomed
    click -- never the only guard. A second Execute is refused at the data
    layer regardless of this function's answer."""
    if controller is None or state.worker_busy:
        return False
    return controller.workflow_state == "APPROVED" and not controller.execution_started


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


def execute_run(state: UIState) -> bool:
    with state.lock:
        controller = current_controller(state)
        if not can_execute(state, controller):
            return False
        state.worker_busy = True
        state.last_error = None
        run_id = state.current_run_id
    known_before = frozenset(orchestration._ACTIVE_RUNS.keys())
    thread = threading.Thread(
        target=_run_worker,
        args=(state, lambda: orchestration.execute_approved_run(run_id), known_before),
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
    only, no invented short phrase (resolved with the user). PRECEDES has
    exactly one real instance in the catalog (status_verification_before_
    retry_charge PRECEDES True) and reads as "<observation> precedes True"
    otherwise, which is meaningless to a reader -- rendered as the plain-
    English phrase instead. Booleans show as true/false (Python's True/
    False, not this)."""
    if assertion["operator"] == "PRECEDES":
        return "verified before retry charge"
    expected = assertion["expected"]
    if isinstance(expected, bool):
        expected = "true" if expected else "false"
    return f"{assertion['observation']} == {expected}"


def build_evidence_zip(run_dir: Path) -> bytes | None:
    """An in-memory zip of the run folder's own JSON/log/xml/md files, built
    fresh at click time from files that already exist on disk -- never
    written to the run folder, never regenerates or reruns anything. Owner-
    approved deviation from docs/06's download-guard row and docs/mockups/
    README.md deviation #10 (both specify individual-file downloads only,
    no zip) -- recorded in the PR description, not in the docs themselves.
    Returns None when the run folder has nothing to bundle yet."""
    if not run_dir.exists():
        return None
    paths = sorted(p for p in run_dir.rglob("*") if p.is_file() and p.suffix in {".json", ".log", ".xml", ".md"})
    if not paths:
        return None
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, arcname=str(path.relative_to(run_dir)))
    return buffer.getvalue()


def sidebar_pending_action(controller: RunController | None) -> dict | None:
    """The one pending human action the sidebar notice should name:
    {"kind": "decision"|"approval", "section_id": <nav id>}, or None. A
    pending Confirm-decision escalation takes priority over
    AWAITING_APPROVAL -- the two stages are mutually exclusive in practice
    (escalating sets stage to AWAITING_DECISION_CONFIRMATION, not
    AWAITING_APPROVAL), but checking decision first keeps this correct even
    if that ever changes. Exactly one result, never both at once."""
    if controller is None:
        return None
    if controller.pending_decision is not None:
        section_id = PENDING_SECTION_FOR_TOOL.get(controller.pending_decision.get("tool_name"))
        if section_id is not None:
            return {"kind": "decision", "section_id": section_id}
    if controller.workflow_state == "AWAITING_APPROVAL":
        return {"kind": "approval", "section_id": "execution"}
    if controller.workflow_state == "APPROVED" and not controller.execution_started:
        return {"kind": "execute_ready", "section_id": "execution"}
    return None


def pending_action(controller: RunController | None, versions: list[dict] | None = None) -> dict | None:
    """sidebar_pending_action's two cases, plus a third fallback the page
    banners (but not the sidebar notice) also surface: an unresolved
    grounding flag on the LATEST scenario version -- an optional review
    step, not a run-blocking one, so it's the lowest priority of the
    three."""
    action = sidebar_pending_action(controller)
    if action is not None:
        return action
    if versions and versions[-1].get("unresolved_grounding"):
        return {"kind": "grounding", "section_id": "scenarios"}
    return None


def _host_label() -> str:
    decider_cfg = _load_json(CONFIG_DIR / "decider.json") or {}
    try:
        return checkpoints.resolve_host_label(decider_cfg)
    except KeyError:
        return "unknown"


def _decider_cfg() -> dict:
    return _load_json(CONFIG_DIR / "decider.json") or {}


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
    data["results"] = _load_json(run_dir / "results.json")
    data["analysis"] = _load_json(run_dir / "analysis.json")
    data["report_metadata"] = _load_json(run_dir / "report_metadata.json")
    return data


# ---------------------------------------------------------------------------
# Rendering -- Streamlit-dependent; exercised by tests/ui/test_ui_apptest.py.
# ---------------------------------------------------------------------------

# Top-bar title has no [theme]/[theme.sidebar] config equivalent (theme
# options style the main area and sidebar, never Streamlit's own header
# chrome) -- a scoped ::before pseudo-element carrying APP_TITLE, kept
# separate from ui_components.AAE_CSS (which is APP_TITLE-independent and
# shared with every other aae- selector). All other top-bar/sidebar/card/
# table/pill/etc. styling lives in ui_components.AAE_CSS -- see that
# module's docstring for the full token-to-selector mapping.
# .block-container's padding is CSS-only (24px 28px, all four sides, per
# ui_components.AAE_CSS): .streamlit/config.toml's theme.paddingTop/
# paddingBottom are pinned to "0rem" so the two mechanisms never stack.
def _title_css(app_title: str) -> str:
    return f"""
    <style>
    [data-testid="stHeader"]::before {{
        content: "{html.escape(app_title)}";
        position: absolute;
        left: 0;
        top: 50%;
        transform: translateY(-50%);
        color: #F5F5F5;
        font-size: 18px;
        font-weight: 700;
        white-space: nowrap;
    }}
    </style>
    """


def _page_config_kwargs() -> dict:
    return {"page_title": APP_TITLE, "layout": "wide"}


def _nav_css(current_nav_id: str) -> str:
    """Highlights the currently-selected sidebar nav button: lighter navy
    background + an inset teal left bar (box-shadow, not a real border --
    avoids a layout shift versus the unselected buttons' own 0-border
    sizing in ui_components.AAE_CSS). Uses the .st-key-<key> hook Streamlit
    documents for exactly this (same mechanism as Approve/Reject's colors
    in AAE_CSS) -- current_nav_id is read from st.session_state before
    this is injected, so it's always correct for the render about to
    happen."""
    return f"""
    <style>
    .st-key-nav_{current_nav_id} button {{
        background-color: #1D3150 !important;
        color: #FFFFFF !important;
        box-shadow: inset 3px 0 0 #2DD4BF !important;
    }}
    </style>
    """


def _inject_css(current_nav_id: str) -> None:
    st.markdown(ui_components.AAE_CSS, unsafe_allow_html=True)
    st.markdown(_title_css(APP_TITLE), unsafe_allow_html=True)
    st.markdown(_nav_css(current_nav_id), unsafe_allow_html=True)


def _render_top_bar_chips(data: dict) -> None:
    """Three monospace pill chips, top-right of the header: run id,
    state · stage, Decider host. Real Streamlit-rendered HTML (not JS),
    position:fixed so it visually layers onto the header bar, re-rendered
    fresh every fragment tick with current values. Every run-derived value
    is ui_components.escape()'d before going into this unsafe_allow_html
    block."""
    controller: RunController | None = data["controller"]
    if controller is not None:
        run_chip = f"run {controller.run_id}"
        state_chip = f"{controller.workflow_state} · {controller.stage}"
    else:
        run_chip = "run (none)"
        state_chip = "no run"
    host_chip = f"Decider · {data['host_label']}"
    spans = "".join(
        f'<span class="aae-chip">{ui_components.escape(text)}</span>' for text in (run_chip, state_chip, host_chip)
    )
    st.markdown(f'<div class="aae-status-chips">{spans}</div>', unsafe_allow_html=True)


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
            "Refresh status",
            type="secondary",
            icon=":material/refresh:",
            width="stretch",
            key="refresh_status_btn",
        ):
            st.rerun()
        if state.last_error:
            st.error(f"Last run failed: {state.last_error}")

        action = sidebar_pending_action(controller)
        if action is not None:
            label = NAV_LABELS[action["section_id"]]
            if action["kind"] == "decision":
                st.warning(f"A decision needs your confirmation — see '{label}'.")
            elif action["kind"] == "approval":
                st.warning(f"Approval needed — see '{label}'.")
            else:
                st.warning(f"Ready to execute — see '{label}'.")
    return current_nav_id


def _render_page_header(title: str) -> None:
    st.title(title)
    st.caption("Payment Retry & Duplicate Charge Prevention")


_ACTION_ANCHOR_ID = "aae-action-anchor"

_ACTION_VERB = {
    "decision": "confirm the Decider decision",
    "grounding": "resolve the grounding flag",
    "approval": "record the approval decision",
    "execute_ready": "run the approved tests",
}


def _render_action_anchor() -> None:
    """A same-page scroll target right before the control a pending
    action's banner link jumps to. Each of the three controls
    (_render_confirm_decision, the grounding-resolution forms, the Human
    approval card) only renders this when it is ALSO the one pending_action
    currently names -- never unconditionally -- so at most one element on
    the page ever carries this id."""
    st.markdown(f'<div id="{_ACTION_ANCHOR_ID}"></div>', unsafe_allow_html=True)


def _render_action_banner(data: dict, current_section_id: str) -> None:
    """Top-of-page notice naming the one pending human action (see
    pending_action's priority order) so it's never missed just because the
    user is looking at a different page. On the page that actually hosts
    the control, it's a same-page "Jump to it" anchor link; everywhere
    else, a button that switches to that page (there's no URL to deep-link
    to -- navigation is plain session_state, not routing)."""
    controller: RunController | None = data["controller"]
    action = pending_action(controller, data.get("versions"))
    if action is None:
        return
    verb = _ACTION_VERB[action["kind"]]
    if action["section_id"] == current_section_id:
        jump_link = f'<a href="#{_ACTION_ANCHOR_ID}">Jump to it</a>'
        st.markdown(
            ui_components.banner(
                f"Action needed below: {verb}.", None, variant="action-needed", action_html=jump_link
            ),
            unsafe_allow_html=True,
        )
        return
    label = NAV_LABELS[action["section_id"]]
    banner_col, button_col = st.columns([5, 1], vertical_alignment="center")
    with banner_col:
        st.markdown(
            ui_components.banner(f"Action needed: {verb} — see '{label}'.", None, variant="action-needed"),
            unsafe_allow_html=True,
        )
    with button_col:
        if st.button(f"Go to {label}", key="jump_to_action_page"):
            st.session_state["nav_id"] = action["section_id"]
            st.rerun()


def _card_heading(text: str, *, prefix: str = "") -> None:
    """A section heading inside a bordered card, styled via the
    .aae-card-heading CSS class rather than plain "**bold**" markdown --
    several pages also use inline bold field labels (e.g. "**Checkpoint:**")
    at body-text size, so a card heading needs its own distinct class to be
    sized independently without a blanket rule touching those too."""
    prefix_html = f"{ui_components.escape(prefix)} " if prefix else ""
    st.markdown(f'<p class="aae-card-heading">{prefix_html}{ui_components.escape(text)}</p>', unsafe_allow_html=True)


def _render_decision_trace_rows(rows: list[dict]) -> None:
    """Renders decision_trace.build_decision_trace's own rows verbatim --
    the Scenarios page's COVERAGE-only card and the Results page's
    all-checkpoints card both call this on their own (pre-filtered) row
    list, so there is exactly one place that decides how a row looks."""
    for row in rows:
        st.markdown(f"Checkpoint: `{row['checkpoint']}`")
        st.markdown(f"{row['icon']} Decision: `{row['decision_text']}`")
        if row["confidence"] is not None:
            threshold = row["threshold"] if row["threshold"] is not None else "—"
            st.markdown(f"Confidence: {row['confidence']} vs τ {threshold}")
        st.markdown(f"Route: {row['route']}")
        st.markdown(f"Source: `{row['source']}`")
        st.divider()


def _render_confirm_decision(state: UIState, controller: RunController) -> None:
    pending = controller.pending_decision
    _render_action_anchor()  # whenever this renders, pending_action always names "decision" here
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
    _render_action_banner(data, "requirements")
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
    with col1:
        use_case_body = (
            ui_components.labeled_section("aae-label-problem", "PROBLEM", _USE_CASE_PROBLEM)
            + ui_components.labeled_section("aae-label-impact", "IMPACT", _USE_CASE_IMPACT)
            + ui_components.labeled_section("aae-label-mitigation", "MITIGATION", _USE_CASE_MITIGATION)
        )
        st.markdown(ui_components.card("The use case", use_case_body), unsafe_allow_html=True)
    with col2:
        requirement = data["requirement"] or {}
        rules = parse_business_rules(requirement.get("text"))
        br_rows = [
            [ui_components.escape(r["rule_id"]), ui_components.escape(r["label"]), ui_components.escape(r["description"])] for r in rules
        ]
        br_body = ui_components.table(["ID", "Rule", "Description"], br_rows) + ui_components.small_caption(
            "Loaded from the requirements catalog — wording follows the source file."
        )
        st.markdown(ui_components.card("Business rules (BR01 – BR06)", br_body), unsafe_allow_html=True)

    col3, col4 = st.columns(2)
    with col3:
        criteria_body = "<ul>" + "".join(f"<li>{ui_components.escape(item)}</li>" for item in _ACCEPTANCE_CRITERIA) + "</ul>"
        st.markdown(ui_components.card("Key acceptance criteria", criteria_body), unsafe_allow_html=True)
    with col4:
        steps_body = "<ol>" + "".join(f"<li>{ui_components.escape(step)}</li>" for step in _AGENT_STEPS) + "</ol>"
        st.markdown(ui_components.card("What the agent does", steps_body), unsafe_allow_html=True)

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
    # A "label" column carries the exact tooltip text ("Covered: 4") --
    # without an explicit tooltip encoding, Altair's default tooltip shows
    # every raw encoded field (count, category, and the auto-generated
    # _category_sort_index), which is meaningless to a reader. The legend
    # is dropped entirely (not placed below) rather than fought with for
    # space -- the caption below already names covered/required, and a
    # fixed-height chart can't both show the full ring AND grow to fit a
    # legend without clipping one or the other.
    frame = pd.DataFrame(
        {
            "category": ["Covered", "Missing"],
            "count": [covered, missing],
            "label": [f"Covered: {covered}", f"Missing: {missing}"],
        }
    )
    arc = (
        alt.Chart(frame)
        .mark_arc(innerRadius=55, outerRadius=85)
        .encode(
            theta="count:Q",
            color=alt.Color(
                "category:N",
                scale=alt.Scale(domain=["Covered", "Missing"], range=["#0D9488", "#F4C7C3"]),
                legend=None,
            ),
            tooltip=alt.Tooltip("label:N", title=None),
        )
    )
    center_text = (
        alt.Chart(pd.DataFrame({"text": [f"{coverage['percentage']:g}%"]}))
        .mark_text(size=22, fontWeight="bold")
        .encode(text="text:N")
    )
    # 200x200 around an outerRadius=85 (170px diameter) ring -- 15px
    # clearance on every side so the ring is never clipped at the edge.
    st.altair_chart(alt.layer(arc, center_text).properties(width=200, height=200))
    st.markdown(ui_components.small_caption(f"{caption} · {covered} of {required}"), unsafe_allow_html=True)


# Maps the real grounding verdict/human_resolution literals (docs/05
# Section: GROUNDED/NOT_GROUNDED/NEEDS_REVIEW, CONFIRMED_GROUNDED/
# CONFIRMED_NOT_GROUNDED -- internal names, unchanged) to the short
# "Rule match" pill label+variant shown in the Scenarios table. Pulled
# forward from the wording-map commit (plan section C6-8): a pill styled
# in Commit B with the OLD raw literal as its text would be an unreadable
# badge, so the two land together for this one column.
_RULE_MATCH_PILL = {
    "CONFIRMED_GROUNDED": ("Matches", "matches"),
    "GROUNDED": ("Matches", "matches"),
    "CONFIRMED_NOT_GROUNDED": ("No match", "no-match"),
    "NOT_GROUNDED": ("No match", "no-match"),
    "NEEDS_REVIEW": ("Needs review", "waiting"),
}


def _render_scenarios_table(scenarios: list[dict], grounding_items: dict, added_ids: set[str]) -> None:
    rows = []
    row_classes = []
    for scenario in scenarios:
        scenario_id = scenario["scenario_id"]
        grounding_item = grounding_items.get(scenario_id)
        raw_status = None
        if grounding_item is not None:
            raw_status = grounding_item.get("human_resolution") or grounding_item.get("verdict")

        if scenario_id in added_ids:
            rule_match_cell = ui_components.pill("Added by remediation", "added-remediation")
        elif raw_status in _RULE_MATCH_PILL:
            rule_match_cell = ui_components.pill(*_RULE_MATCH_PILL[raw_status])
        else:
            rule_match_cell = "—"

        rows.append(
            [
                ui_components.escape(scenario_id),
                ui_components.escape(scenario["title"]),
                ui_components.escape(", ".join(scenario.get("rule_ids", []))),
                rule_match_cell,
            ]
        )
        row_classes.append("aae-row--added" if scenario_id in added_ids else "")
    st.markdown(
        ui_components.table(["ID", "Scenario", "Rules", "Rule match"], rows, row_classes=row_classes),
        unsafe_allow_html=True,
    )


def _render_page_scenarios(state: UIState, data: dict) -> None:
    _render_page_header("Scenarios")
    _render_action_banner(data, "scenarios")
    controller: RunController | None = data["controller"]
    versions_list = data.get("versions", [])
    if controller is None or not versions_list:
        st.info("Start analysis to generate scenarios.")
        return

    latest_version = versions_list[-1]["version"]
    by_version = {entry["version"]: entry for entry in versions_list}

    col_left, col_right = st.columns(2)

    with col_left, ui_components.widget_card_container("generated-scenarios"):
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
            caption_text = "From scenarios_v1.json. S04 is deliberately omitted from this pass (seeded omission)."
        else:
            caption_text = f"From scenarios_v{selected_version}.json."
        st.markdown(ui_components.small_caption(caption_text), unsafe_allow_html=True)

        if selected_version == latest_version:
            current_action = pending_action(controller, versions_list)
            for index, scenario_id in enumerate(entry["unresolved_grounding"]):
                if index == 0 and current_action is not None and current_action["kind"] == "grounding":
                    _render_action_anchor()
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

    with col_right, ui_components.widget_card_container("coverage"):
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
    coverage_rows = [
        row for row in build_decision_trace(data.get("decisions") or [], _decider_cfg()) if row["checkpoint"] == "COVERAGE"
    ]
    if gap_entry is not None or coverage_rows:
        gap_col, decider_col = st.columns(2)
        if gap_entry is not None:
            with gap_col:
                coverage = gap_entry["effective_coverage"]
                catalog = (_load_json(CONFIG_DIR / "scenario_catalog.json") or {}).get("scenarios", {})
                gap_lines = []
                for row in coverage["rows"]:
                    if row["status"] == "VALID":
                        continue
                    title = catalog.get(row["scenario_id"], {}).get("title", "—")
                    gap_lines.append(f"<p><strong>Missing:</strong> {ui_components.escape(row['scenario_id'])} — {ui_components.escape(title)}</p>")
                    gap_lines.append(
                        f"<p><strong>Uncovered rule:</strong> {ui_components.escape(', '.join(row['rule_ids']))}</p>"
                    )
                gap_lines.append(
                    f"<p><strong>Coverage:</strong> {coverage['covered']}/{coverage['required']} — "
                    "below the 5/5 acceptance guard</p>"
                )
                st.markdown(
                    ui_components.card("⚠️ Gap found by coverage check", "".join(gap_lines), tone="gap"),
                    unsafe_allow_html=True,
                )
        if coverage_rows:
            # _render_decision_trace_rows (and this card's wrapper/tone)
            # are deferred to Commit C5, which replaces it with
            # decision_trace_table() once decision_trace.py's "outcome"
            # field exists -- see the polish-1 ledger.
            with decider_col, ui_components.widget_card_container("coverage-decider-trace"):
                _card_heading("Strands Decider (2B) decision")
                _render_decision_trace_rows(coverage_rows)

    with ui_components.widget_card_container("gherkin"):
        _card_heading("Gherkin")
        if scenarios:
            tabs = st.tabs([f"{s['scenario_id']} · {s['title']}" for s in scenarios])
            for tab, scenario in zip(tabs, scenarios):
                with tab:
                    gherkin = scenario.get("gherkin")
                    if gherkin:
                        block = ui_components.code_block(ui_components.gherkin_highlight(gherkin), language="gherkin")
                    else:
                        block = ui_components.code_block(ui_components.escape("(no gherkin available)"))
                    st.markdown(block, unsafe_allow_html=True)

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
    _render_action_banner(data, "execution")
    controller: RunController | None = data["controller"]

    col1, col2 = st.columns(2)

    with col1:
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
                [
                    ui_components.escape(test["test_id"]),
                    ui_components.escape(title),
                    ui_components.escape(expected),
                    ui_components.escape(", ".join(test.get("rule_ids", []))),
                ]
            )
        manifest_body = ui_components.table(["ID", "Scenario", "Expected", "Rules"], rows)
        if shared:
            manifest_body += ui_components.small_caption(
                "All tests also check: " + ", ".join(format_assertion(a) for a in shared) + "."
            )
        st.markdown(ui_components.card("Test manifest (fixed)", manifest_body), unsafe_allow_html=True)

    with col2, ui_components.widget_card_container("human-approval"):
        _card_heading("Human approval")
        approval = _load_json(controller.run_dir / "approval.json") if controller is not None else None

        if approval is not None:
            decision_label = "Approved" if approval["decision"] == "APPROVE" else "Rejected"
            st.markdown(
                ui_components.verdict(
                    f"{decision_label} by {approval['reviewer']} at {approval['timestamp']}",
                    None,
                    ok=approval["decision"] == "APPROVE",
                ),
                unsafe_allow_html=True,
            )
            if approval.get("comment"):
                st.markdown(ui_components.small_caption(f"Comment: {approval['comment']}"), unsafe_allow_html=True)
            execute_ready = can_execute(state, controller)
            if execute_ready:
                _render_action_anchor()
            button_cols = st.columns(3)
            button_cols[0].button("Reject", key="reject_btn", icon=":material/lock:", disabled=True)
            button_cols[1].button(
                "Approve", type="primary", key="approve_btn", icon=":material/lock:", disabled=True
            )
            with button_cols[2]:
                execute_clicked = st.button(
                    "Execute",
                    type="primary",
                    key="execute_btn",
                    icon=None if execute_ready else ":material/lock:",
                    disabled=not execute_ready,
                )
            if controller is not None and controller.execution_started:
                caption_text = f"Execution status: {controller.workflow_state}."
            elif execute_ready:
                caption_text = "Ready to run the five fixed tests."
            else:
                caption_text = "Execute unlocks once the run reaches APPROVED."
            st.markdown(ui_components.small_caption(caption_text), unsafe_allow_html=True)
            if execute_clicked:
                execute_run(state)
                st.rerun()
        else:
            versions_list = data.get("versions", [])
            latest_version = versions_list[-1]["version"] if versions_list else None
            # Buttons were already gated on workflow_state == AWAITING_APPROVAL
            # via can_approve/can_reject; the inputs weren't -- they stayed
            # editable even before a run reached that state, which doesn't
            # match the buttons' own lock. Same reason, same caption, now
            # for both.
            execution_unlocked = controller is not None and controller.workflow_state == "AWAITING_APPROVAL"
            if execution_unlocked:
                _render_action_anchor()
            if controller is None:
                blockers = ["No run yet."]
            elif not execution_unlocked:
                blockers = [f"Locked until the run reaches AWAITING_APPROVAL (currently {controller.workflow_state})."]
            else:
                blockers = controller.approval_blockers(latest_version) if latest_version is not None else []

            if not blockers:
                st.markdown(
                    ui_components.banner(
                        "Ready for approval — coverage 5/5, no open grounding flags.", None, variant="workflow-ok"
                    ),
                    unsafe_allow_html=True,
                )
            else:
                for blocker in blockers:
                    st.markdown(
                        ui_components.banner(blocker, None, variant="action-needed"), unsafe_allow_html=True
                    )

            reviewer = st.text_input("Reviewer name", key="reviewer_name", disabled=not execution_unlocked)
            comment = st.text_input("Comment (optional)", key="approval_comment", disabled=not execution_unlocked)
            st.markdown(
                ui_components.small_caption("Approval time is stamped automatically and saved to approval.json."),
                unsafe_allow_html=True,
            )

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
            st.markdown(
                ui_components.small_caption("Execute unlocks once the run reaches APPROVED."),
                unsafe_allow_html=True,
            )

            if reject_clicked and controller is not None and latest_version is not None:
                with state.lock:
                    controller.record_approval("REJECT", reviewer, latest_version, comment or None)
                st.rerun()
            if approve_clicked and controller is not None and latest_version is not None:
                with state.lock:
                    controller.record_approval("APPROVE", reviewer, latest_version, comment or None)
                st.rerun()

    if (
        controller is not None
        and controller.pending_decision is not None
        and controller.pending_decision.get("tool_name") in ("finalize_analysis", "render_report")
    ):
        _render_confirm_decision(state, controller)

    results = data.get("results")
    run_col, log_col = st.columns(2)
    with run_col:
        if results is None:
            body = "<p>Results appear here after execution.</p>"
        else:
            tests = results["tests"]
            passed = sum(1 for t in tests if t["status"] == "PASS")
            duration_s = round(results["duration_ms"] / 1000, 1)
            rows = [
                [ui_components.escape(t["test_id"]), ui_components.escape(t["scenario_id"]), ui_components.pill(t["status"], t["status"].lower())]
                for t in tests
            ]
            row_classes = ["aae-row--fail" if t["status"] == "FAIL" else "" for t in tests]
            body = ui_components.small_caption(
                f"{passed}/{len(tests)} completed · {duration_s}s"
            ) + ui_components.table(["ID", "Scenario", "Result"], rows, row_classes=row_classes)
        st.markdown(ui_components.card("Test run", body), unsafe_allow_html=True)
    with log_col:
        log_path = controller.run_dir / "execution" / "pytest_output.log" if controller is not None else None
        if log_path is not None and log_path.exists():
            log_body = ui_components.code_block(
                ui_components.log_highlight(log_path.read_text(encoding="utf-8", errors="replace"))
            )
        else:
            log_body = "<p>Real pytest output appears here after execution.</p>"
        st.markdown(ui_components.card("Execution log", log_body), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Page "results" (Results)
# ---------------------------------------------------------------------------


def _render_page_results(state: UIState, data: dict) -> None:
    _render_page_header("Results")
    _render_action_banner(data, "results")
    controller: RunController | None = data["controller"]
    results = data.get("results")
    if controller is None or results is None:
        st.info("Results appear after the tests run.")
        return

    tests = results["tests"]
    metadata = data.get("report_metadata") or {}
    workflow_status = metadata.get("agent_workflow_status") or controller.workflow_state
    verification_status = metadata.get("payment_verification_status") or reporting.payment_verification_status(results)

    banner_cols = st.columns(2)
    with banner_cols[0]:
        st.markdown(
            ui_components.verdict(f"Agent workflow: {workflow_status}", None, ok=workflow_status == "COMPLETED"),
            unsafe_allow_html=True,
        )
    with banner_cols[1]:
        st.markdown(
            ui_components.verdict(
                f"Payment verification: {verification_status}", None, ok=verification_status == "PASSED"
            ),
            unsafe_allow_html=True,
        )

    total = len(tests)
    passed = sum(1 for t in tests if t["status"] == "PASS")
    failed = total - passed
    coverage = data.get("coverage_final") or {}
    coverage_text = f"{coverage['covered']}/{coverage['required']}" if coverage else "—"
    duration_s = round(results["duration_ms"] / 1000, 1)

    st.markdown(
        ui_components.kpi_row(
            [
                {"label": "Total tests", "value": str(total), "tone": "default"},
                {"label": "Passed", "value": str(passed), "tone": "passed"},
                {"label": "Failed", "value": str(failed), "tone": "failed"},
                {"label": "Coverage", "value": coverage_text, "tone": "default"},
                {"label": "Duration", "value": f"{duration_s}s", "tone": "default"},
            ]
        ),
        unsafe_allow_html=True,
    )

    results_rows = [
        [
            ui_components.escape(t["test_id"]),
            ui_components.escape(t["scenario_id"]),
            ui_components.escape(t["expected"]),
            ui_components.escape(t["actual"]),
            ui_components.pill(t["status"], t["status"].lower()),
        ]
        for t in tests
    ]
    results_row_classes = ["aae-row--fail" if t["status"] == "FAIL" else "" for t in tests]
    results_body = ui_components.table(
        ["ID", "Scenario", "Expected", "Actual", "Status"], results_rows, row_classes=results_row_classes
    ) + ui_components.small_caption("Actual values are recorded by each test, never copied from Expected.")
    st.markdown(ui_components.card("Test results", results_body), unsafe_allow_html=True)

    analysis = data.get("analysis")
    has_failure = bool(analysis and analysis.get("has_failure"))
    failure_col, decider_col = st.columns(2)
    with failure_col:
        heading = f"Failure — {analysis['test_id']}" if has_failure else "Failure"
        if has_failure:
            failure_lines = [
                f"<p><strong>What happened:</strong> {ui_components.escape(analysis['actual_behavior'])}</p>",
                f'<div class="aae-rule-quote"><strong>Rule violated:</strong> {ui_components.escape(analysis["expected_behavior"])}</div>',
                "<p><strong>Impact:</strong></p>",
                "<ul>" + "".join(f"<li>{ui_components.escape(item)}</li>" for item in analysis.get("impact", [])) + "</ul>",
                "<p><strong>Next step:</strong> Developer fix. The agent reports defects; it does not change code.</p>",
            ]
            failure_body = "".join(failure_lines)
        else:
            failure_body = "<p>No failure observed.</p>"
        st.markdown(ui_components.card(heading, failure_body, tone="failure"), unsafe_allow_html=True)

    with decider_col, ui_components.widget_card_container("results-decider-trace"):
        # _render_decision_trace_rows (and this card's wrapper/tone) are
        # deferred to Commit C5, which replaces it with
        # decision_trace_table() once decision_trace.py's "outcome" field
        # exists -- see the polish-1 ledger.
        _card_heading("Decider decision trace")
        trace_rows = build_decision_trace(data.get("decisions") or [], _decider_cfg())
        if not trace_rows:
            st.markdown(ui_components.small_caption("No decisions recorded."), unsafe_allow_html=True)
        _render_decision_trace_rows(trace_rows)

    if has_failure:
        with ui_components.widget_card_container("evidence"):
            _card_heading(f"Evidence — {analysis['test_id']}")
            failed_test = next((t for t in tests if t["test_id"] == analysis["test_id"]), None)
            evidence_reference = failed_test.get("evidence_reference") if failed_test else None
            evidence = _load_json(controller.run_dir / evidence_reference) if evidence_reference else None
            evidence = evidence or {}
            tabs = st.tabs(["Callbacks", "Ledger entries", "Test log"])
            with tabs[0]:
                callbacks = [e for e in evidence.get("events", []) if e.get("action") == "callback_delivered"]
                st.markdown(
                    ui_components.code_block(ui_components.escape(json.dumps(callbacks, indent=2)), language="json"),
                    unsafe_allow_html=True,
                )
            with tabs[1]:
                st.markdown(
                    ui_components.code_block(
                        ui_components.escape(json.dumps(evidence.get("ledger", []), indent=2)), language="json"
                    ),
                    unsafe_allow_html=True,
                )
            with tabs[2]:
                log_path = controller.run_dir / results.get("log_reference", "")
                log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else "(no log captured)"
                st.markdown(
                    ui_components.code_block(ui_components.log_highlight(log_text)), unsafe_allow_html=True
                )
            st.markdown(
                ui_components.small_caption("Real JSON is whatever the test wrote to the run's evidence folder."),
                unsafe_allow_html=True,
            )

    report_path = controller.run_dir / "report.md"
    download_cols = st.columns(2)
    with download_cols[0]:
        if report_path.exists():
            st.download_button(
                "Download report (.md)",
                data=report_path.read_bytes(),
                file_name="report.md",
                mime="text/markdown",
            )
        else:
            st.button("Download report (.md)", key="download_report_btn", icon=":material/lock:", disabled=True)
    with download_cols[1]:
        zip_bytes = build_evidence_zip(controller.run_dir)
        if zip_bytes is not None:
            st.download_button(
                "Download evidence (.zip)",
                data=zip_bytes,
                file_name=f"{controller.run_id}_evidence.zip",
                mime="application/zip",
            )
        else:
            st.button("Download evidence (.zip)", key="download_evidence_btn", icon=":material/lock:", disabled=True)


_NAV_RENDERERS = {
    "requirements": _render_page_requirements,
    "scenarios": _render_page_scenarios,
    "execution": _render_page_execution,
    "results": _render_page_results,
}


def main() -> None:
    st.set_page_config(**_page_config_kwargs())
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
