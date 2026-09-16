"""lease未取得runの課金前REFUSED監査記録。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orc.checkpoint import collect_artifact_digests, write_checkpoint
from orc.events import EventLog
from orc.io_utils import atomic_write_json
from orc.paths import StatePaths, ensure_private_dir
from orc.schemas import validate_manifest


def record_prelease_refusal(
    repo_path: Path,
    run_id: str,
    manifest: dict[str, Any],
    *,
    reason: str,
) -> tuple[Path, Path]:
    """run固有directoryへimmutableなREFUSED記録を作る。"""
    paths = StatePaths.for_run(repo_path, run_id)
    ensure_private_dir(paths.root)
    ensure_private_dir(paths.run_dir.parent)
    paths.run_dir.mkdir(mode=0o700, exist_ok=False)
    manifest = {**manifest, "state": "REFUSED", "fencing_token": 0}
    validate_manifest(manifest)
    manifest_path = paths.run_dir / "manifest.json"
    events_path = paths.run_dir / "events.jsonl"
    checkpoint_path = paths.run_dir / "checkpoint.json"
    atomic_write_json(manifest_path, manifest)
    events = EventLog(events_path, run_id)
    events.append("run_created", "manager", {"state": "INIT"})
    final = events.append(
        "state_transition",
        "manager",
        {"from": "INIT", "to": "REFUSED", "reason": reason},
    )
    write_checkpoint(
        checkpoint_path,
        {
            "seq": 2,
            "events_head_hash": final["hash"],
            "run_state": "REFUSED",
            "tasks": {},
            "budget": {
                "tokens_used": 0,
                "budget_source": manifest["budget_source"],
                "invocations": 0,
                "child_invocations": 0,
                "manager_calls": 0,
                "active_seconds": 0,
                "calendar_seconds": 0,
                "soft_reached": False,
                "hard_reached": False,
                "halt_reason": None,
            },
            "base_commit": manifest["base_commit"],
            "artifact_digests": collect_artifact_digests(paths.run_dir),
            "fencing_token": 0,
        },
    )
    return events_path, checkpoint_path
