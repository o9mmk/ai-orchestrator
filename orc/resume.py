"""M8 immutable-source resume into a new run generation."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validate

from orc.errors import ResumeBlocked
from orc.git_facts import collect_git_facts
from orc.lease import LeaseManager
from orc.paths import StatePaths
from orc.run_snapshot import load_run_snapshot
from orc.schemas import CAPS_SCHEMA
from orc.store import RunStateStore


@dataclass(frozen=True)
class ResumeConfig:
    """User-fixed new-generation inputs; cap raise requires explicit approval."""

    new_run_id: str
    safety_policy_version: str
    gates: tuple[str, ...]
    caps: dict[str, Any] | None = None
    allow_cap_raise: bool = False


@dataclass(frozen=True)
class ResumeOutcome:
    """New generation and reusable task evidence."""

    store: RunStateStore
    generation: int
    reused_tasks: tuple[str, ...]


class ResumeService:
    """Verify an old terminal run read-only, then create a separate generation."""

    def __init__(self, repo_path: Path) -> None:
        self.repo_path = repo_path.resolve(strict=True)

    def resume(self, source_run_id: str, config: ResumeConfig) -> ResumeOutcome:
        """Create a new INIT run or fail before creating any new run artifacts."""
        snapshot = load_run_snapshot(self.repo_path, source_run_id)
        manifest = snapshot.manifest
        if manifest["state"] not in {"HALTED", "CANCELLED", "FAILED"}:
            raise ResumeBlocked(f"source_state_not_resumable:{manifest['state']}")
        if (
            manifest["safety_policy_version"] != config.safety_policy_version
            or tuple(manifest["gates"]) != config.gates
        ):
            raise ResumeBlocked("policy_or_gates_mismatch")
        if collect_git_facts(self.repo_path).head != manifest["base_commit"]:
            raise ResumeBlocked("stale_head")
        selected_caps = deepcopy(config.caps if config.caps is not None else manifest["caps"])
        try:
            validate(instance=selected_caps, schema=CAPS_SCHEMA)
        except ValidationError as error:
            raise ResumeBlocked("invalid_caps") from error
        if _has_cap_raise(manifest["caps"], selected_caps) and not config.allow_cap_raise:
            raise ResumeBlocked("cap_raise_requires_approval")
        tasks, reused = _resume_tasks(
            snapshot.checkpoint.tasks,
            snapshot.checkpoint.artifact_digests,
            source_run_id,
        )
        target_paths = StatePaths.for_run(self.repo_path, config.new_run_id)
        if target_paths.run_dir.exists() or target_paths.worktree_root.exists():
            raise ResumeBlocked("new_run_id_already_exists")
        lease_manager = LeaseManager(self.repo_path)
        lease = lease_manager.acquire(config.new_run_id)
        new_manifest = {
            "run_id": config.new_run_id,
            "generation": manifest["generation"] + 1,
            "parent_run_id": source_run_id,
            "resumed_from": snapshot.checkpoint.seq,
            "created_at": datetime.now(UTC).isoformat(),
            "goal": manifest["goal"],
            "acceptance_criteria": deepcopy(manifest["acceptance_criteria"]),
            "forbidden": deepcopy(manifest["forbidden"]),
            "authority_sources": deepcopy(manifest["authority_sources"]),
            "repo_path": str(self.repo_path),
            "base_commit": manifest["base_commit"],
            "size": manifest["size"],
            "caps": selected_caps,
            "budget_source": manifest["budget_source"],
            "reviewer_policy": manifest["reviewer_policy"],
            "gates": list(config.gates),
            "safety_policy_version": config.safety_policy_version,
            "state": "INIT",
            "fencing_token": lease.fencing_token,
        }
        store = RunStateStore(self.repo_path, config.new_run_id, lease_manager, lease)
        store.initialize(new_manifest)
        store.append_event(
            "run_resumed",
            "manager",
            {
                "parent_run_id": source_run_id,
                "resumed_from": snapshot.checkpoint.seq,
                "reused_tasks": list(reused),
            },
        )
        for task_id, task in tasks.items():
            store.record_task_state(task_id, task)
        return ResumeOutcome(store, new_manifest["generation"], reused)


def _resume_tasks(
    tasks: dict[str, Any],
    artifact_digests: dict[str, str],
    source_run_id: str,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    resumed: dict[str, Any] = {}
    reused: list[str] = []
    for task_id, source in sorted(tasks.items()):
        if not isinstance(source, dict) or source.get("state") not in {
            "QUEUED",
            "RUNNING",
            "VERIFYING",
            "REVIEWING",
            "FIXING",
            "DONE",
            "ESCALATED",
            "ABORTED",
        }:
            raise ResumeBlocked(f"invalid_task_state:{task_id}")
        task = deepcopy(source)
        if task["state"] == "DONE":
            attempt = task.get("attempt")
            if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
                raise ResumeBlocked(f"invalid_done_attempt:{task_id}")
            prefix = f"tasks/{task_id}/attempt-{attempt}/"
            references = [
                {"path": path, "digest": digest}
                for path, digest in sorted(artifact_digests.items())
                if path.startswith(prefix)
            ]
            if not references:
                raise ResumeBlocked(f"done_task_artifacts_missing:{task_id}")
            task["reused_from"] = source_run_id
            task["reused_artifacts"] = references
            reused.append(task_id)
        else:
            task["state"] = "QUEUED"
        resumed[task_id] = task
    return resumed, tuple(reused)


def _has_cap_raise(parent: dict[str, Any], selected: dict[str, Any]) -> bool:
    if set(parent) != set(selected):
        raise ResumeBlocked("caps_shape_mismatch")
    for key, parent_value in parent.items():
        selected_value = selected[key]
        if isinstance(parent_value, dict):
            if not isinstance(selected_value, dict) or _has_cap_raise(parent_value, selected_value):
                return True
        elif (
            isinstance(parent_value, (int, float))
            and not isinstance(parent_value, bool)
            and selected_value > parent_value
        ):
            return True
    return False
