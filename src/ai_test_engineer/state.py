"""Minimal per-run controller: workflow state, single-use flags, decision audit log.

No Strands/decider_gate imports -- pure data + JSON persistence, independently
testable. checkpoints.py and orchestration.py hold a reference to one
RunController per run and drive it; nothing outside this module is ever
allowed to flip a single-use flag directly (there is no public setter for
them -- only the use_*() methods, which flip once and report whether they
could).

Known scope limit: persistence here is business state only (workflow_state,
stage, flags, decisions, pending_decision) -- it does NOT capture the live
Strands Agent/_interrupt_state needed to resume a paused run. That lives only
in orchestration.py's in-process _ACTIVE_RUNS dict. A process restart between
a pause and resume_pending_decision() loses the ability to resume; this
milestone does not solve that (no SessionManager wiring), only documents it.
"""

from __future__ import annotations

import json
from pathlib import Path

WORKFLOW_STATES = {
    "PREPARING",
    "AWAITING_CLARIFICATION",
    "REMEDIATING",
    "AWAITING_APPROVAL",
    "APPROVED",
    "EXECUTING",
    "ANALYZING_RESULTS",
    "REPORTING",
    "COMPLETED",
    "REJECTED",
    "BLOCKED",
    "STOPPED",
}
# This milestone only ever drives PREPARING / AWAITING_CLARIFICATION / BLOCKED
# / STOPPED. The rest are later milestones' stages -- listed here so the
# assertion in set_workflow_state has the full, correct vocabulary from docs/05.


class RunController:
    """Owns one run's workflow state, single-use flags, and decision/evidence audit trail."""

    def __init__(self, run_id: str, run_dir: Path) -> None:
        self.run_id = run_id
        self.run_dir = run_dir
        self.workflow_state = "PREPARING"
        self.stage: str | None = None
        self.busy = True
        self._remediation_used = False
        self._execution_started = False
        self._investigation_used = False
        self._halted = False
        self._halt_reason: str | None = None
        self.pending_decision: dict | None = None
        self.decisions: list[dict] = []
        self._tool_stage: str | None = None
        self._allowed_tools: frozenset[str] | None = None
        self.stage_violations: list[dict] = []
        self.tool_calls: list[dict] = []
        self._save_state()

    # -- single-use flags: only the controller itself flips them ------------

    def use_remediation(self) -> bool:
        if self._remediation_used:
            return False
        self._remediation_used = True
        self._save_state()
        return True

    def use_execution(self) -> bool:
        if self._execution_started:
            return False
        self._execution_started = True
        self._save_state()
        return True

    def use_investigation(self) -> bool:
        if self._investigation_used:
            return False
        self._investigation_used = True
        self._save_state()
        return True

    @property
    def remediation_used(self) -> bool:
        return self._remediation_used

    @property
    def execution_started(self) -> bool:
        return self._execution_started

    @property
    def investigation_used(self) -> bool:
        return self._investigation_used

    # -- controller halt: deny every tool call until cleared -----------------
    #
    # Set the instant a decision schedules remediation/clarification/block, so
    # a model that keeps acting within the same agent() turn (before the
    # orchestration code driving that decision regains control) is denied
    # without ever reaching Decider again. _drive_recheck clears it to start
    # the fresh turn remediation runs in. Checked by checkpoints.HaltGuard,
    # registered ahead of DeciderGuardrail in the Agent's interventions list.

    def set_halt(self, reason: str) -> None:
        self._halted = True
        self._halt_reason = reason
        self._save_state()

    def clear_halt(self) -> None:
        self._halted = False
        self._halt_reason = None
        self._save_state()

    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str | None:
        return self._halt_reason

    # -- per-stage tool gating ------------------------------------------------
    #
    # docs/03 Section 3: "Tools available to the agent are limited per stage
    # by the controller." orchestration.py calls set_stage() before every
    # agent() call; checkpoints.StageGuard (registered ahead of
    # DeciderGuardrail) denies any tool not in allowed_tools before Decider
    # is ever consulted, so a model that calls a tool early never reaches
    # the real gate for it.

    def set_stage(self, stage: str, allowed_tools: "frozenset[str] | set[str]") -> None:
        self._tool_stage = stage
        self._allowed_tools = frozenset(allowed_tools)
        self._save_state()

    @property
    def tool_stage(self) -> str | None:
        return self._tool_stage

    @property
    def allowed_tools(self) -> frozenset[str] | None:
        return self._allowed_tools

    def record_stage_violation(self, tool_name: str, reason: str) -> None:
        self.stage_violations.append(
            {"stage": self._tool_stage, "tool_name": tool_name, "reason": reason}
        )
        (self.run_dir / "stage_violations.json").write_text(json.dumps(self.stage_violations, indent=2))

    def record_tool_call(
        self, tool_name: str, tool_use_id: str | None, stage: str | None, guard: str, outcome: str
    ) -> None:
        """One row per before_tool_call pass (every registered tool, gated or
        not): which guard made the final decision and what it was. Lets a run
        be fully reconstructed alongside decisions.json/stage_violations.json."""
        self.tool_calls.append(
            {
                "tool_name": tool_name,
                "tool_use_id": tool_use_id,
                "stage": stage,
                "guard": guard,
                "outcome": outcome,
            }
        )
        (self.run_dir / "tool_calls.json").write_text(json.dumps(self.tool_calls, indent=2))

    # -- state transitions / audit -------------------------------------------

    def set_workflow_state(self, workflow_state: str, *, stage: str | None = None, busy: bool = True) -> None:
        assert workflow_state in WORKFLOW_STATES, f"unknown workflow_state {workflow_state!r}"
        self.workflow_state = workflow_state
        self.stage = stage
        self.busy = busy
        self._save_state()

    def record_decision(self, record: dict) -> None:
        self.decisions.append(record)
        (self.run_dir / "decisions.json").write_text(json.dumps(self.decisions, indent=2))

    def record_grounding(self, scenario_set_version: int, items: list[dict]) -> None:
        path = self.run_dir / f"grounding_v{scenario_set_version}.json"
        path.write_text(json.dumps({"scenario_set_version": scenario_set_version, "items": items}, indent=2))

    def set_pending_decision(self, pending: dict | None) -> None:
        self.pending_decision = pending
        self._save_state()

    def save_evidence(self, name: str, data: dict) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / name).write_text(json.dumps(data, indent=2))

    def _save_state(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "state.json").write_text(
            json.dumps(
                {
                    "run_id": self.run_id,
                    "workflow_state": self.workflow_state,
                    "stage": self.stage,
                    "tool_stage": self._tool_stage,
                    "allowed_tools": sorted(self._allowed_tools) if self._allowed_tools else None,
                    "busy": self.busy,
                    "flags": {
                        "remediation_used": self._remediation_used,
                        "execution_started": self._execution_started,
                        "investigation_used": self._investigation_used,
                    },
                    "pending_decision": self.pending_decision,
                    "halted": self._halted,
                    "halt_reason": self._halt_reason,
                },
                indent=2,
            )
        )
