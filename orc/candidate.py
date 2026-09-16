"""Manager-generated candidate diff capture and exact DLP publication."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from orc.artifact_ingest import ArtifactIngestor
from orc.errors import VerificationError
from orc.paths import validate_identifier
from orc.sandbox import scan_worktree_boundary
from orc.store import RunStateStore
from orc.worktree import Worktree


@dataclass(frozen=True)
class CandidatePatch:
    """Exact clean patch and its mechanically derived file set."""

    payload: bytes
    text: str
    changed_files: tuple[str, ...]
    path: Path


class CandidatePatchService:
    """Capture actual Git state instead of trusting a child-authored patch."""

    def __init__(self, store: RunStateStore, ingestor: ArtifactIngestor) -> None:
        self.store = store
        self.ingestor = ingestor

    def capture(
        self,
        worktree: Worktree,
        *,
        task_id: str,
        attempt: int,
    ) -> CandidatePatch:
        """Intent-add untracked files, generate one binary diff, scan, and publish it."""
        safe_task = validate_identifier(task_id, label="task_id")
        if worktree.task_id != safe_task or attempt < 1:
            raise VerificationError("candidate worktree identity mismatch")
        scan_worktree_boundary(worktree.path)
        untracked = _git_z(worktree.path, "ls-files", "--others", "--exclude-standard", "-z")
        if untracked:
            _git_checked(worktree.path, "add", "-N", "--", *untracked)
        payload = _git_bytes(
            worktree.path,
            "diff",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "HEAD",
            "--",
        )
        if not payload:
            raise VerificationError("implementer produced no candidate diff")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise VerificationError("candidate patch is not UTF-8") from error
        changed = tuple(
            sorted(_git_z(worktree.path, "diff", "--name-only", "-z", "HEAD", "--"))
        )
        assessment = self.ingestor.assess_raw_payload(
            "patch",
            f"{safe_task}-attempt-{attempt}",
            payload,
        )
        if not assessment.clean or assessment.clearance is None:
            raise VerificationError(f"candidate_patch_dlp_blocked:{assessment.reason_code}")
        path = self.store.write_task_patch(
            safe_task,
            attempt,
            payload,
            clearance=assessment.clearance,
        )
        return CandidatePatch(payload, text, changed, path)


def _git_z(cwd: Path, *args: str) -> tuple[str, ...]:
    payload = _git_bytes(cwd, *args)
    try:
        values = payload.decode("utf-8").split("\0")
    except UnicodeDecodeError as error:
        raise VerificationError("git path output was not UTF-8") from error
    return tuple(value for value in values if value)


def _git_checked(cwd: Path, *args: str) -> None:
    _git_bytes(cwd, *args)


def _git_bytes(cwd: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise VerificationError(f"candidate git command failed: {args[0]}")
    return result.stdout
