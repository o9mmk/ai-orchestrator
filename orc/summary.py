"""Bounded, DLP-cleared human summary generation."""

from __future__ import annotations

from orc.artifact_ingest import ArtifactIngestor
from orc.errors import DlpBoundaryError
from orc.store import RunStateStore


class SummaryService:
    """Render only Manager-owned state and safe counters, never raw child output."""

    def __init__(self, store: RunStateStore, ingestor: ArtifactIngestor) -> None:
        self.store = store
        self.ingestor = ingestor

    def write(
        self,
        *,
        reason: str,
        branch: str | None = None,
        merge_command: str | None = None,
    ) -> str:
        """Verify integrity, render bounded Markdown, scan exact bytes, and publish."""
        checkpoint = self.store.verify_integrity()
        manifest = self.store.read_manifest()
        events = self.store.verify_events()
        tasks = checkpoint.tasks
        skipped = any(event["type"] == "external_review_skipped" for event in events) or any(
            task.get("human_approval_required") is True for task in tasks.values()
        )
        escalated = sorted(
            task_id for task_id, task in tasks.items() if task.get("state") == "ESCALATED"
        )
        completed = sorted(
            task_id for task_id, task in tasks.items() if task.get("state") == "DONE"
        )
        lines = [
            "# orc run summary",
            "",
            f"- run_id: {self.store.run_id}",
            f"- state: {manifest['state']}",
            f"- reason: {reason}",
            f"- base_commit: {manifest['base_commit']}",
            f"- generation: {manifest['generation']}",
            f"- budget_source: {checkpoint.budget['budget_source']}",
            f"- child_invocations: {checkpoint.budget['child_invocations']}",
            f"- manager_calls: {checkpoint.budget['manager_calls']}",
            f"- completed_tasks: {','.join(completed) if completed else 'none'}",
            f"- escalated_tasks: {','.join(escalated) if escalated else 'none'}",
            f"- external_review_skipped: {str(skipped).lower()}",
            f"- partial: {str(bool(escalated)).lower()}",
            f"- integration_branch: {branch or 'none'}",
            f"- merge_command: {merge_command or 'none'}",
            "- automatic_merge: false",
            "- automatic_push: false",
            "",
        ]
        text = "\n".join(lines)
        payload = text.encode("utf-8")
        assessment = self.ingestor.assess_raw_payload(
            "summary",
            f"summary-{checkpoint.seq}",
            payload,
        )
        if not assessment.clean or assessment.clearance is None:
            raise DlpBoundaryError(f"summary_dlp_blocked:{assessment.reason_code}")
        self.store.write_summary(payload, clearance=assessment.clearance)
        return text
