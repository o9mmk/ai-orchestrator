"""Private atomic quarantine storage with safe manifests only."""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orc.io_utils import atomic_write_json
from orc.paths import ensure_private_dir, validate_identifier


def _now() -> str:
    return datetime.now(UTC).isoformat()


class QuarantineStore:
    """Persist payload and metadata at 0600 under one opaque Manager id."""

    def __init__(self, run_dir: Path, *, clock: Callable[[], str] = _now) -> None:
        self.root = run_dir / "quarantine"
        self.clock = clock

    def save(
        self,
        artifact_id: str,
        artifact_kind: str,
        payload: bytes,
        safe_metadata: dict[str, Any],
    ) -> Path:
        """Atomically create a new quarantine entry or leave no partial files."""
        safe_id = validate_identifier(artifact_id, label="artifact_id")
        target = self.root / safe_id
        if target.exists():
            raise FileExistsError("quarantine artifact id already exists")
        ensure_private_dir(self.root)
        target.mkdir(mode=0o700)
        payload_path = target / "payload"
        manifest_path = target / "manifest.json"
        try:
            _atomic_write_bytes(payload_path, payload)
            manifest = {
                **safe_metadata,
                "artifact_kind": artifact_kind,
                "artifact_id": safe_id,
                "created_at": self.clock(),
                "retention_days": 14,
                "automatic_restore": False,
                "automatic_delete": False,
            }
            atomic_write_json(manifest_path, manifest)
            payload_path.chmod(0o600)
            manifest_path.chmod(0o600)
            target.chmod(0o700)
            return target
        except (OSError, ValueError, TypeError):
            for path in (manifest_path, payload_path):
                if path.exists() and path.is_file():
                    path.unlink()
            if target.exists():
                target.rmdir()
            raise

    def remove(self, artifact_id: str) -> None:
        """Rollback one entry created by this store without following links."""
        safe_id = validate_identifier(artifact_id, label="artifact_id")
        target = self.root / safe_id
        for name in ("manifest.json", "payload"):
            path = target / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not path.is_file() or path.is_symlink() or info.st_nlink != 1:
                raise OSError("unsafe quarantine rollback entry")
            path.unlink()
        target.rmdir()
        if self.root.exists() and not any(self.root.iterdir()):
            self.root.rmdir()


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.tmp-{secrets.token_hex(8)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
