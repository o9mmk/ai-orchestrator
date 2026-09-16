"""O_EXCL lease、renew、単調増加fencing token。"""

from __future__ import annotations

import fcntl
import json
import math
import os
import secrets
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from orc.errors import LeaseHeld, LeaseLost
from orc.io_utils import atomic_write_json, canonical_json
from orc.paths import StatePaths, ensure_private_dir, validate_identifier


@dataclass(frozen=True)
class Lease:
    """repo leaseの永続化内容。"""

    run_id: str
    pid: int
    fencing_token: int
    acquired_at: float
    ttl: int
    renewed_at: float

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Lease:
        """必須fieldを持つJSON objectからleaseを復元する。"""
        required = {"run_id", "pid", "fencing_token", "acquired_at", "ttl", "renewed_at"}
        if set(data) != required:
            raise LeaseLost("lease_lost: invalid lease fields")
        if (
            not isinstance(data["run_id"], str)
            or not isinstance(data["pid"], int)
            or isinstance(data["pid"], bool)
            or not isinstance(data["fencing_token"], int)
            or isinstance(data["fencing_token"], bool)
            or not isinstance(data["acquired_at"], (int, float))
            or isinstance(data["acquired_at"], bool)
            or not isinstance(data["ttl"], int)
            or isinstance(data["ttl"], bool)
            or not isinstance(data["renewed_at"], (int, float))
            or isinstance(data["renewed_at"], bool)
        ):
            raise LeaseLost("lease_lost: invalid lease value types")
        if (
            data["pid"] <= 0
            or data["fencing_token"] <= 0
            or data["ttl"] <= 0
            or not math.isfinite(float(data["acquired_at"]))
            or not math.isfinite(float(data["renewed_at"]))
        ):
            raise LeaseLost("lease_lost: invalid lease value range")
        try:
            validate_identifier(data["run_id"], label="run_id")
        except ValueError as error:
            raise LeaseLost("lease_lost: invalid lease run_id") from error
        return cls(
            run_id=data["run_id"],
            pid=data["pid"],
            fencing_token=data["fencing_token"],
            acquired_at=float(data["acquired_at"]),
            ttl=data["ttl"],
            renewed_at=float(data["renewed_at"]),
        )


def _pid_alive(pid: int) -> bool:
    """権限不足は生存扱いにしてlease強奪を避ける。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class LeaseManager:
    """1 repoのlease lifecycleを管理する。"""

    def __init__(
        self,
        repo_path: Path,
        *,
        pid: int | None = None,
        clock: Callable[[], float] = time.time,
        pid_alive: Callable[[int], bool] = _pid_alive,
    ) -> None:
        self.repo_path = repo_path.resolve(strict=True)
        self.pid = os.getpid() if pid is None else pid
        self.clock = clock
        self.pid_alive = pid_alive
        paths = StatePaths.for_run(self.repo_path, "_lease")
        ensure_private_dir(paths.root)
        self.lock_dir = paths.lock_dir
        self.lease_path = self.lock_dir / "lease.json"
        self.counter_path = self.lock_dir / "fencing_counter"
        self.counter_lock_path = self.lock_dir / ".fencing_counter.lock"
        self.operation_lock_path = self.lock_dir / ".lease-operation.lock"

    def acquire(self, run_id: str, *, ttl_seconds: int = 900) -> Lease:
        """原子的にleaseを取得し、競合はlease_heldで拒否する。"""
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        run_id = validate_identifier(run_id, label="run_id")
        ensure_private_dir(self.lock_dir)
        with self._operation_lock():
            existing = self._read_if_present()
            if existing is not None:
                if self.pid_alive(existing.pid):
                    raise LeaseHeld(f"lease_held: run_id={existing.run_id}")
                self._move_stale_lease()
            token = self._next_fencing_token()
            now = self.clock()
            lease = Lease(run_id, self.pid, token, now, ttl_seconds, now)
            self._create_exclusive(lease)
            return lease

    def renew(self, lease: Lease) -> Lease:
        """所有権を再確認してrenewed_atだけを更新する。"""
        with self.fenced(lease.run_id, lease.fencing_token):
            renewed = Lease(
                lease.run_id,
                lease.pid,
                lease.fencing_token,
                lease.acquired_at,
                lease.ttl,
                self.clock(),
            )
            atomic_write_json(self.lease_path, asdict(renewed))
            return renewed

    def assert_fencing(self, run_id: str, fencing_token: int) -> Lease:
        """run/token不一致を即lease_lostとして通知する。"""
        with self._operation_lock():
            return self._assert_fencing_unlocked(run_id, fencing_token)

    def current(self) -> Lease | None:
        """操作lock下で現在のleaseを読み、cancel判定へ渡す。"""
        with self._operation_lock():
            return self._read_if_present()

    @contextmanager
    def fenced(self, run_id: str, fencing_token: int) -> Iterator[Lease]:
        """fencing確認からstate write完了までlease交代を直列化する。"""
        with self._operation_lock():
            yield self._assert_fencing_unlocked(run_id, fencing_token)

    def _assert_fencing_unlocked(self, run_id: str, fencing_token: int) -> Lease:
        current = self._read_if_present()
        if current is None:
            raise LeaseLost("lease_lost: lease file is missing")
        if current.run_id != run_id or current.fencing_token != fencing_token:
            raise LeaseLost("lease_lost: fencing token mismatch")
        return current

    def release(self, lease: Lease) -> None:
        """現在の所有者だけがleaseを解放できる。"""
        with self.fenced(lease.run_id, lease.fencing_token):
            self.lease_path.unlink()

    def _read_if_present(self) -> Lease | None:
        try:
            raw = self.lease_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            raise LeaseLost("lease_lost: invalid lease JSON") from error
        if not isinstance(data, dict):
            raise LeaseLost("lease_lost: lease must be an object")
        return Lease.from_dict(data)

    def _move_stale_lease(self) -> None:
        tombstone = self.lock_dir / f".lease.stale-{secrets.token_hex(8)}"
        try:
            os.rename(self.lease_path, tombstone)
        except FileNotFoundError:
            return
        tombstone.unlink()

    def _next_fencing_token(self) -> int:
        descriptor = os.open(self.counter_lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "r+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                current = int(self.counter_path.read_text(encoding="ascii"))
            except FileNotFoundError:
                current = 0
            token = current + 1
            temporary = self.counter_path.with_suffix(".tmp")
            temporary.write_text(f"{token}\n", encoding="ascii")
            temporary.chmod(0o600)
            os.replace(temporary, self.counter_path)
            return token

    def _create_exclusive(self, lease: Lease) -> None:
        descriptor = os.open(self.lease_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json(asdict(lease)) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())

    @contextmanager
    def _operation_lock(self) -> Iterator[None]:
        ensure_private_dir(self.lock_dir)
        descriptor = os.open(self.operation_lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "r+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            yield
