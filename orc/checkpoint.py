"""checkpoint digest、atomic write、events replay。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import ValidationError

from orc.errors import CheckpointCorrupt, TamperDetected
from orc.io_utils import atomic_write_json
from orc.schemas import validate_checkpoint

QUARANTINE_HMAC_KEY_NAME = ".quarantine-hmac-key"
QUARANTINE_KEY_COMMITMENT_EVENT = "quarantine_integrity_key_committed"


@dataclass(frozen=True)
class CheckpointView:
    """checkpointまたはevents replayの復元結果。"""

    seq: int
    events_head_hash: str
    run_state: str
    tasks: dict[str, Any]
    budget: dict[str, Any]
    base_commit: str
    artifact_digests: dict[str, str]
    fencing_token: int
    source: str = "checkpoint"


def _requires_quarantine_key(run_dir: Path) -> bool:
    """lease取得済みrunだけquarantine keyを必須にする。"""
    try:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise TamperDetected("tamper_detected: invalid run manifest") from error
    fencing_token = manifest.get("fencing_token") if isinstance(manifest, dict) else None
    if not isinstance(fencing_token, int) or fencing_token < 0:
        raise TamperDetected("tamper_detected: invalid run fencing token")
    return fencing_token > 0


def _read_quarantine_key(run_dir: Path) -> bytes | None:
    """symlinkを辿らずprivateな32-byte keyだけを読む。"""
    key_path = run_dir / QUARANTINE_HMAC_KEY_NAME
    try:
        descriptor = os.open(
            key_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise TamperDetected("tamper_detected: invalid quarantine integrity key") from error
    with os.fdopen(descriptor, "rb", closefd=True) as handle:
        key_info = os.fstat(handle.fileno())
        key = handle.read(33)
    if (
        not stat.S_ISREG(key_info.st_mode)
        or key_info.st_nlink != 1
        or stat.S_IMODE(key_info.st_mode) != 0o600
        or len(key) != 32
    ):
        raise TamperDetected("tamper_detected: invalid quarantine integrity key")
    return key


def _read_quarantine_key_commitment(run_dir: Path) -> str | None:
    """hash-chainに固定した初期key commitmentを読む。"""
    try:
        lines = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as error:
        raise TamperDetected("tamper_detected: events missing") from error
    commitments: list[str] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise TamperDetected("tamper_detected: invalid events JSON") from error
        if not isinstance(event, dict) or event.get("type") != QUARANTINE_KEY_COMMITMENT_EVENT:
            continue
        data = event.get("data")
        commitment = data.get("commitment") if isinstance(data, dict) else None
        algorithm = data.get("algorithm") if isinstance(data, dict) else None
        if (
            algorithm != "sha256"
            or not isinstance(commitment, str)
            or len(commitment) != 64
            or any(character not in "0123456789abcdef" for character in commitment)
        ):
            raise TamperDetected("tamper_detected: invalid quarantine key commitment")
        commitments.append(commitment)
    if not commitments:
        return None
    if len(commitments) != 1:
        raise TamperDetected("tamper_detected: duplicate quarantine key commitment")
    return commitments[0]


def verify_quarantine_integrity_key(run_dir: Path) -> bytes | None:
    """run種別に応じてquarantine keyと初期commitmentを照合する。"""
    key = _read_quarantine_key(run_dir)
    if _requires_quarantine_key(run_dir):
        commitment = _read_quarantine_key_commitment(run_dir)
        if key is None or commitment is None:
            raise TamperDetected("tamper_detected: quarantine integrity key missing")
        actual_commitment = hashlib.sha256(key).hexdigest()
        if not hmac.compare_digest(commitment, actual_commitment):
            raise TamperDetected("tamper_detected: quarantine integrity key replaced")
    elif key is not None:
        raise TamperDetected("tamper_detected: unexpected pre-lease quarantine key")
    return key


def collect_artifact_digests(run_dir: Path) -> dict[str, str]:
    """通常artifactのSHA-256とquarantineのkeyed HMACを返す。"""
    digests: dict[str, str] = {}
    key_path = run_dir / QUARANTINE_HMAC_KEY_NAME
    key = verify_quarantine_integrity_key(run_dir)
    for path in sorted(run_dir.rglob("*")):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise TamperDetected("tamper_detected: invalid artifact file type")
        if path.name in {"events.jsonl", "checkpoint.json"}:
            continue
        if path == key_path:
            continue
        if ".tmp-" in path.name:
            continue
        relative = path.relative_to(run_dir).as_posix()
        payload = path.read_bytes()
        if path.is_relative_to(run_dir / "quarantine"):
            if key is None:
                raise TamperDetected("tamper_detected: quarantine integrity key missing")
            digests[relative] = hmac.new(
                key,
                relative.encode("utf-8") + b"\x00" + payload,
                hashlib.sha256,
            ).hexdigest()
        else:
            digests[relative] = hashlib.sha256(payload).hexdigest()
    return digests


def write_checkpoint(path: Path, data: dict[str, Any]) -> CheckpointView:
    """schema検証後にcheckpointを原子的に置換する。"""
    validate_checkpoint(data)
    atomic_write_json(path, data)
    return CheckpointView(**data)


def read_checkpoint(path: Path) -> CheckpointView:
    """壊れたJSON/schemaをreplay可能な専用例外へ変換する。"""
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise CheckpointCorrupt("checkpoint_corrupt: unreadable JSON") from error
    if not isinstance(data, dict):
        raise CheckpointCorrupt("checkpoint_corrupt: checkpoint must be an object")
    try:
        validate_checkpoint(data)
    except ValidationError as error:
        raise CheckpointCorrupt("checkpoint_corrupt: invalid schema") from error
    return CheckpointView(**data)


def replay_events(
    events: list[dict[str, Any]],
    *,
    base_commit: str,
    fencing_token: int,
) -> CheckpointView:
    """検証済みeventだけから最新run/task/budget状態を再構築する。"""
    run_state = "INIT"
    tasks: dict[str, Any] = {}
    budget: dict[str, Any] = {
        "tokens_used": 0,
        "budget_source": "count_proxy",
        "invocations": 0,
        "child_invocations": 0,
        "manager_calls": 0,
        "active_seconds": 0,
        "calendar_seconds": 0,
        "soft_reached": False,
        "hard_reached": False,
        "halt_reason": None,
    }
    for event in events:
        data = event["data"]
        if event["type"] == "run_created":
            state = data.get("state")
            if state not in {
                "INIT",
                "PREFLIGHT1",
                "PLANNING",
                "PREFLIGHT2",
                "AWAITING_START_APPROVAL",
                "RUNNING",
                "INTEGRATING",
                "AWAITING_APPROVAL",
                "CANCELLING",
                "REFUSED",
                "FAILED",
                "HALTED",
                "CANCELLED",
                "COMPLETED",
            }:
                raise TamperDetected("tamper_detected: invalid run_created state")
            run_state = state
        elif event["type"] == "state_transition":
            state = data.get("to")
            if state not in {
                "INIT",
                "PREFLIGHT1",
                "PLANNING",
                "PREFLIGHT2",
                "AWAITING_START_APPROVAL",
                "RUNNING",
                "INTEGRATING",
                "AWAITING_APPROVAL",
                "CANCELLING",
                "REFUSED",
                "FAILED",
                "HALTED",
                "CANCELLED",
                "COMPLETED",
            }:
                raise TamperDetected("tamper_detected: invalid state transition state")
            run_state = state
        elif event["type"] == "task_updated":
            tasks[event["task_id"]] = data
        elif event["type"] == "budget_updated":
            budget = data
    head = events[-1]["hash"] if events else "0" * 64
    return CheckpointView(
        seq=len(events),
        events_head_hash=head,
        run_state=run_state,
        tasks=tasks,
        budget=budget,
        base_commit=base_commit,
        artifact_digests={},
        fencing_token=fencing_token,
        source="events",
    )


def verify_checkpoint(
    checkpoint: CheckpointView,
    events: list[dict[str, Any]],
    run_dir: Path,
    *,
    base_commit: str,
    fencing_token: int,
) -> None:
    """checkpoint同期点と全artifact digestを照合する。"""
    if checkpoint.seq > len(events):
        raise TamperDetected("tamper_detected: checkpoint seq exceeds events")
    expected_head = "0" * 64 if checkpoint.seq == 0 else events[checkpoint.seq - 1]["hash"]
    if checkpoint.events_head_hash != expected_head:
        raise TamperDetected("tamper_detected: checkpoint event head mismatch")
    if checkpoint.base_commit != base_commit or checkpoint.fencing_token != fencing_token:
        raise TamperDetected("tamper_detected: checkpoint invariant mismatch")
    replayed = replay_events(
        events[: checkpoint.seq],
        base_commit=base_commit,
        fencing_token=fencing_token,
    )
    if checkpoint.run_state != replayed.run_state:
        raise TamperDetected("tamper_detected: checkpoint state mismatch")
    if checkpoint.tasks != replayed.tasks:
        raise TamperDetected("tamper_detected: checkpoint tasks mismatch")
    if checkpoint.artifact_digests != collect_artifact_digests(run_dir):
        raise TamperDetected("tamper_detected: artifact digest mismatch")
