"""Promote an integrity-verified new generation into the normal Manager flow."""

from __future__ import annotations

import json

from orc.artifact_ingest import ArtifactIngestor
from orc.errors import ResumeBlocked
from orc.git_facts import collect_git_facts
from orc.manager import ManagerOutcome, ManagerService
from orc.plan_schema import validate_plan
from orc.scope import find_dirty_overlaps
from orc.state_machine import RunState
from orc.store import RunStateStore
from orc.summary import SummaryService
from orc.worktree import WorktreeManager


def continue_resumed_generation(
    store: RunStateStore,
    source_run_id: str,
    manager: ManagerService,
) -> ManagerOutcome:
    """Copy only the verified source plan, rerun safety checks, and skip reused DONE tasks."""
    source_plan_path = store.paths.run_dir.parent / source_run_id / "plan.json"
    try:
        plan_data = json.loads(source_plan_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ResumeBlocked("source_plan_missing_or_invalid") from error
    if not isinstance(plan_data, dict):
        raise ResumeBlocked("source_plan_missing_or_invalid")
    validate_plan(plan_data)
    store.transition("lease_acquired", reason="resume_preflight_started")
    store.transition("checks_passed", reason="resume_source_verified")
    ingestor = ArtifactIngestor(store)
    assessment = ingestor.assess_manager_payload("plan", "resumed-plan", plan_data)
    if not assessment.clean or assessment.clearance is None:
        raise ResumeBlocked(f"source_plan_dlp_blocked:{assessment.reason_code}")
    store.record_plan(
        plan_data,
        plan_data["final_size"],
        clearance=assessment.clearance,
    )
    store.transition("plan_valid", reason="resume_plan_reused")
    scopes = [scope for task in plan_data["tasks"] for scope in task["path_scope"]]
    facts = collect_git_facts(store.repo_path)
    overlaps = find_dirty_overlaps(tuple(scopes), facts.dirty_paths)
    worktrees = WorktreeManager(store.repo_path, store.run_id)
    if overlaps:
        store.transition("dirty_overlap", reason="dirty_overlap")
        reason = "dirty_overlap"
    elif not worktrees.probe(facts.head):
        store.transition("worktree_unavailable", reason="worktree_probe_failed")
        reason = "worktree_probe_failed"
    elif plan_data["final_size"] == "XL":
        store.transition("size_xl", reason="xl_plan_only")
        reason = "xl_plan_only"
    elif plan_data["final_size"] == "L":
        store.transition("size_l", reason="start_approval_required")
        reason = "start_approval_required"
    else:
        store.transition("size_sm", reason="resume_ready")
        reason = "resume_ready"
    if store.read_manifest()["state"] == RunState.RUNNING.value:
        outcome, _ = manager.continue_run(store)
        return outcome
    SummaryService(store, ingestor).write(reason=reason)
    return ManagerOutcome(store.run_id, store.read_manifest()["state"], reason)
