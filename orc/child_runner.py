"""One-shot child result ingestion built on the shared structured runner."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orc.artifact_ingest import ArtifactIngestor
from orc.attempt_staging import AttemptStaging
from orc.budget import BudgetLimitReached, BudgetMeter
from orc.cancel_intent import finalize_pending_cancel
from orc.child_models import ChildAttemptOutcome
from orc.child_request import ROLE_TIMEOUT_KEYS, validate_child_request
from orc.codex_adapter import CodexExecAdapter
from orc.dlp_models import DlpResult, DlpStatus, StreamRedactionResult
from orc.errors import ChildExecutionError
from orc.store import RunStateStore
from orc.structured_process import StructuredProcessOutcome, StructuredProcessRunner
from orc.usage import measure_usage
from orc.worktree import Worktree


class ChildRunner:
    """Run schema-bound task children with optional M5 budget accounting."""

    def __init__(
        self,
        store: RunStateStore,
        adapter: CodexExecAdapter,
        *,
        budget: BudgetMeter | None = None,
        term_grace_seconds: float = 5.0,
        poll_interval: float = 0.05,
        dlp_ingestor: ArtifactIngestor | None = None,
        stream_overlap_chars: int = 64 * 1024,
        stream_output_limit_bytes: int = 4 * 1024 * 1024,
        artifact_id_factory: Callable[[], str] = lambda: secrets.token_hex(16),
    ) -> None:
        self.store = store
        self.budget = budget
        self.dlp_ingestor = dlp_ingestor or ArtifactIngestor(store)
        self.artifact_id_factory = artifact_id_factory
        self.process_runner = StructuredProcessRunner(
            store,
            adapter,
            term_grace_seconds=term_grace_seconds,
            poll_interval=poll_interval,
            matcher=self.dlp_ingestor.matcher,
            stream_overlap_chars=stream_overlap_chars,
            stream_output_limit_bytes=stream_output_limit_bytes,
        )
        self.ledger = self.process_runner.ledger

    def run(
        self,
        worktree: Worktree,
        *,
        task_id: str,
        role: str,
        attempt: int,
        prompt: str,
    ) -> ChildAttemptOutcome:
        """Run one child and expose only a validated result or bounded evidence."""
        root = validate_child_request(
            self.store,
            worktree,
            task_id=task_id,
            role=role,
            attempt=attempt,
            prompt=prompt,
        )
        manifest = self.store.read_manifest()
        timeout = manifest["caps"]["timeouts_seconds"][ROLE_TIMEOUT_KEYS[role]]
        hard_attempts = manifest["caps"]["task_attempts_hard"]
        if attempt > hard_attempts:
            raise ChildExecutionError("attempt exceeds manifest task_attempts_hard")
        task_state = "ESCALATED" if attempt >= hard_attempts else "RUNNING"
        staging = AttemptStaging(root, attempt)
        staging.create()
        activity_id = f"child:{task_id}:{attempt}"
        activity_started = False
        process_completed = False
        try:
            if self.budget is not None:
                self.budget.reserve_child()
                self.budget.begin_activity(activity_id)
                activity_started = True
                try:
                    timeout = self.budget.bound_timeout(timeout)
                except BudgetLimitReached as limit:
                    self.budget.halt_before_spawn(limit)
                    raise
            process = self.process_runner.run(
                worktree,
                task_id=task_id,
                role=role,
                attempt=attempt,
                prompt=prompt,
                schema_path=staging.schema_path,
                result_path=staging.result_path,
                stdout_path=staging.stdout_path,
                stderr_path=staging.stderr_path,
                staging_dir_fd=staging.directory_fd,
                timeout=timeout,
            )
            process_completed = True
            stdout_digest, stderr_digest = staging.log_digests()
            cancel_applied = finalize_pending_cancel(self.store)
            if process.cancelled or cancel_applied:
                return self._outcome(
                    "CANCELLED",
                    task_id,
                    role,
                    attempt,
                    process,
                    "ABORTED",
                    None,
                    stdout_digest,
                    stderr_digest,
                )
            if self.budget is not None:
                usage = measure_usage(
                    validated_usage=None,
                    byte_count=staging.usage_bytes(prompt),
                    invocation_count=1,
                )
                self.budget.add_usage(usage)
            stream_results = self._ingest_streams(staging, process)
            dlp_results = list(stream_results)
            structured_results = self._ingest_structured_artifacts(staging)
            dlp_results.extend(structured_results)
            blocked = [result for result in dlp_results if not result.clean]
            if blocked:
                return self._outcome(
                    "DLP_BLOCKED",
                    task_id,
                    role,
                    attempt,
                    process,
                    "ESCALATED",
                    None,
                    stdout_digest if stream_results[0].clean else None,
                    stderr_digest if stream_results[1].clean else None,
                    dlp_status=self._blocked_status(blocked),
                )
            if process.timed_out:
                return self._outcome(
                    "TIMED_OUT",
                    task_id,
                    role,
                    attempt,
                    process,
                    task_state,
                    None,
                    stdout_digest,
                    stderr_digest,
                )
            if process.exit_code != 0:
                return self._outcome(
                    "CHILD_FAILED",
                    task_id,
                    role,
                    attempt,
                    process,
                    task_state,
                    None,
                    stdout_digest,
                    stderr_digest,
                )
            result, invalid_reason, content_digest = staging.read_result(
                task_id=task_id,
                role=role,
                attempt=attempt,
            )
            if result is None:
                self.store.record_schema_invalid(
                    task_id,
                    attempt,
                    reason=invalid_reason,
                    content_digest=content_digest,
                    task_state=task_state,
                )
                return self._outcome(
                    "SCHEMA_INVALID",
                    task_id,
                    role,
                    attempt,
                    process,
                    task_state,
                    None,
                    stdout_digest,
                    stderr_digest,
                )
            canonical_dlp = self.dlp_ingestor.assess_manager_payload(
                "result",
                self.artifact_id_factory(),
                result,
            )
            if not canonical_dlp.clean or canonical_dlp.clearance is None:
                return self._outcome(
                    "DLP_BLOCKED",
                    task_id,
                    role,
                    attempt,
                    process,
                    "ESCALATED",
                    None,
                    stdout_digest,
                    stderr_digest,
                    dlp_status=canonical_dlp.result.status.value,
                )
            self.store.write_result_report(
                task_id,
                role,
                attempt,
                result,
                clearance=canonical_dlp.clearance,
            )
            return ChildAttemptOutcome(
                "VALIDATED",
                task_id,
                role,
                attempt,
                process.exit_code,
                False,
                None,
                False,
                False,
                "VERIFYING",
                result,
                stdout_digest,
                stderr_digest,
                process.pid,
                process.pgid,
                DlpStatus.CLEAN.value,
            )
        finally:
            if activity_started and self.budget is not None:
                self.budget.end_activity(activity_id)
                if process_completed and self.budget.snapshot().hard_reached:
                    self.budget.enforce_hard(self.ledger)
            staging.cleanup()

    @staticmethod
    def _outcome(
        status: str,
        task_id: str,
        role: str,
        attempt: int,
        process: StructuredProcessOutcome,
        task_state: str,
        result: dict[str, Any] | None,
        stdout_digest: str | None,
        stderr_digest: str | None,
        *,
        dlp_status: str = "CLEAN",
    ) -> ChildAttemptOutcome:
        return ChildAttemptOutcome(
            status,
            task_id,
            role,
            attempt,
            process.exit_code,
            process.timed_out,
            process.termination_signal,
            process.forced_kill,
            True,
            task_state,
            result,
            stdout_digest,
            stderr_digest,
            process.pid,
            process.pgid,
            dlp_status,
        )

    def _ingest_streams(
        self,
        staging: AttemptStaging,
        process: StructuredProcessOutcome,
    ) -> tuple[DlpResult, DlpResult]:
        return (
            self._ingest_stream(staging, "stdout", process.stdout_stream),
            self._ingest_stream(staging, "stderr", process.stderr_stream),
        )

    def _ingest_stream(
        self,
        staging: AttemptStaging,
        kind: str,
        stream: StreamRedactionResult,
    ) -> DlpResult:
        force_reason = "OUTPUT_LIMIT_EXCEEDED" if stream.limit_exceeded else None
        path = Path(f"{kind}.log")
        return self.dlp_ingestor.ingest_payload(
            staging.read_artifact(path, max_bytes=self.dlp_ingestor.max_artifact_bytes),
            kind,
            self.artifact_id_factory(),
            pre_detected=dict(stream.category_counts),
            force_reason=force_reason,
        )

    def _ingest_structured_artifacts(self, staging: AttemptStaging) -> list[DlpResult]:
        results: list[DlpResult] = []
        candidates = (
            (staging.patch_path, "patch", True),
            (staging.transcript_path, "transcript", True),
            (staging.findings_path, "findings", True),
            (staging.result_path, "result", False),
        )
        for path, kind, publish_clean in candidates:
            if not staging.exists(path):
                continue
            results.append(
                self.dlp_ingestor.ingest_payload(
                    staging.read_artifact(
                        path, max_bytes=self.dlp_ingestor.max_artifact_bytes
                    ),
                    kind,
                    self.artifact_id_factory(),
                    publish_clean=publish_clean,
                )
            )
        return results

    @staticmethod
    def _blocked_status(results: list[DlpResult]) -> str:
        if any(result.status is DlpStatus.SCAN_FAILED for result in results):
            return DlpStatus.SCAN_FAILED.value
        return DlpStatus.QUARANTINED.value
