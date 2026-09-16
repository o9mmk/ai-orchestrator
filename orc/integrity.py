"""checkpoint遅延replayとresume前整合検査。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orc.checkpoint import (
    CheckpointView,
    read_checkpoint,
    replay_events,
    verify_checkpoint,
    verify_quarantine_integrity_key,
)
from orc.errors import CheckpointCorrupt, TamperDetected


def load_or_replay(
    events: list[dict[str, Any]],
    manifest: dict[str, Any],
    checkpoint_path: Path,
    run_dir: Path,
    fencing_token: int,
) -> CheckpointView:
    """checkpoint破損/遅延時は検証済みeventsから非破壊replayする。"""
    verify_quarantine_integrity_key(run_dir)
    try:
        checkpoint = read_checkpoint(checkpoint_path)
    except CheckpointCorrupt:
        return replay_events(
            events,
            base_commit=manifest["base_commit"],
            fencing_token=fencing_token,
        )
    if checkpoint.seq < len(events):
        expected = events[checkpoint.seq - 1]["hash"] if checkpoint.seq else "0" * 64
        if checkpoint.events_head_hash != expected:
            raise TamperDetected("tamper_detected: stale checkpoint head mismatch")
        return replay_events(
            events,
            base_commit=manifest["base_commit"],
            fencing_token=fencing_token,
        )
    verify_checkpoint(
        checkpoint,
        events,
        run_dir,
        base_commit=manifest["base_commit"],
        fencing_token=fencing_token,
    )
    return checkpoint


def verify_integrity_snapshot(
    events: list[dict[str, Any]],
    manifest: dict[str, Any],
    checkpoint_path: Path,
    run_dir: Path,
    fencing_token: int,
) -> CheckpointView:
    """chain/digest/base/fenceを全て照合する。"""
    try:
        checkpoint = read_checkpoint(checkpoint_path)
    except CheckpointCorrupt as error:
        raise TamperDetected("tamper_detected: checkpoint corrupt") from error
    verify_checkpoint(
        checkpoint,
        events,
        run_dir,
        base_commit=manifest["base_commit"],
        fencing_token=fencing_token,
    )
    return checkpoint
