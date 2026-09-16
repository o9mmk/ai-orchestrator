"""Shared fake Planner, context, and budget fixtures for M5 tests."""

import stat
import sys
import textwrap
from pathlib import Path
from typing import Any

from orc.lease import LeaseManager
from orc.manager_context import PinnedContext
from orc.state_machine import RunState
from orc.store import RunStateStore
from tests.helpers import manifest_data
from tests.m4_helpers import make_owned_worktree
from tests.strict_schema_helpers import STRICT_SCHEMA_CHECK_SOURCE


def make_planning_store(
    repo: Path,
    *,
    cap_overrides: dict[str, Any] | None = None,
    budget_source: str = "bytes_proxy",
) -> RunStateStore:
    """Create a fenced store positioned at PLANNING."""
    manager = LeaseManager(repo)
    lease = manager.acquire("run-1")
    manifest = manifest_data(repo, "run-1", lease.fencing_token)
    manifest["budget_source"] = budget_source
    if cap_overrides:
        manifest["caps"].update(cap_overrides)
    store = RunStateStore(repo, "run-1", manager, lease)
    store.initialize(manifest)
    store.transition("lease_acquired")
    store.transition("checks_passed")
    assert store.read_manifest()["state"] == RunState.PLANNING.value
    return store


def move_to_running(store: RunStateStore) -> None:
    """Advance a test store from PLANNING to RUNNING without a scheduler."""
    store.transition("plan_valid")
    store.transition("size_sm")


def pinned_context(*, goal: str = "Implement the bounded change") -> PinnedContext:
    """Return every mandatory pinned context field in fixed order."""
    return PinnedContext(
        goal=goal,
        acceptance_criteria=("all acceptance tests pass",),
        forbidden=("push", "raw logs in prompts"),
        authority_sources=("user", "AGENTS.md"),
        base_commit="a" * 40,
        path_scope=("tracked.py",),
        safety_policy_version="1.0",
        unresolved_blockers=(),
    )


def valid_plan(*, path_scope: str = "tracked.py", planner_size: str = "S") -> dict[str, Any]:
    """Return a schema-valid Planner payload before Manager sizing correction."""
    return {
        "tasks": [
            {
                "task_id": "task-1",
                "role": "implementer",
                "objective": "Implement the bounded change",
                "path_scope": [path_scope],
                "acceptance": ["tests pass"],
                "depends_on": [],
                "size_estimate": {
                    "estimated_files": 1,
                    "estimated_diff_lines": 20,
                    "estimated_invocations": 1,
                },
                "scope_confidence": "high",
                "commands": ["pytest -q"],
            }
        ],
        "planner_size": planner_size,
        "deterministic_size": "S",
        "final_size": planner_size,
    }


def make_fake_planner(tmp_path: Path, mode: str) -> Path:
    """Create a no-network fake Codex executable for Planner attempts."""
    executable = tmp_path / f"fake-planner-{mode}"
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
    script = f"""\
    #!{sys.executable}
    import json
    import os
    import sys
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
    counter = Path(os.environ["FAKE_PLANNER_COUNTER"])
    attempt = int(counter.read_text(encoding="utf-8")) + 1 if counter.exists() else 1
    counter.write_text(str(attempt), encoding="utf-8")
    Path(os.environ["FAKE_EXEC_MARKER"]).write_text("spawned", encoding="utf-8")
    output = Path(args[args.index("--output-last-message") + 1])
    schema = Path(args[args.index("--output-schema") + 1])
    schema_data = json.loads(schema.read_text(encoding="utf-8"))
    # STRICT_SCHEMA_CHECK
    reject_unsupported_schema(schema_data)
    sys.stdin.read()

    if MODE == "stream_secret":
        os.write(1, ("sk" + "_" + ("m6safe" * 6)).encode())

    invalid = MODE in {{"invalid_json", "invalid_schema"}} or (
        MODE == "invalid_then_valid" and attempt == 1
    )
    if invalid and MODE != "invalid_schema":
        output.write_text("UNTRUSTED_INVALID_PLAN_BODY{{", encoding="utf-8")
    elif invalid:
        output.write_text(json.dumps({{"leak": "UNTRUSTED_SCHEMA_PLAN_BODY"}}), encoding="utf-8")
    else:
        scope = os.environ.get("FAKE_PLAN_SCOPE", "tracked.py")
        plan = {valid_plan()!r}
        plan["tasks"][0]["path_scope"] = [scope]
        if MODE == "escaped_plan_secret":
            dummy_key = "sk" + "_" + ("m6safe" * 6)
            plan["tasks"][0]["objective"] = dummy_key
            encoded = "".join(
                chr(92) + "u" + format(ord(char), "04x") for char in dummy_key
            )
            output.write_text(json.dumps(plan).replace(dummy_key, encoded), encoding="utf-8")
        else:
            output.write_text(json.dumps(plan), encoding="utf-8")
    """
    script = textwrap.dedent(script).replace(
        "# STRICT_SCHEMA_CHECK", STRICT_SCHEMA_CHECK_SOURCE
    )
    executable.write_text(script, encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def planner_worktree(store: RunStateStore):  # type: ignore[no-untyped-def]
    """Create the ledger-owned Planner worktree."""
    return make_owned_worktree(store, "planner")
