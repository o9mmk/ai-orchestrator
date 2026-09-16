"""Lease-free, read-only run status projection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orc.run_snapshot import load_run_snapshot


def read_status(repo_path: Path, run_id: str) -> dict[str, Any]:
    """Verify the entire snapshot and expose only bounded Manager-owned fields."""
    snapshot = load_run_snapshot(repo_path, run_id)
    reason = "unknown"
    for event in reversed(snapshot.events):
        if event["type"] == "state_transition":
            value = event["data"].get("reason")
            reason = value if isinstance(value, str) else "unknown"
            break
    tasks = {
        task_id: {
            "state": task["state"],
            "attempt": task["attempt"],
            "review_cycles": task["review_cycles"],
            "reason": task.get("reason", "unknown"),
        }
        for task_id, task in sorted(snapshot.checkpoint.tasks.items())
    }
    return {
        "run_id": run_id,
        "state": snapshot.manifest["state"],
        "reason": reason,
        "generation": snapshot.manifest["generation"],
        "base_commit": snapshot.manifest["base_commit"],
        "budget": snapshot.checkpoint.budget,
        "tasks": tasks,
        "summary_path": (
            str(snapshot.run_dir / "summary.md")
            if (snapshot.run_dir / "summary.md").is_file()
            else None
        ),
    }
