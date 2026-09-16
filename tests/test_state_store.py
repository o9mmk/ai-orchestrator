"""hash chain/checkpoint/replay/tamper検知の受入テスト。"""

import json
import os
import stat
from pathlib import Path

import pytest
from jsonschema import ValidationError

from orc.errors import TamperDetected
from orc.lease import LeaseManager
from orc.store import RunStateStore
from tests.helpers import manifest_data


def make_store(repo: Path, run_id: str = "run-1") -> RunStateStore:
    """lease取得済みのstoreを返す。"""
    manager = LeaseManager(repo)
    lease = manager.acquire(run_id)
    return RunStateStore(repo, run_id, manager, lease)


def test_manifest_requires_every_design_field(repo: Path) -> None:
    """§9の必須フィールド欠落をjsonschemaでfail-loudに拒否する。"""
    store = make_store(repo)
    manifest = manifest_data(repo, "run-1", store.fencing_token)
    del manifest["safety_policy_version"]

    with pytest.raises(ValidationError):
        store.initialize(manifest)


def test_events_form_hash_chain_and_checkpoint_replays(repo: Path) -> None:
    """eventsの連鎖とcheckpoint同期点から状態を復元する。"""
    store = make_store(repo)
    store.initialize(manifest_data(repo, "run-1", store.fencing_token))
    store.append_event(
        "state_transition",
        "manager",
        {"from": "INIT", "to": "PREFLIGHT1", "reason": "lease_acquired"},
    )
    checkpoint = store.write_checkpoint(run_state="PREFLIGHT1")

    loaded = store.load_checkpoint_or_replay()
    events = store.verify_events()

    assert loaded.run_state == "PREFLIGHT1"
    assert loaded.seq == checkpoint.seq == 3
    assert events[-1]["hash"] == checkpoint.events_head_hash


def test_checkpoint_state_mismatch_is_rejected(repo: Path) -> None:
    """checkpointのrun_state改変はevents replayとの差分として拒否する。"""
    store = make_store(repo)
    store.initialize(manifest_data(repo, "run-1", store.fencing_token))
    store.transition("lease_acquired")

    checkpoint = json.loads(store.checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["run_state"] = "INIT"
    store.checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

    with pytest.raises(TamperDetected, match="tamper_detected"):
        store.verify_integrity()


def test_run_artifacts_use_private_permissions(repo: Path) -> None:
    """監査directoryは0700、主要artifactは0600で保存する。"""
    store = make_store(repo)
    store.initialize(manifest_data(repo, "run-1", store.fencing_token))

    assert stat.S_IMODE(store.run_dir.stat().st_mode) == 0o700
    for path in (store.manifest_path, store.events_path, store.checkpoint_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_at8_corrupt_checkpoint_replays_events_without_silent_repair(repo: Path) -> None:
    """checkpoint JSON破損時はeventsから復元し、破損ファイル自体は直さない。"""
    store = make_store(repo)
    store.initialize(manifest_data(repo, "run-1", store.fencing_token))
    store.append_event(
        "state_transition",
        "manager",
        {"from": "INIT", "to": "PREFLIGHT1", "reason": "lease_acquired"},
    )
    store.write_checkpoint(run_state="PREFLIGHT1")
    store.checkpoint_path.write_text("{broken", encoding="utf-8")

    replayed = store.load_checkpoint_or_replay()

    assert replayed.source == "events"
    assert replayed.run_state == "PREFLIGHT1"
    assert store.checkpoint_path.read_text(encoding="utf-8") == "{broken"


@pytest.mark.parametrize("mutation", ["missing", "replaced"])
def test_checkpoint_replay_rejects_missing_or_replaced_quarantine_key(
    repo: Path,
    mutation: str,
) -> None:
    """checkpoint fallbackでもquarantine key commitmentを迂回させない。"""
    store = make_store(repo)
    store.initialize(manifest_data(repo, "run-1", store.fencing_token))
    key_path = store.run_dir / ".quarantine-hmac-key"
    store.checkpoint_path.write_text("{broken", encoding="utf-8")
    if mutation == "missing":
        key_path.unlink()
        expected = "quarantine integrity key missing"
    else:
        key_path.write_bytes(os.urandom(32))
        key_path.chmod(0o600)
        expected = "quarantine integrity key replaced"

    with pytest.raises(TamperDetected, match=expected):
        store.load_checkpoint_or_replay()


@pytest.mark.parametrize("target", ["manifest", "gates", "artifact", "events"])
def test_at19_tamper_is_rejected_without_repair(repo: Path, target: str) -> None:
    """manifest/gates/patch/events改変は全てresume相当の整合検査で拒否する。"""
    store = make_store(repo)
    store.initialize(manifest_data(repo, "run-1", store.fencing_token))
    artifact = store.run_dir / "tasks" / "t1" / "attempt-1" / "patch.diff"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("safe patch\n", encoding="utf-8")
    store.write_checkpoint(run_state="INIT")

    if target in {"manifest", "gates"}:
        manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
        if target == "manifest":
            manifest["goal"] = "tampered"
        else:
            manifest["gates"].append("tampered-gate")
        store.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif target == "artifact":
        artifact.write_text("tampered patch\n", encoding="utf-8")
    else:
        lines = store.events_path.read_text(encoding="utf-8").splitlines()
        event = json.loads(lines[0])
        event["data"]["state"] = "COMPLETED"
        lines[0] = json.dumps(event)
        store.events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(TamperDetected, match="tamper_detected"):
        store.verify_integrity()

    assert target != "artifact" or artifact.read_text(encoding="utf-8") == "tampered patch\n"
