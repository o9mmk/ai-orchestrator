"""Shared fake-Codex and state fixtures for M4 tests."""

import os
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from orc.child_models import ChildAttemptOutcome
from orc.child_runner import ChildRunner
from orc.codex_adapter import CodexExecAdapter
from orc.lease import LeaseManager
from orc.store import RunStateStore
from orc.worktree import Worktree
from tests.helpers import manifest_data
from tests.m6_helpers import make_clean_ingestor
from tests.strict_schema_helpers import STRICT_SCHEMA_CHECK_SOURCE


def git(repo: Path, *args: str) -> str:
    """Run a deterministic local git command."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def make_store(repo: Path, *, timeout: int = 1, hard_attempts: int = 3) -> RunStateStore:
    """Create a store with short M4 test caps."""
    manager = LeaseManager(repo)
    lease = manager.acquire("run-1")
    manifest = manifest_data(repo, "run-1", lease.fencing_token)
    manifest["caps"]["task_attempts_hard"] = hard_attempts
    manifest["caps"]["timeouts_seconds"] = {
        "research": timeout,
        "implement": timeout,
        "review": timeout,
        "verify": timeout,
    }
    store = RunStateStore(repo, "run-1", manager, lease)
    store.initialize(manifest)
    return store


def make_owned_worktree(store: RunStateStore, task_id: str = "task-1") -> Worktree:
    """Create a minimal state-owned dedicated worktree for fake execution."""
    path = store.paths.worktree_root / task_id
    path.mkdir(mode=0o700, parents=True)
    (path / ".git").write_text("gitdir: fake\n", encoding="utf-8")
    return Worktree(task_id, "a" * 40, path)


def make_fake_codex(tmp_path: Path, mode: str, *, complete_help: bool = True) -> Path:
    """Create a no-network fake implementing the Codex CLI surface used by M4."""
    executable = tmp_path / f"fake-codex-{mode}"
    help_text = " ".join(
        [
            "--output-schema",
            "--sandbox",
            "--json",
            "--output-last-message",
            "--cd",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
        ]
    )
    if not complete_help:
        help_text = "--output-schema --sandbox"
    script = f"""\
#!{sys.executable}
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

MODE = {mode!r}
if sys.argv[1:] == ["--version"]:
    print("fake-codex 1.0")
    raise SystemExit(0)
if sys.argv[1:] == ["exec", "--help"]:
    print({help_text!r})
    raise SystemExit(0)

args = sys.argv[1:]
assert "--ignore-rules" in args
if "FAKE_EXEC_MARKER" in os.environ:
    Path(os.environ["FAKE_EXEC_MARKER"]).write_text("spawned", encoding="utf-8")
output = Path(args[args.index("--output-last-message") + 1])
schema = Path(args[args.index("--output-schema") + 1])
worktree = Path(args[args.index("--cd") + 1])
assert Path.cwd().resolve() == worktree.resolve()
schema_data = json.loads(schema.read_text(encoding="utf-8"))
{STRICT_SCHEMA_CHECK_SOURCE}
reject_unsupported_schema(schema_data)
staging = output.parent
dummy_key = "sk" + "_" + ("m6safe" * 6)
dummy_email = "m6.person" + "@" + "example.invalid"
dummy_phone = "090" + "-1234" + "-5678"

if MODE == "env_guard":
    assert "ORC_TEST_SECRET" not in os.environ
if MODE == "exit_descendant":
    subprocess.Popen([
        sys.executable,
        "-c",
        (
            "import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "open(os.environ['FAKE_DESCENDANT_PID'], 'w').write(str(os.getpid())); "
            "time.sleep(30)"
        ),
    ])
    descendant_pid = Path(os.environ["FAKE_DESCENDANT_PID"])
    deadline = time.monotonic() + 2
    while not descendant_pid.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert descendant_pid.exists()
if MODE == "stream_secret":
    os.write(1, ("stdout=" + dummy_key).encode())
    os.write(2, ("stderr=" + dummy_email).encode())
elif MODE == "dual_large":
    for _ in range(2048):
        os.write(1, b"o" * 512)
        os.write(2, b"e" * 512)
elif MODE == "output_limit":
    os.write(1, b"x" * 8192)
