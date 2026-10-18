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

import json
import statistics
import threading
import time
from pathlib import Path
from typing import Any, Callable

import streamlit as st

from ai_test_engineer import checkpoints, orchestration
from ai_test_engineer.state import RunController

CONFIG_DIR = orchestration.CONFIG_DIR

SECTIONS = [
    "Overview & Requirements",
    "Scenarios & Coverage",
    "Approval & Execution",
    "Results & Report",
]

# Which section owns the Confirm decision control for a given pending
# checkpoint tool -- the sidebar points the user there when a decision is
# awaiting confirmation but they're looking at a different section.
PENDING_SECTION_FOR_TOOL = {
    "generate_scenarios": SECTIONS[0],
    "save_scenario_set": SECTIONS[1],
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

# The one question per checkpoint whose suggestion should preselect the
# Confirm decision radio -- the other verdicts in pending["verdicts"] are
# per-scenario grounding/ambiguity yes-no questions, not the actual
# action choice (dict iteration order puts those first, see checkpoints.py's
# build_generate_scenarios_checkpoint/build_coverage_checkpoint).
CHOICE_QUESTION_FOR_TOOL = {
    "save_scenario_set": "coverage_action",
    "generate_scenarios": "strategy_action",
}


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


def _host_label() -> str:
    decider_cfg = _load_json(CONFIG_DIR / "decider.json") or {}
    try:
        return checkpoints.resolve_host_label(decider_cfg)
    except KeyError:
        return "unknown"


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

# Top-bar text and Approve/Reject's fixed colors have no [theme]/[theme.sidebar]
# config equivalent (theme options style the main area and sidebar, never
# Streamlit's own header chrome, and there's no per-button color option) --
# both are explicitly requested as scoped CSS. Padding is deliberately NOT
# done here: theme.paddingTop/theme.sidebar.paddingTop in .streamlit/config.toml
# is the native, officially-recommended mechanism (Streamlit's own guidance
# is to avoid CSS .block-container padding), and already reserves space for
# the header's height automatically.
_CSS = """
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
</style>
"""


def _inject_css() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


def _render_sidebar(state: UIState, data: dict) -> str:
    """Run status + navigation, all in the dark left sidebar (docs/mockups/
    README.md row 1, owner-approved layout change -- see module docstring).
    Navigation is a plain st.radio: selecting a section is read-only and
    never calls start_analysis/confirm_decision, same guarantee the former
    st.tabs gave (tests/ui/test_ui_apptest.py's reload-safety test covers
    this directly)."""
    controller: RunController | None = data["controller"]
    with st.sidebar:
        st.caption("Payment Retry & Duplicate Charge Prevention")
        st.metric("Run ID", controller.run_id if controller else "(none)")
        workflow_state = controller.workflow_state if controller else "(no run)"
        stage = controller.stage if controller else "(no run)"
        st.metric("Workflow state", workflow_state, help=f"stage: {stage}")
        busy = state.worker_busy or (controller.busy if controller else False)
        st.metric("Decider host", data["host_label"], help="busy" if busy else "idle")
        if busy:
            st.info("Working...")
        if state.last_error:
            st.error(f"Last run failed: {state.last_error}")
        if st.button("Refresh Status", type="secondary"):
            st.rerun()

        if controller is not None and controller.pending_decision is not None:
            target_section = PENDING_SECTION_FOR_TOOL.get(controller.pending_decision.get("tool_name"))
            if target_section is not None:
                st.warning(f"A decision needs your confirmation -- see '{target_section}'.")

        st.divider()
        section = st.radio("Navigation", SECTIONS, key="nav_section")
    return section


def _render_confirm_decision(state: UIState, controller: RunController) -> None:
    pending = controller.pending_decision
    st.subheader("Confirm decision")
    st.caption(f"checkpoint tool: {pending['tool_name']}")
    for name, fields in pending["verdicts"].items():
        st.write(
            f"**{name}** -- suggestion: `{fields['suggestion']}`, "
            f"confidence: {fields['confidence']}, route: {fields['route']}, "
            f"probabilities: {fields['probabilities']}"
        )

    options = options_for(pending["tool_name"])
    choice_question = CHOICE_QUESTION_FOR_TOOL.get(pending["tool_name"])
    primary = pending["verdicts"].get(choice_question) if choice_question else None
    default_index = options.index(primary["suggestion"]) if primary and primary["suggestion"] in options else 0

    result = state.last_result or {}
    if result.get("rejected_choice") and controller.decisions:
        last_decision = controller.decisions[-1]
        st.error(
            f"'{result['rejected_choice']}' was rejected: "
            f"{last_decision.get('rejection_reason') or 'deterministic guard'} -- pick again."
        )

    choice = st.radio("Your choice", options, index=default_index, key=f"confirm_{controller.run_id}")
    if st.button(
        "Confirm decision", type="primary", disabled=not can_confirm_decision(state, controller)
    ):
        confirm_decision(state, choice)
        st.rerun()


def _render_section_overview(state: UIState, data: dict) -> None:
    requirement = data["requirement"] or {}
    controller: RunController | None = data["controller"]

    st.caption(
        f"requirement_id: {requirement.get('requirement_id')} | "
        f"source_version: {requirement.get('source_version')} | "
        f"source_hash: {requirement.get('source_hash')} | "
        f"authority_file_sha256: {requirement.get('authority_file_sha256')}"
    )

    # Doc 06 Section 2 / mockup deviation #3: always show the exact bundled
    # BR01-BR06 text, never only the model's paraphrase of it.
    st.text(requirement.get("text", "(requirement not loaded)"))

    interpretation = data.get("interpretation")
    if interpretation is not None:
        with st.expander("Interpreted rules & ambiguities"):
            for rule in interpretation.get("rules", []):
                st.write(f"**{rule['rule_id']}**: {rule['meaning']}")
            for ambiguity in interpretation.get("ambiguities", []):
                label = "CRITICAL" if ambiguity.get("critical") else "non-critical"
                st.write(f"- ({label}, {ambiguity.get('status')}) {ambiguity.get('question')}")

    label = "New Analysis" if state.current_run_id is not None else "Start Analysis"
    if st.button(
        label, type="primary", key="start_or_new_analysis_btn", disabled=not can_start_analysis(state, controller)
    ):
        start_analysis(state)
        st.rerun()

    if controller is not None and controller.stage == "AWAITING_CLARIFICATION":
        st.warning("Run is paused awaiting clarification on a critical ambiguity.")
        st.text_area("Submit clarification", disabled=True, placeholder="Not implemented in this milestone.")
        st.button("Submit clarification", disabled=True)

    if controller is not None and controller.stage == "AWAITING_DECISION_CONFIRMATION" \
            and controller.pending_decision is not None \
            and controller.pending_decision.get("tool_name") == "generate_scenarios":
        _render_confirm_decision(state, controller)

    if controller is not None and controller.workflow_state in {"BLOCKED", "STOPPED"}:
        st.error(f"Run {controller.workflow_state}: {controller.halt_reason or '(no reason recorded)'}")


def _render_coverage_table(label: str, coverage: dict) -> None:
    st.caption(f"{label}: {coverage['covered']}/{coverage['required']} ({coverage['percentage']}%)")
    st.table(
        [
            {"scenario_id": r["scenario_id"], "status": r["status"], "findings": "; ".join(r["findings"])}
            for r in coverage["rows"]
        ]
    )


def _render_section_scenarios(state: UIState, data: dict) -> None:
    controller: RunController | None = data["controller"]
    if controller is None:
        st.info("No run yet.")
        return

    versions_list = data.get("versions", [])
    latest_version = versions_list[-1]["version"] if versions_list else None

    for entry in versions_list:
        version = entry["version"]
        st.markdown(f"#### Scenario set v{version}")
        if version == 1:
            st.caption("S04 is deliberately omitted from this pass (seeded omission).")

        scenario_set = entry["scenarios"]
        if scenario_set:
            for scenario in scenario_set.get("scenarios", []):
                with st.expander(f"{scenario['scenario_id']} -- {scenario['title']}"):
                    st.code(scenario.get("gherkin", ""), language="gherkin")

        grounding = entry["grounding"]
        if grounding:
            for item in grounding.get("items", []):
                st.write(
                    f"{item['scenario_id']}: {item['verdict']} (confidence {item.get('confidence')}) "
                    f"human_resolution={item.get('human_resolution')}"
                )

        # Resolve only applies to the latest version -- an older version's
        # flag is superseded once a newer scenario set exists.
        if version == latest_version:
            for scenario_id in entry["unresolved_grounding"]:
                with st.form(key=f"resolve_{version}_{scenario_id}"):
                    st.write(f"Resolve grounding flag for {scenario_id}")
                    resolution = st.radio(
                        "Resolution",
                        ["CONFIRMED_GROUNDED", "CONFIRMED_NOT_GROUNDED"],
                        key=f"res_{version}_{scenario_id}",
                    )
                    note = st.text_input("Note (optional)", key=f"note_{version}_{scenario_id}")
                    if st.form_submit_button("Resolve", type="secondary", disabled=state.worker_busy):
                        with state.lock:  # serialize against a worker thread writing the same run's files
                            controller.resolve_grounding(version, scenario_id, resolution, note or None)
                        st.rerun()

        recorded = entry["recorded_coverage"]
        effective = entry["effective_coverage"]
        if recorded is not None:
            if effective != recorded:
                col1, col2 = st.columns(2)
                with col1:
                    _render_coverage_table("Recorded (validator)", recorded)
                with col2:
                    _render_coverage_table("Effective (human-adjusted)", effective)
            else:
                _render_coverage_table("Coverage", recorded)

    if controller.stage == "AWAITING_DECISION_CONFIRMATION" and controller.pending_decision is not None \
            and controller.pending_decision.get("tool_name") == "save_scenario_set":
        _render_confirm_decision(state, controller)


def _render_section_approval(state: UIState, data: dict) -> None:
    st.info("Approval & Execution is the next milestone -- NOT RUN.")
    test_manifest = _load_json(CONFIG_DIR / "test_manifest.json") or {}
    st.table(test_manifest.get("tests", []))
    # Colors only (layout, no behavior change) -- all three stay disabled
    # placeholders this milestone. Approve/Reject's green/red come from the
    # .st-key-approve_btn/.st-key-reject_btn CSS rules in _CSS; Execute uses
    # the plain primary teal like the other main actions.
    st.button("Approve", key="approve_btn", disabled=True)
    st.button("Reject", key="reject_btn", disabled=True)
    st.button("Execute", type="primary", key="execute_btn", disabled=True)


def _render_section_results(state: UIState, data: dict) -> None:
    st.markdown("#### Decision audit")
    records = data.get("decisions") or []
    st.table(
        [
            {
                "checkpoint": r.get("checkpoint"),
                "suggestion": r.get("decider_suggestion"),
                "selected_action": r.get("selected_action"),
                "confidence": r.get("confidence"),
                "route": r.get("route"),
                "decision_source": r.get("decision_source"),
                "latency_ms": r.get("latency_ms"),
                "host_label": r.get("host_label"),
            }
            for r in records
        ]
    )
    metrics = data.get("metrics") or {"decider_calls": 0, "median_latency_ms": None, "escalated": 0}
    cols = st.columns(3)
    cols[0].metric("Decider calls", metrics["decider_calls"])
    cols[1].metric("Median latency (ms)", metrics["median_latency_ms"] or "n/a")
    cols[2].metric("Escalated", metrics["escalated"])

    st.markdown("#### Results & Report")
    st.info("Results & Report is the next milestone -- NOT RUN.")


_SECTION_RENDERERS = {
    SECTIONS[0]: _render_section_overview,
    SECTIONS[1]: _render_section_scenarios,
    SECTIONS[2]: _render_section_approval,
    SECTIONS[3]: _render_section_results,
}


def main() -> None:
    st.set_page_config(page_title="AI Test Engineer Agent", layout="wide")
    _inject_css()
    state = get_ui_state()

    # One fragment for the whole page (sidebar + the selected section): a
    # worker finishing must refresh everything on the next tick, not just
    # part of it, or the Confirm decision control / Start button's disabled
    # state would stay stale until a manual Refresh Status click.
    @st.fragment(run_every=2)
    def _app_fragment() -> None:
        data = gather_display_data(state)
        section = _render_sidebar(state, data)
        _SECTION_RENDERERS[section](state, data)

    _app_fragment()


if __name__ == "__main__":
    main()
