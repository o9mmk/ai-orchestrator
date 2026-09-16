"""Ephemeral worktree-local schema/result/log staging for one attempt."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import ValidationError

from orc.codex_transport_schema import to_codex_transport_schema
from orc.result_schema import RESULT_SCHEMA, validate_result
from orc.staging_files import SecureStagingDirectory

MAX_RESULT_BYTES = 1024 * 1024


class AttemptStaging:
    """Create only known files and reject unexpected cleanup entries."""

    _allowed = {
        "result.schema.json",
        "result.json",
        "patch.diff",
        "transcript.log",
        "findings.json",
        "stdout.log",
        "stderr.log",
    }

    def __init__(self, worktree: Path, attempt: int) -> None:
        self.path = worktree / f".orc-attempt-{attempt}"
        self._files = SecureStagingDirectory(worktree, self.path.name, self._allowed)
        self.schema_path = self.path / "result.schema.json"
        self.result_path = self.path / "result.json"
        self.patch_path = self.path / "patch.diff"
        self.transcript_path = self.path / "transcript.log"
        self.findings_path = self.path / "findings.json"
        self.stdout_path = self.path / "stdout.log"
        self.stderr_path = self.path / "stderr.log"

    def create(self) -> None:
        """Create a fresh 0700 staging directory and schema file."""
        self._files.create(
            self.schema_path.name,
            to_codex_transport_schema(RESULT_SCHEMA),
        )

    @property
    def directory_fd(self) -> int:
        """Return the descriptor that pins the Manager-created staging inode."""
        return self._files.directory_fd

    def exists(self, path: Path) -> bool:
        """Check an allowlisted child on the pinned staging inode."""
        return self._files.exists(path.name)

    def read_artifact(self, path: Path, *, max_bytes: int) -> bytes:
        """Read an allowlisted artifact without resolving the child-controlled path."""
        return self._files.read_bytes(path.name, max_bytes=max_bytes)

    def log_digests(self) -> tuple[str, str]:
        """Return only stdout/stderr digests to Manager-facing code."""
        return self._files.digest(self.stdout_path.name), self._files.digest(
            self.stderr_path.name
        )

    def usage_bytes(self, prompt: str) -> int:
        """Return byte proxy input without parsing JSONL usage fields."""
        paths = (
            self.result_path,
            self.patch_path,
            self.transcript_path,
            self.findings_path,
            self.stdout_path,
            self.stderr_path,
        )
        return len(prompt.encode("utf-8")) + sum(
            self._files.size(path.name) for path in paths if self._files.exists(path.name)
        )

    def read_result(
        self,
        *,
        task_id: str,
        role: str,
        attempt: int,
    ) -> tuple[dict[str, Any] | None, str, str]:
        """Parse and validate without returning invalid content."""
        if not self._files.exists(self.result_path.name):
            return None, "missing_result", hashlib.sha256(b"").hexdigest()
        payload = self._files.read_bytes(self.result_path.name, max_bytes=MAX_RESULT_BYTES)
        digest = hashlib.sha256(payload).hexdigest()
        try:
            data = json.loads(payload)
            validated = validate_result(
                data,
                expected_task_id=task_id,
                expected_role=role,
                expected_attempt=attempt,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
            return None, "json_or_schema_invalid", digest
        return validated, "", digest

    def cleanup(self) -> None:
        """Remove known ephemeral files and fail on any unexpected entry."""
        self._files.cleanup()
