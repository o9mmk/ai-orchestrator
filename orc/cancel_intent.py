"""Repo-lock-dir cooperative cancel intent shared by short-lived terminals."""

from __future__ import annotations

import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orc.errors import TamperDetected
from orc.io_utils import canonical_json
from orc.paths import StatePaths, ensure_private_dir


@dataclass(frozen=True)
class CancelIntentResult:
    """A bounded acknowledgement that does not expose state artifact content."""

    path: Path
    created: bool


class CancelIntent:
    """One O_EXCL, 0600 cancel request tied to a run fencing generation."""

    def __init__(self, repo_path: Path, run_id: str) -> None:
        paths = StatePaths.for_run(repo_path.resolve(strict=True), run_id)
        self.run_id = run_id
        self.lock_dir = paths.lock_dir
        self.path = self.lock_dir / f"cancel-{run_id}.json"

    def request(self, fencing_token: int) -> CancelIntentResult:
        """Create once; repeated requests for the same generation are idempotent."""
        if isinstance(fencing_token, bool) or not isinstance(fencing_token, int) or fencing_token <= 0:
            raise ValueError("cancel fencing_token must be a positive integer")
        ensure_private_dir(self.lock_dir)
        payload = {
            "run_id": self.run_id,
            "fencing_token": fencing_token,
            "requested_at": time.time(),
        }
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except FileExistsError:
            existing = self._read()
            if existing["fencing_token"] != fencing_token:
                raise TamperDetected(
                    "tamper_detected: stale cancel intent blocks request"
                ) from None
            return CancelIntentResult(self.path, False)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json(payload) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        return CancelIntentResult(self.path, True)

    def pending(self, fencing_token: int) -> bool:
        """Return true only for this exact live fencing generation."""
        try:
            payload = self._read()
        except FileNotFoundError:
            return False
        return bool(payload["fencing_token"] == fencing_token)

    def clear(self, fencing_token: int) -> bool:
        """Remove only a validated intent for the expected generation."""
        try:
            payload = self._read()
        except FileNotFoundError:
            return False
        if payload["fencing_token"] != fencing_token:
            return False
        self.path.unlink()
        return True

    def _read(self) -> dict[str, Any]:
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or self.path.is_symlink():
            raise TamperDetected("tamper_detected: invalid cancel intent path")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise TamperDetected("tamper_detected: invalid cancel intent permissions")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise TamperDetected("tamper_detected: invalid cancel intent JSON") from error
        if not isinstance(payload, dict) or set(payload) != {
            "run_id",
            "fencing_token",
            "requested_at",
        }:
            raise TamperDetected("tamper_detected: invalid cancel intent fields")
        token = payload["fencing_token"]
        requested_at = payload["requested_at"]
        if (
            payload["run_id"] != self.run_id
            or isinstance(token, bool)
            or not isinstance(token, int)
            or token <= 0
            or isinstance(requested_at, bool)
            or not isinstance(requested_at, (int, float))
        ):
            raise TamperDetected("tamper_detected: invalid cancel intent values")
        return payload


def finalize_pending_cancel(store: Any) -> bool:
    """Consume an exact intent after any registered child is ledger-terminal."""
    intent = CancelIntent(store.repo_path, store.run_id)
    if not intent.pending(store.fencing_token):
        return False
    from orc.cancel import CancelService

    CancelService(store).cancel()
    intent.clear(store.fencing_token)
    return True
