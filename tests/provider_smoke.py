"""Opt-in real Codex smoke with a hard two-invocation adapter limit."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from orc.cli import main
from orc.codex_adapter import CodexExecAdapter
from orc.manager import ManagerService
from orc.preflight_models import PreflightConfig
from orc.session import release_run


class InvocationLimitedCodexAdapter(CodexExecAdapter):
    """Fail before a third provider process can be spawned."""

    def __init__(self, executable: Path, *, limit: int = 2) -> None:
        super().__init__(executable)
        self.limit = limit
        self.invocations = 0

    def spawn(self, **kwargs: Any) -> subprocess.Popen[bytes]:
        if self.invocations >= self.limit:
            raise RuntimeError("real Codex smoke invocation limit reached")
        self.invocations += 1
        return super().spawn(**kwargs)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _fixture_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir(mode=0o700)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "orc-provider-smoke")
    _git(repo, "config", "user.email", "orc-provider-smoke@example.invalid")
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "test_app.py").write_text(
        "from app import VALUE\n\n\ndef test_value():\n    assert VALUE == 2\n",
        encoding="utf-8",
    )
    _git(repo, "add", "app.py", "test_app.py")
    _git(repo, "commit", "-m", "provider smoke fixture")
    return repo


def run_smoke(codex_path: Path) -> dict[str, Any]:
    """Exercise real Planner and implementer schemas, then cancel/gc normally."""
    with tempfile.TemporaryDirectory(prefix="orc-provider-smoke-") as temporary:
        root = Path(temporary)
        repo = _fixture_repo(root)
        state_root = root / "state"
        previous_state = os.environ.get("ORC_STATE_DIR")
        os.environ["ORC_STATE_DIR"] = str(state_root)
        run_id = "provider-smoke"
        adapter = InvocationLimitedCodexAdapter(codex_path)
        manager = ManagerService(repo, adapter)
        store = None
        try:
            outcome, store = manager.start(
                PreflightConfig(
                    run_id=run_id,
                    goal=(
                        "Change only app.py so VALUE equals 2. Produce one size S implementer "
                        "task scoped to app.py and verify it with python -m pytest -q."
                    ),
                    acceptance_criteria=["python -m pytest -q passes"],
                    forbidden=["commit", "merge", "push", "deploy", "network"],
                    authority_sources=["provider smoke fixture"],
                    budget_source="bytes_proxy",
                    reviewer_policy="codex-none",
                    gates=["pytest", "gitleaks", "glassworm"],
                    safety_policy_version="1.0",
                    required_tools=("git", "gitleaks"),
                    optional_tools=(),
                ),
                plan=None,
                model_window_tokens=200_000,
            )
            if outcome.state != "AWAITING_APPROVAL":
                raise RuntimeError(f"provider smoke stopped in unexpected state: {outcome.state}")
            if adapter.invocations != 2:
                raise RuntimeError(
                    f"provider smoke expected two invocations, got {adapter.invocations}"
                )
        finally:
            owned = store or manager.active_store
            if owned is not None and owned.lease_manager.lease_path.exists():
                release_run(owned)
        try:
            if main(["cancel", "--repo", str(repo), run_id]) != 0:
                raise RuntimeError("provider smoke cancel failed")
            if (
                main(
                    [
                        "gc",
                        "--repo",
                        str(repo),
                        run_id,
                        "--confirm-run-id",
                        run_id,
                        "--force-unmerged",
                    ]
                )
                != 0
            ):
                raise RuntimeError("provider smoke gc failed")
            return {
                "run_id": run_id,
                "state": "AWAITING_APPROVAL",
                "invocations": adapter.invocations,
                "cleanup": "cancelled_and_collected",
            }
        finally:
            if previous_state is None:
                os.environ.pop("ORC_STATE_DIR", None)
            else:
                os.environ["ORC_STATE_DIR"] = previous_state


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", type=Path, required=True)
    return parser


if __name__ == "__main__":
    arguments = _parser().parse_args()
    print(json.dumps(run_smoke(arguments.codex), sort_keys=True))
