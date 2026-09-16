"""Shared bounded process-group termination primitives."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass

from orc.errors import ChildExecutionError


@dataclass(frozen=True)
class TerminationResult:
    """How a process group reached a terminal state."""

    signal: str
    forced: bool
    exit_code: int | None


def process_group_alive(pgid: int) -> bool:
    """Return whether the OS still knows the process group."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError as error:
        raise ChildExecutionError(f"cannot inspect process group {pgid}") from error
    return True


def validate_process_identity(pid: int, pgid: int) -> bool:
    """Prove the leader/session identity before an orphan group may be killed."""
    if pgid == os.getpgrp():
        raise ChildExecutionError("PID/PGID mismatch: refusing to signal Manager group")
    try:
        actual_pgid = os.getpgid(pid)
    except ProcessLookupError as error:
        if process_group_alive(pgid):
            raise ChildExecutionError(
                "PID/PGID mismatch: leader absent but group is live"
            ) from error
        return False
    if actual_pgid != pgid or pid != pgid:
        raise ChildExecutionError("PID/PGID mismatch: refusing unsafe orphan recovery")
    return True


def terminate_popen_group(
    process: subprocess.Popen[bytes],
    *,
    grace_seconds: float,
    poll_interval: float,
) -> TerminationResult:
    """Send TERM then KILL to the Popen session and wait for the full group."""
    pgid = os.getpgid(process.pid)
    return terminate_known_group(
        process,
        pgid,
        grace_seconds=grace_seconds,
        poll_interval=poll_interval,
    )


def terminate_known_group(
    process: subprocess.Popen[bytes],
    pgid: int,
    *,
    grace_seconds: float,
    poll_interval: float,
) -> TerminationResult:
    """Terminate a saved child PGID even after its leader has already exited."""
    if pgid == os.getpgrp():
        raise ChildExecutionError("PID/PGID mismatch: refusing to signal Manager group")
    _send_group_signal(pgid, signal.SIGTERM)
    if _wait_for_group_exit(pgid, grace_seconds, poll_interval, process=process):
        return TerminationResult("SIGTERM", False, _reap_popen(process))
    _send_group_signal(pgid, signal.SIGKILL)
    if not _wait_for_group_exit(pgid, 2.0, poll_interval, process=process):
        raise ChildExecutionError(f"process group survived SIGKILL: {pgid}")
    return TerminationResult("SIGKILL", True, _reap_popen(process))


def terminate_orphan_group(
    pid: int,
    pgid: int,
    *,
    grace_seconds: float,
    poll_interval: float,
) -> TerminationResult:
    """Terminate a ledgered group that is not represented by a Popen object."""
    _send_group_signal(pgid, signal.SIGTERM)
    if _wait_for_group_exit(pgid, grace_seconds, poll_interval, orphan_pid=pid):
        return TerminationResult("SIGTERM", False, None)
    _send_group_signal(pgid, signal.SIGKILL)
    if not _wait_for_group_exit(pgid, 2.0, poll_interval, orphan_pid=pid):
        raise ChildExecutionError(f"orphan process group survived SIGKILL: {pgid}")
    return TerminationResult("SIGKILL", True, None)


def _send_group_signal(pgid: int, requested: signal.Signals) -> None:
    try:
        os.killpg(pgid, requested)
    except ProcessLookupError:
        return


def _wait_for_group_exit(
    pgid: int,
    timeout: float,
    poll_interval: float,
    *,
    process: subprocess.Popen[bytes] | None = None,
    orphan_pid: int | None = None,
) -> bool:
    deadline = time.monotonic() + max(timeout, 0)
    while True:
        if process is not None:
            process.poll()
        if orphan_pid is not None:
            _reap_if_child(orphan_pid)
        try:
            alive = process_group_alive(pgid)
        except ChildExecutionError:
            leader_pid = process.pid if process is not None else orphan_pid
            if leader_pid is not None and not _pid_alive(leader_pid):
                return True
            raise
        if not alive:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)


def _reap_if_child(pid: int) -> None:
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return


def _reap_popen(process: subprocess.Popen[bytes]) -> int:
    """Collect an exited leader even when group disappearance won the poll race."""
    try:
        return process.wait(timeout=2.0)
    except subprocess.TimeoutExpired as error:
        raise ChildExecutionError(
            f"process leader was not reapable after group exit: {process.pid}"
        ) from error


def _pid_alive(pid: int) -> bool:
    try:
        os.getpgid(pid)
    except ProcessLookupError:
        return False
    return True
