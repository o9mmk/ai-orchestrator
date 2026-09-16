"""Ephemeral worktree-local PLAN_SCHEMA staging for Planner attempts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import ValidationError

from orc.codex_transport_schema import to_codex_transport_schema
from orc.plan_schema import PLAN_SCHEMA, validate_plan
from orc.staging_files import SecureStagingDirectory

MAX_PLAN_BYTES = 1024 * 1024


class PlannerStaging:
    """Keep invalid Planner content ephemeral and expose only digest/reason."""

    _allowed = {"plan.schema.json", "plan.json", "stdout.log", "stderr.log"}

    def __init__(self, worktree: Path, attempt: int) -> None:
        self.path = worktree / f".orc-planner-attempt-{attempt}"
        self._files = SecureStagingDirectory(worktree, self.path.name, self._allowed)
        self.schema_path = self.path / "plan.schema.json"
        self.result_path = self.path / "plan.json"
        self.stdout_path = self.path / "stdout.log"
        self.stderr_path = self.path / "stderr.log"

    def create(self) -> None:
        """Create a fresh private staging directory and Planner schema."""
        self._files.create(
            self.schema_path.name,
            to_codex_transport_schema(PLAN_SCHEMA),
        )

    @property
    def directory_fd(self) -> int:
        """Return the descriptor that pins the Manager-created staging inode."""
        return self._files.directory_fd

    def exists(self, path: Path) -> bool:
        """Check an allowlisted child on the pinned staging inode."""
        return self._files.exists(path.name)

    def read_artifact(self, path: Path, *, max_bytes: int) -> bytes:
        """Read an allowlisted artifact without resolving a replacement path."""
        return self._files.read_bytes(path.name, max_bytes=max_bytes)

    def read_plan(self) -> tuple[dict[str, Any] | None, str, str]:
        """Return valid JSON only; invalid content becomes reason plus SHA-256."""
        if not self._files.exists(self.result_path.name):
            return None, "missing_plan", hashlib.sha256(b"").hexdigest()
        payload = self._files.read_bytes(self.result_path.name, max_bytes=MAX_PLAN_BYTES)
        digest = hashlib.sha256(payload).hexdigest()
        try:
            data = json.loads(payload)
            if not isinstance(data, dict):
                raise ValidationError("plan must be an object")
            validate_plan(data)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
            return None, "json_or_schema_invalid", digest
        return data, "", digest

    def log_digests(self) -> tuple[str, str]:
        """Return stdout/stderr digests without exposing either body."""
        return self._files.digest(self.stdout_path.name), self._files.digest(
            self.stderr_path.name
        )

    def content_digest(self) -> str:
        """Digest the plan body without parsing or retaining it."""
        if not self._files.exists(self.result_path.name):
            return hashlib.sha256(b"").hexdigest()
        return self._files.digest(self.result_path.name)

    def usage_bytes(self, prompt: str) -> int:
        """Return an explicit byte proxy when structured usage is unavailable."""
        paths = (self.result_path, self.stdout_path, self.stderr_path)
        return len(prompt.encode("utf-8")) + sum(
            self._files.size(path.name) for path in paths if self._files.exists(path.name)
        )

    def cleanup(self) -> None:
        """Delete known ephemeral files and fail on unexpected child output."""
        self._files.cleanup()