elif MODE == "secret_patch":
    (staging / "patch.diff").write_text("+credential=" + dummy_key, encoding="utf-8")
elif MODE == "at21":
    (staging / "patch.diff").write_text("+VALUE = 1\\n", encoding="utf-8")
    (staging / "transcript.log").write_text("contact=" + dummy_email, encoding="utf-8")
    (staging / "findings.json").write_text(json.dumps({{"phone": dummy_phone}}), encoding="utf-8")
    os.write(1, ("stdout=" + dummy_key).encode())

if MODE.startswith("timeout"):
    subprocess.Popen([
        sys.executable,
        "-c",
        (
            "import os,signal,time; "
            + ("signal.signal(signal.SIGTERM, signal.SIG_IGN); " if MODE == "timeout_kill" else "")
            + "open(os.environ['FAKE_DESCENDANT_PID'], 'w').write(str(os.getpid())); time.sleep(30)"
        ),
    ])
    if MODE == "timeout_kill":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    else:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    Path(os.environ["FAKE_LEADER_PID"]).write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(30)
elif MODE == "invalid_json":
    output.write_text("UNTRUSTED_BODY_DO_NOT_FORWARD{{", encoding="utf-8")
elif MODE == "invalid_schema":
    output.write_text(json.dumps({{
        "task_id": "task-1",
        "role": "implementer",
        "attempt": 1,
        "claimed_status": "done",
        "summary": "UNTRUSTED_SCHEMA_BODY_DO_NOT_FORWARD",
        "changed_files": ["../escape"],
        "truncated": False,
        "context_requests_used": 0,
    }}), encoding="utf-8")
else:
    (worktree / "child-output.txt").write_text("child-only", encoding="utf-8")
    result_body = {{
        "task_id": "task-1",
        "role": "implementer",
        "attempt": 1,
        "claimed_status": "done",
        "summary": dummy_key if MODE == "escaped_result_secret" else "valid bounded summary",
        "changed_files": ["child-output.txt"],
        "self_check": {{"command": "fake-check", "exit_code": 0}},
        "references": [],
        "truncated": False,
        "context_requests_used": 0,
    }}
    serialized = json.dumps(result_body)
    if MODE == "escaped_result_secret":
        encoded = "".join(chr(92) + "u" + format(ord(char), "04x") for char in dummy_key)
        serialized = serialized.replace(dummy_key, encoded)
    output.write_text(serialized, encoding="utf-8")
"""
    executable.write_text(textwrap.dedent(script), encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def process_exists(pid: int) -> bool:
    """Return whether a PID is still signalable."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_pid_file(path: Path) -> int:
    """Read a child PID emitted before the timeout fires."""
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if path.exists():
            return int(path.read_text(encoding="utf-8"))
        time.sleep(0.01)
    raise AssertionError(f"PID file not created: {path}")


def run_fake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    *,
    attempt: int = 1,
    hard_attempts: int = 3,
    grace: float = 0.1,
    stream_output_limit_bytes: int = 4 * 1024 * 1024,
    prompt: str = "Perform the bounded fake task.",
) -> tuple[RunStateStore, ChildAttemptOutcome]:
    """Run one fake Codex attempt and return its store/outcome."""
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_store(repo, hard_attempts=hard_attempts)
    worktree = make_owned_worktree(store)
    executable = make_fake_codex(tmp_path, mode)
    monkeypatch.setenv("FAKE_EXEC_MARKER", str(worktree.path / "spawned.marker"))
    monkeypatch.setenv("FAKE_LEADER_PID", str(worktree.path / "leader.pid"))
    monkeypatch.setenv("FAKE_DESCENDANT_PID", str(worktree.path / "descendant.pid"))
    runner = ChildRunner(
        store,
        CodexExecAdapter(executable, environment=os.environ.copy()),
        term_grace_seconds=grace,
        poll_interval=0.005,
        dlp_ingestor=make_clean_ingestor(store),
        stream_output_limit_bytes=stream_output_limit_bytes,
    )
    outcome = runner.run(
        worktree,
        task_id="task-1",
        role="implementer",
        attempt=attempt,
        prompt=prompt,
    )
    return store, outcome
