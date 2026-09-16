"""One Planner process attempt using M4 process boundaries."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from pathlib import Path

from orc.artifact_ingest import ArtifactIngestor
from orc.budget import BudgetMeter
from orc.cancel_intent import finalize_pending_cancel
from orc.dlp_models import DlpResult, StreamRedactionResult
from orc.planner_models import PlannerAttemptResult
from orc.planner_staging import PlannerStaging
from orc.store import RunStateStore
from orc.structured_process import StructuredProcessRunner
from orc.usage import measure_usage
from orc.worktree import Worktree


class PlannerAttemptRunner:
    """Run and ingest one Planner attempt without persisting invalid content."""

    def __init__(
        self,
        store: RunStateStore,
        process_runner: StructuredProcessRunner,
        budget: BudgetMeter,
        dlp_ingestor: ArtifactIngestor,
        *,
        artifact_id_factory: Callable[[], str] = lambda: secrets.token_hex(16),
    ) -> None:
        self.store = store
        self.process_runner = process_runner
        self.budget = budget
        self.dlp_ingestor = dlp_ingestor
        self.artifact_id_factory = artifact_id_factory

    def run(self, worktree: Worktree, *, attempt: int, prompt: str) -> PlannerAttemptResult:
        """Return a valid plan or only failure reason and digests."""
        staging = PlannerStaging(worktree.path, attempt)
        staging.create()
        activity_id = f"planner:{attempt}"
        self.budget.begin_activity(activity_id)
        try:
            timeout = self.store.read_manifest()["caps"]["timeouts_seconds"]["research"]
            timeout = self.budget.bound_timeout(timeout)
            process = self.process_runner.run(
                worktree,
                task_id="planner",
                role="planner",
                attempt=attempt,
                prompt=prompt,
                schema_path=staging.schema_path,
                result_path=staging.result_path,
                stdout_path=staging.stdout_path,
                stderr_path=staging.stderr_path,
                staging_dir_fd=staging.directory_fd,
                timeout=timeout,
            )
            stdout_digest, stderr_digest = staging.log_digests()
            cancel_applied = finalize_pending_cancel(self.store)
            if process.cancelled or cancel_applied:
                return PlannerAttemptResult(
                    None,
                    "cancel_requested",
                    staging.content_digest(),
                    stdout_digest,
                    stderr_digest,
                )
            self.budget.add_usage(
                measure_usage(
                    validated_usage=None,
                    byte_count=staging.usage_bytes(prompt),
                    invocation_count=1,
                )
            )
            stream_results = (
                self._ingest_stream(staging, "stdout", process.stdout_stream),
                self._ingest_stream(staging, "stderr", process.stderr_stream),
            )
            dlp_results = list(stream_results)
            if staging.exists(staging.result_path):
                dlp_results.append(
                    self.dlp_ingestor.ingest_payload(
                        staging.read_artifact(
                            staging.result_path,
                            max_bytes=self.dlp_ingestor.max_artifact_bytes,
                        ),
                        "plan",
                        self.artifact_id_factory(),
                        publish_clean=False,
                    )
                )
            if any(not result.clean for result in dlp_results):
                return PlannerAttemptResult(
                    None,
                    "dlp_blocked",
                    hashlib.sha256(b"").hexdigest(),
                    stdout_digest if stream_results[0].clean else None,
                    stderr_digest if stream_results[1].clean else None,
                )
            if process.timed_out:
                return PlannerAttemptResult(
                    None,
                    "planner_timeout",
                    staging.content_digest(),
                    stdout_digest,
                    stderr_digest,
                )
            if process.exit_code != 0:
                return PlannerAttemptResult(
                    None,
                    "planner_exit_nonzero",
                    staging.content_digest(),
                    stdout_digest,
                    stderr_digest,
                )
            plan, reason, digest = staging.read_plan()
            return PlannerAttemptResult(
                plan,
                reason or None,
                digest,
                stdout_digest,
                stderr_digest,
            )
        finally:
            self.budget.end_activity(activity_id)
            staging.cleanup()

    def _ingest_stream(
        self,
        staging: PlannerStaging,
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
