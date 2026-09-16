"""設計書§5.1の二段階Preflight orchestration。"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orc.artifact_ingest import ArtifactIngestor
from orc.budget_models import default_budget_caps, validate_budget_caps
from orc.errors import DuplicateRunId, LeaseHeld, PreflightError
from orc.git_facts import collect_git_facts
from orc.lease import LeaseManager
from orc.paths import StatePaths, validate_state_root
from orc.plan_finalizer import finalize_plan
from orc.plan_schema import validate_plan
from orc.preflight_models import (
    PreflightConfig,
    Stage1Result,
    Stage2Result,
    ToolInfo,
    build_manifest,
    probe_tool,
)
from orc.refusal import record_prelease_refusal
from orc.scope import find_dirty_overlaps
from orc.sizing import Size
from orc.state_machine import RunState
from orc.store import RunStateStore

LOGGER = logging.getLogger(__name__)


class PreflightService:
    """lease取得前後とplan後の安全検査を順序どおり実行する。"""

    def __init__(
        self,
        repo_path: Path,
        *,
        tool_probe: Callable[[str], ToolInfo] = probe_tool,
        dlp_ingestor_factory: Callable[[RunStateStore], ArtifactIngestor] = ArtifactIngestor,
    ) -> None:
        self.repo_path = repo_path.resolve(strict=True)
        self.tool_probe = tool_probe
        self.dlp_ingestor_factory = dlp_ingestor_factory

    def stage1(self, config: PreflightConfig) -> Stage1Result:
        """repo/tool/authorityを固定し、repo lease後にPLANNINGへ進める。"""
        try:
            validate_state_root(self.repo_path)
        except ValueError as error:
            raise PreflightError(str(error)) from error
        validate_budget_caps(config.caps if config.caps is not None else default_budget_caps())
        paths = StatePaths.for_run(self.repo_path, config.run_id)
        if paths.run_dir.exists():
            raise DuplicateRunId(f"duplicate_run_id: {config.run_id}")
        facts = collect_git_facts(self.repo_path)
        tool_names = dict.fromkeys((*config.required_tools, *config.optional_tools))
        tools = {name: self.tool_probe(name) for name in tool_names}
        manifest = build_manifest(self.repo_path, config, facts)
        lease_manager = LeaseManager(self.repo_path)
        try:
            lease = lease_manager.acquire(config.run_id)
        except LeaseHeld:
            events_path, checkpoint_path = record_prelease_refusal(
                self.repo_path,
                config.run_id,
                manifest,
                reason="lease_held",
            )
            return Stage1Result(
                RunState.REFUSED,
                "lease_held",
                facts,
                tools,
                None,
                None,
                events_path,
                checkpoint_path,
                config,
            )
        manifest["fencing_token"] = lease.fencing_token
        store = RunStateStore(self.repo_path, config.run_id, lease_manager, lease)
        try:
            store.initialize(manifest)
            store.transition("lease_acquired")
            store.append_event(
                "preflight1_snapshot",
                "manager",
                {
                    "head": facts.head,
                    "branch": facts.branch,
                    "dirty": list(facts.dirty_paths),
                    "tools": {
                        name: {"available": info.available, "version": info.version}
                        for name, info in tools.items()
                    },
                    "authority_sources": config.authority_sources,
                },
            )
            missing = [name for name in config.required_tools if not tools[name].available]
            if missing:
                store.transition("checks_refused", reason=f"tool_missing:{','.join(missing)}")
                state, reason = RunState.REFUSED, "tool_missing"
            else:
                store.transition("checks_passed")
                state, reason = RunState.PLANNING, "ready"
        except BaseException as primary:
            try:
                lease_manager.release(lease)
            except Exception as cleanup_error:
                LOGGER.exception("stage1 lease cleanup failed", exc_info=cleanup_error)
                primary.add_note(f"lease cleanup failed: {type(cleanup_error).__name__}")
            raise
        return Stage1Result(
            state,
            reason,
            facts,
            tools,
            store,
            lease,
            store.events_path,
            store.checkpoint_path,
            config,
        )

    def stage2(
        self,
        stage1: Stage1Result,
        plan: dict[str, Any],
        *,
        worktree_probe: Callable[[], bool],
        multiple_repos: bool = False,
        production_hint: bool = False,
        large_migration: bool = False,
    ) -> Stage2Result:
        """planを検証し、scope/dirty/command/worktree/sizingを適用する。"""
        if stage1.state is not RunState.PLANNING or stage1.store is None:
            raise PreflightError("stage2 requires a successful stage1")
        input_assessment = self.dlp_ingestor_factory(stage1.store).assess_manager_payload(
            "plan",
            secrets.token_hex(16),
            plan,
        )
        if not input_assessment.clean or input_assessment.clearance is None:
            raise PreflightError(f"plan_dlp_blocked:input:{input_assessment.reason_code}")
        validate_plan(plan)
        current = collect_git_facts(self.repo_path)
        if current.head != stage1.git_facts.head:
            raise PreflightError("HEAD changed during preflight")
        finalized = finalize_plan(
            self.repo_path,
            plan,
            invocation_cap=stage1.store.read_manifest()["caps"]["child_invocations_hard"],
            multiple_repos=multiple_repos,
            production_hint=production_hint,
            large_migration=large_migration,
        )
        resolution = finalized.resolution
        overlaps = find_dirty_overlaps(resolution.resolved, current.dirty_paths)
        final_plan, decision = finalized.plan, finalized.decision
        recorded_plan = stage1.store.read_plan()
        if recorded_plan is None:
            assessment = self.dlp_ingestor_factory(stage1.store).assess_manager_payload(
                "plan",
                secrets.token_hex(16),
                final_plan,
            )
            if not assessment.clean or assessment.clearance is None:
                raise PreflightError(f"plan_dlp_blocked:{assessment.reason_code}")
            stage1.store.record_plan(
                final_plan,
                decision.final_size.value,
                clearance=assessment.clearance,
            )
        elif recorded_plan != final_plan:
            raise PreflightError("recorded Planner plan does not match Stage 2 input")
        stage1.store.append_event(
            "preflight2_snapshot",
            "manager",
            {
                "normalized_scopes": list(resolution.resolved),
                "unresolved_scopes": list(resolution.unresolved),
                "read_scopes": list(finalized.read_resolution.resolved),
                "unresolved_read_scopes": list(finalized.read_resolution.unresolved),
                "dirty_overlaps": list(overlaps),
                "dirty_warning": list(current.dirty_paths if not overlaps else ()),
                "command_risks": [risk.value for risk in decision.command_risks],
                "escalation_flags": list(decision.escalation_flags),
                "xl_flags": list(decision.xl_flags),
                "final_size": decision.final_size.value,
            },
        )
        stage1.store.write_checkpoint(run_state=RunState.PLANNING.value)
        stage1.store.transition("plan_valid")
        if overlaps:
            state = stage1.store.transition("dirty_overlap")
            reason = "dirty_overlap"
        elif not worktree_probe():
            state = stage1.store.transition("worktree_unavailable")
            reason = "worktree_probe_failed"
        elif decision.final_size is Size.XL:
            state = stage1.store.transition("size_xl")
            reason = "xl_plan_only"
        elif decision.final_size is Size.L:
            state = stage1.store.transition("size_l")
            reason = "start_approval_required"
        else:
            state = stage1.store.transition("size_sm")
            reason = "ready"
        return Stage2Result(
            state=state,
            reason=reason,
            final_size=decision.final_size.value,
            normalized_scopes=resolution.resolved,
            unresolved_scopes=resolution.unresolved,
            dirty_overlaps=overlaps,
            dirty_warning=() if overlaps else current.dirty_paths,
            command_risks=tuple(risk.value for risk in decision.command_risks),
            plan=final_plan,
        )
