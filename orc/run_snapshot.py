"""Lease-independent read-only validation of one completed source run."""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from jsonschema import ValidationError

from orc.checkpoint import CheckpointView
from orc.errors import TamperDetected
from orc.events import EventLog
from orc.integrity import verify_integrity_snapshot
from orc.paths import StatePaths
from orc.schemas import validate_manifest


@dataclass(frozen=True)
class RunSnapshot:
    """Fully verified source-run manifest/events/checkpoint."""

    run_dir: Path
    manifest: dict[str, Any]
    events: tuple[dict[str, Any], ...]
    checkpoint: CheckpointView


def load_run_snapshot(repo_path: Path, run_id: str) -> RunSnapshot:
    """Validate source bytes without acquiring its expired lease or repairing it."""
    repo = repo_path.resolve(strict=True)
    paths = StatePaths.for_run(repo, run_id)
    run_dir = paths.run_dir
    try:
        info = run_dir.lstat()
    except FileNotFoundError as error:
        raise TamperDetected("tamper_detected: source run missing") from error
    if not stat.S_ISDIR(info.st_mode) or run_dir.is_symlink():
        raise TamperDetected("tamper_detected: source run path invalid")
    manifest_path = run_dir / "manifest.json"
    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest_data, dict):
            raise TamperDetected("tamper_detected: source manifest must be an object")
        validate_manifest(manifest_data)
    except (FileNotFoundError, json.JSONDecodeError, ValidationError) as error:
        raise TamperDetected("tamper_detected: invalid source manifest") from error
    manifest = cast(dict[str, Any], manifest_data)
    if manifest["run_id"] != run_id or Path(manifest["repo_path"]).resolve() != repo:
        raise TamperDetected("tamper_detected: source run identity mismatch")
    events = EventLog(run_dir / "events.jsonl", run_id).verify()
    checkpoint = verify_integrity_snapshot(
        events,
        manifest,
        run_dir / "checkpoint.json",
        run_dir,
        manifest["fencing_token"],
    )
    return RunSnapshot(run_dir, manifest, tuple(events), checkpoint)
