"""M1-M3の入力→隔離→sandbox baseline出力を通すintegration test。"""

import subprocess
import sys
from pathlib import Path

from orc.baseline import BaselineVerifier, GateClassification, GateSpec
from orc.preflight import PreflightConfig, PreflightService, ToolInfo
from orc.sandbox import SandboxRunner
from orc.state_machine import RunState
from orc.worktree import WorktreeManager
from tests.helpers import manifest_data


def git(repo: Path, *args: str) -> str:
    """fixture repoのgit stdoutを返す。"""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_preflight_worktree_sandbox_baseline_flow(repo: Path) -> None:
    """user tree不変のままStage 2からbaseline PASSまで到達する。"""
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "orc-test")
    git(repo, "config", "user.email", "orc-test@example.invalid")
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(repo, "add", "app.py")
    git(repo, "commit", "-m", "initial")
    base_commit = git(repo, "rev-parse", "HEAD")
    sample = manifest_data(repo, "run-1", 0)
    config = PreflightConfig(
        run_id="run-1",
        goal=sample["goal"],
        acceptance_criteria=sample["acceptance_criteria"],
        forbidden=sample["forbidden"],
        authority_sources=sample["authority_sources"],
        caps=sample["caps"],
        budget_source=sample["budget_source"],
        reviewer_policy=sample["reviewer_policy"],
        gates=sample["gates"],
        safety_policy_version=sample["safety_policy_version"],
        required_tools=("git",),
        optional_tools=(),
    )
    service = PreflightService(
        repo,
        tool_probe=lambda name: ToolInfo(name, True, f"/test/{name}", "test"),
    )
    stage1 = service.stage1(config)
    worktrees = WorktreeManager(repo, "run-1")
    plan = {
        "tasks": [
            {
                "task_id": "task-1",
                "role": "implementer",
                "objective": "app.pyを検証する",
                "path_scope": ["app.py"],
                "acceptance": ["smoke pass"],
                "depends_on": [],
                "size_estimate": {
                    "estimated_files": 1,
                    "estimated_diff_lines": 1,
                    "estimated_invocations": 1,
                },
                "scope_confidence": "high",
                "commands": ["pytest -q"],
            }
        ],
        "planner_size": "S",
        "deterministic_size": "S",
        "final_size": "S",
    }

    stage2 = service.stage2(
        stage1,
        plan,
        worktree_probe=lambda: worktrees.probe(base_commit),
    )
    baseline_tree = worktrees.create("baseline", base_commit)
    candidate_tree = worktrees.create("candidate", base_commit)
    decision = BaselineVerifier(stage1.store, SandboxRunner()).verify(
        GateSpec(
            "python-smoke",
            (
                sys.executable,
                "-c",
                "from pathlib import Path; raise SystemExit(0 if Path('app.py').is_file() else 1)",
            ),
            timeout_seconds=10,
        ),
        base_commit,
        baseline_tree.path,
        candidate_tree.path,
    )

    assert stage2.state is RunState.RUNNING
    assert decision.classification is GateClassification.PASS
    assert git(repo, "status", "--porcelain") == ""
