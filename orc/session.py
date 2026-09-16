"""Short-lived CLI session reopening with monotonic fencing rotation."""

from __future__ import annotations

from pathlib import Path

from orc.io_utils import atomic_write_json
from orc.lease import LeaseManager
from orc.run_snapshot import load_run_snapshot
from orc.schemas import validate_manifest
from orc.store import RunStateStore


def reopen_run(repo_path: Path, run_id: str) -> RunStateStore:
    """Verify old bytes first, acquire a new lease, then rotate the run fence."""
    repo = repo_path.resolve(strict=True)
    snapshot = load_run_snapshot(repo, run_id)
    if snapshot.manifest["fencing_token"] == 0:
        raise ValueError("pre-lease refused run cannot be reopened")
    lease_manager = LeaseManager(repo)
    lease = lease_manager.acquire(run_id)
    try:
        fresh = load_run_snapshot(repo, run_id)
        if fresh.checkpoint.events_head_hash != snapshot.checkpoint.events_head_hash:
            raise ValueError("run changed while reopening")
        manifest = fresh.manifest.copy()
        old_token = manifest["fencing_token"]
        manifest["fencing_token"] = lease.fencing_token
        validate_manifest(manifest)
        store = RunStateStore(repo, run_id, lease_manager, lease)
        with lease_manager.fenced(run_id, lease.fencing_token):
            atomic_write_json(store.manifest_path, manifest)
            store.runtime_state = manifest["state"]
            store.runtime_budget = fresh.checkpoint.budget.copy()
            store.runtime_tasks = {
                key: value.copy() for key, value in fresh.checkpoint.tasks.items()
            }
            store.events.append(
                "lease_rotated",
                "manager",
                {"old_fencing_token": old_token, "new_fencing_token": lease.fencing_token},
            )
            store._write_checkpoint(run_state=manifest["state"])
        return store
    except Exception:
        lease_manager.release(lease)
        raise


def release_run(store: RunStateStore) -> None:
    """Release exactly the lease owned by the current short-lived command."""
    store.lease_manager.release(store.lease)
