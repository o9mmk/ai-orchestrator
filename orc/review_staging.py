"""Ephemeral reviewer schema/result/log staging."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import ValidationError

from orc.codex_transport_schema import to_codex_transport_schema
from orc.review_schema import REVIEW_SCHEMA, validate_review
from orc.staging_files import SecureStagingDirectory

MAX_REVIEW_BYTES = 1024 * 1024


class ReviewStaging:
    """Pin known reviewer artifacts and reject unexpected cleanup entries."""

    _allowed = {
        "review.schema.json",
        "review.json",
        "stdout.log",
        "stderr.log",
    }

    def __init__(self, worktree: Path, attempt: int) -> None:
        self.path = worktree / f".orc-review-attempt-{attempt}"
        self._files = SecureStagingDirectory(worktree, self.path.name, self._allowed)
        self.schema_path = self.path / "review.schema.json"
        self.result_path = self.path / "review.json"
        self.stdout_path = self.path / "stdout.log"
        self.stderr_path = self.path / "stderr.log"

    def create(self) -> None:
        self._files.create(
            self.schema_path.name,
            to_codex_transport_schema(REVIEW_SCHEMA),
        )

    @property
    def directory_fd(self) -> int:
        return self._files.directory_fd

    def exists(self, path: Path) -> bool:
        return self._files.exists(path.name)

    def read_artifact(self, path: Path, *, max_bytes: int = MAX_REVIEW_BYTES) -> bytes:
        return self._files.read_bytes(path.name, max_bytes=max_bytes)

    def usage_bytes(self, prompt: str) -> int:
        paths = (self.result_path, self.stdout_path, self.stderr_path)
        return len(prompt.encode("utf-8")) + sum(
            self._files.size(path.name) for path in paths if self._files.exists(path.name)
        )

    def read_review(
        self,
        payload: bytes,
        *,
        expected_reviewer: str,
        expected_input_digest: str,
    ) -> tuple[dict[str, Any] | None, str, str]:
        digest = hashlib.sha256(payload).hexdigest()
        try:
            data = json.loads(payload)
            validated = validate_review(
                data,
                expected_reviewer=expected_reviewer,
                expected_input_digest=expected_input_digest,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
            return None, "json_schema_or_identity_invalid", digest
        return validated, "", digest

    def cleanup(self) -> None:
        self._files.cleanup()
