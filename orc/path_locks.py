"""lease holderだけが更新できるrepo path lock台帳。"""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any

from orc.errors import PathLockConflict, TamperDetected
from orc.io_utils import atomic_write_json
from orc.lease import Lease, LeaseManager


def _normalized(path: str) -> str:
    """絶対pathと親参照を拒否し、比較用POSIX pathへ正規化する。"""
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"unsafe path lock scope: {path}")
    normalized = candidate.as_posix()
    if not normalized or normalized == ".":
        raise ValueError("path lock scope must not be empty")
    return normalized


def _overlaps(left: str, right: str) -> bool:
    """同一pathまたは親子pathなら重複と判定する。"""
    left_parts = PurePosixPath(left).parts
    right_parts = PurePosixPath(right).parts
    shorter = min(len(left_parts), len(right_parts))
    return left_parts[:shorter] == right_parts[:shorter]


class PathLockManager:
    """path_locks.jsonをfencing付きで更新する。"""

    def __init__(self, lease_manager: LeaseManager, lease: Lease) -> None:
        self.lease_manager = lease_manager
        self.lease = lease
        self.path = lease_manager.lock_dir / "path_locks.json"

    def acquire(self, task_id: str, scopes: list[str]) -> list[str]:
        """重複のないscopeをtaskへ割り当て、正規化結果を返す。"""
        normalized = sorted({_normalized(scope) for scope in scopes})
        with self.lease_manager.fenced(self.lease.run_id, self.lease.fencing_token):
            data = self._load_for_current_fence()
            for requested in normalized:
                conflict = next(
                    (
                        lock
                        for lock in data["locks"]
                        if lock["task_id"] != task_id and _overlaps(requested, lock["path"])
                    ),
                    None,
                )
                if conflict is not None:
                    raise PathLockConflict(f"path_lock_conflict: {requested} overlaps {conflict['path']}")
            data["locks"] = [lock for lock in data["locks"] if lock["task_id"] != task_id]
            data["locks"].extend(
                {"run_id": self.lease.run_id, "task_id": task_id, "path": scope} for scope in normalized
            )
            atomic_write_json(self.path, data)
        return normalized

    def release(self, task_id: str) -> None:
        """taskに属するlockだけを除去する。"""
        with self.lease_manager.fenced(self.lease.run_id, self.lease.fencing_token):
            data = self._load_for_current_fence()
            data["locks"] = [lock for lock in data["locks"] if lock["task_id"] != task_id]
            atomic_write_json(self.path, data)

    def _load_for_current_fence(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"fencing_token": self.lease.fencing_token, "locks": []}
        except json.JSONDecodeError as error:
            raise TamperDetected("tamper_detected: invalid path_locks JSON") from error
        if not isinstance(data, dict) or set(data) != {"fencing_token", "locks"}:
            raise TamperDetected("tamper_detected: invalid path_locks schema")
        if data["fencing_token"] != self.lease.fencing_token:
            return {"fencing_token": self.lease.fencing_token, "locks": []}
        if not isinstance(data["locks"], list):
            raise TamperDetected("tamper_detected: path_locks locks must be an array")
        required = {"run_id", "task_id", "path"}
        if any(
            not isinstance(lock, dict)
            or set(lock) != required
            or not all(isinstance(lock[field], str) for field in required)
            for lock in data["locks"]
        ):
            raise TamperDetected("tamper_detected: invalid path lock entry")
        return data
