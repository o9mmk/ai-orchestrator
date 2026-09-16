"""Bounded one-shot Planner orchestration for M5."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Sequence
from typing import Any

from orc.artifact_ingest import ArtifactIngestor
from orc.budget import BudgetLimitReached, BudgetMeter
from orc.codex_adapter import CodexExecAdapter
from orc.errors import PreflightError
from orc.manager_context import (
    ContextLimitError,
    ManagerContextBuilder,
    PinnedContext,
    PinnedContextTooLarge,
    ValidatedVariable,
)
from orc.plan_finalizer import finalize_plan
from orc.planner_attempt import PlannerAttemptRunner
from orc.planner_models import PlannerAttemptResult, PlannerOutcome
from orc.state_machine import RunState
from orc.store import RunStateStore
from orc.structured_process import StructuredProcessRunner
from orc.worktree import Worktree

PLANNER_ATTEMPTS_HARD = 2


class PlannerService:
    """Generate at most two plans and persist only a safe finalized plan."""

    def __init__(
        self,
        store: RunStateStore,
        adapter: CodexExecAdapter,
        budget: BudgetMeter,
        context_builder: ManagerContextBuilder,
        *,
        term_grace_seconds: float = 5.0,
        poll_interval: float = 0.05,
        dlp_ingestor: ArtifactIngestor | None = None,
        stream_overlap_chars: int = 64 * 1024,
        stream_output_limit_bytes: int = 4 * 1024 * 1024,
        artifact_id_factory: Callable[[], str] = lambda: secrets.token_hex(16),
    ) -> None:
        self.store = store
        self.budget = budget
        self.context_builder = context_builder
        self.dlp_ingestor = dlp_ingestor or ArtifactIngestor(store)
        self.artifact_id_factory = artifact_id_factory
        process_runner = StructuredProcessRunner(
            store,
            adapter,
            term_grace_seconds=term_grace_seconds,
            poll_interval=poll_interval,
            matcher=self.dlp_ingestor.matcher,
            stream_overlap_chars=stream_overlap_chars,
            stream_output_limit_bytes=stream_output_limit_bytes,
        )
        self.attempt_runner = PlannerAttemptRunner(
            store,
            process_runner,
            budget,
            self.dlp_ingestor,
            artifact_id_factory=artifact_id_factory,
        )
        self.ledger = process_runner.ledger

    def generate(
        self,
        worktree: Worktree,
        pinned: PinnedContext,
        *,
        model_window_tokens: int | None,
        variables: Sequence[ValidatedVariable] = (),
    ) -> PlannerOutcome:
        """Build bounded context, run Planner, and revalidate with safe sizing."""
        manifest = self.store.read_manifest()
        if manifest["state"] != RunState.PLANNING.value:
            raise ValueError("Planner requires a PLANNING run")
        pinned_dlp = self.dlp_ingestor.assess_manager_payload(
            "summary",
            self.artifact_id_factory(),
            self._pinned_payload(pinned),
        )
        if not pinned_dlp.clean:
            self.store.append_event(
                "manager_context_dlp_blocked",
                "manager",
                pinned_dlp.to_event_data(),
            )
            self.store.write_checkpoint(run_state=RunState.PLANNING.value)
            return PlannerOutcome(
                "DLP_BLOCKED",
                0,
                None,
                None,
                pinned_dlp.reason_code,
                None,
            )
        try:
            context = self.context_builder.build(
                pinned,
                variables,
                manifest["caps"]["manager_input_tokens_hard"],
                model_window_tokens,
            )
        except PinnedContextTooLarge:
            return self._refuse_context("goal_too_large", "goal_too_large")
        except ContextLimitError as error:
            reason = str(error)
            event = "context_window_unknown" if reason == "model_window_unknown" else "context_too_large"
            return self._refuse_context(reason, event)
        self.store.append_event(
            "manager_context_built",
            "manager",
            {
                "prompt_digest": context.prompt_digest,
                "estimated_tokens": context.estimate.tokens,
                "estimator": context.estimate.estimator,
                "budget_source": context.estimate.source.value,
                "dropped_count": context.dropped_count,
                "dropped_digests": list(context.dropped_digests),
            },
        )
        for attempt in range(1, PLANNER_ATTEMPTS_HARD + 1):
            try:
                self.budget.reserve_planner()
            except BudgetLimitReached as limit:
                self.budget.halt_before_spawn(limit)
                return PlannerOutcome(
                    "HALTED",
                    attempt - 1,
                    None,
                    None,
                    limit.reason,
                    context.prompt_digest,
                )
            try:
                result = self.attempt_runner.run(
                    worktree,
                    attempt=attempt,
                    prompt=context.prompt,
                )
            except BudgetLimitReached as limit:
                self.budget.halt_before_spawn(limit)
                return PlannerOutcome(
                    "HALTED",
                    attempt - 1,
                    None,
                    None,
                    limit.reason,
                    context.prompt_digest,
                )
            finalized_plan = None
            final_size = None
            if result.reason == "cancel_requested":
                return PlannerOutcome(
                    "CANCELLED",
                    attempt,
                    None,
                    None,
                    "cancel_requested",
                    context.prompt_digest,
                )
            if result.reason == "dlp_blocked":
                self._record_attempt_failure(attempt, result)
                return PlannerOutcome(
                    "DLP_BLOCKED",
                    attempt,
                    None,
                    None,
                    "dlp_blocked",
                    context.prompt_digest,
                )
            if result.plan is not None:
                try:
                    finalized = finalize_plan(
                        self.store.repo_path,
                        result.plan,
                        invocation_cap=manifest["caps"]["child_invocations_hard"],
                    )
                except (PreflightError, ValueError):
                    self._record_failure(
                        attempt,
                        "plan_safety_invalid",
                        result.content_digest,
                        stdout_digest=result.stdout_digest,
                        stderr_digest=result.stderr_digest,
                    )
                else:
                    canonical_dlp = self.dlp_ingestor.assess_manager_payload(
                        "plan",
                        self.artifact_id_factory(),
                        finalized.plan,
                    )
                    if not canonical_dlp.clean or canonical_dlp.clearance is None:
                        self._record_failure(
                            attempt,
                            canonical_dlp.reason_code,
                            result.content_digest,
                            stdout_digest=result.stdout_digest,
                            stderr_digest=result.stderr_digest,
                        )
                        return PlannerOutcome(
                            "DLP_BLOCKED",
                            attempt,
                            None,
                            None,
                            canonical_dlp.reason_code,
                            context.prompt_digest,
                        )
                    self.store.record_plan(
                        finalized.plan,
                        finalized.decision.final_size.value,
                        clearance=canonical_dlp.clearance,
                    )
                    finalized_plan = finalized.plan
                    final_size = finalized.decision.final_size.value
            else:
                self._record_attempt_failure(attempt, result)
            halted = self._halt_after_attempt(
                attempt,
                context.prompt_digest,
                finalized_plan,
                final_size,
            )
            if halted is not None:
                return halted
            if finalized_plan is not None:
                return PlannerOutcome(
                    "VALIDATED",
                    attempt,
                    finalized_plan,
                    final_size,
                    None,
                    context.prompt_digest,
                )
            if attempt < PLANNER_ATTEMPTS_HARD:
                self.store.transition("plan_retry", reason="planner_attempt_failed")
        self.store.transition("plan_failed", reason="plan_failed")
        return PlannerOutcome(
            "PLAN_FAILED",
            PLANNER_ATTEMPTS_HARD,
            None,
            None,
            "plan_failed",
            context.prompt_digest,
        )

    def _record_failure(
        self,
        attempt: int,
        reason: str,
        content_digest: str,
        *,
        stdout_digest: str | None = None,
        stderr_digest: str | None = None,
    ) -> None:
        self.store.append_event(
            "planner_attempt_failed",
            "manager",
            {
                "attempt": attempt,
                "reason": reason,
                "content_digest": content_digest,
                "stdout_digest": stdout_digest,
                "stderr_digest": stderr_digest,
            },
        )

    def _record_attempt_failure(self, attempt: int, result: PlannerAttemptResult) -> None:
        if result.reason is None:
            raise ValueError("invalid Planner attempt must include a bounded reason")
        self._record_failure(
            attempt,
            result.reason,
            result.content_digest,
            stdout_digest=result.stdout_digest,
            stderr_digest=result.stderr_digest,
        )

    def _refuse_context(self, reason: str, transition_event: str) -> PlannerOutcome:
        self.store.append_event(
            "manager_context_refused",
            "manager",
            {"reason": reason, "budget_consumed": 0},
        )
        self.store.transition(transition_event, reason=reason)
        return PlannerOutcome("REFUSED", 0, None, None, reason, None)

    def _halt_after_attempt(
        self,
        attempt: int,
        prompt_digest: str,
        plan: dict[str, Any] | None,
        final_size: str | None,
    ) -> PlannerOutcome | None:
        snapshot = self.budget.snapshot()
        if snapshot.hard_reached:
            self.budget.enforce_hard(self.ledger)
            reason = "budget_hard"
        elif snapshot.soft_reached:
            if not self.budget.enforce_soft(pending_tasks=True, running_children=0):
                return None
            reason = "budget_soft"
        else:
            return None
        return PlannerOutcome(
            "HALTED",
            attempt,
            plan,
            final_size,
            reason,
            prompt_digest,
        )

    @staticmethod
    def _pinned_payload(pinned: PinnedContext) -> dict[str, Any]:
        return {
            "role_contract": pinned.role_contract,
            "goal": pinned.goal,
            "acceptance_criteria": list(pinned.acceptance_criteria),
            "forbidden": list(pinned.forbidden),
            "authority_sources": list(pinned.authority_sources),
            "base_commit": pinned.base_commit,
            "path_scope": list(pinned.path_scope),
            "safety_policy_version": pinned.safety_policy_version,
            "unresolved_blockers": list(pinned.unresolved_blockers),
        }
