"""二段階PreflightとAT-11 dirty overlapのテスト。"""

import json
import os
import subprocess
from pathlib import Path

import pytest
from jsonschema import ValidationError

from orc.budget import BudgetMeter
from orc.codex_adapter import CodexExecAdapter
from orc.errors import DuplicateRunId, PreflightError
from orc.lease import Lease, LeaseManager
from orc.manager_context import ManagerContextBuilder
from orc.paths import StatePaths
from orc.planner import PlannerService
from orc.preflight import PreflightConfig, PreflightService, ToolInfo
from orc.session import release_run
from orc.state_machine import RunState
from orc.store import RunStateStore
from tests.helpers import manifest_data
from tests.m5_helpers import make_fake_planner, pinned_context, planner_worktree
from tests.m6_helpers import dummy_api_key, make_clean_ingestor


def git(repo: Path, *args: str) -> str:
    """fixture repoでgit commandを実行する。"""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def git_repo(repo: Path) -> Path:
    """1 commitを持つisolated git repoを返す。"""
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "orc-test")
    git(repo, "config", "user.email", "orc-test@example.invalid")
    (repo / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "other.py").write_text("VALUE = 2\n", encoding="utf-8")
    git(repo, "add", "tracked.py", "other.py")
    git(repo, "commit", "-m", "initial")
    return repo


def config(repo: Path, run_id: str) -> PreflightConfig:
    """全必須manifest固定値を持つPreflight configを返す。"""
    sample = manifest_data(repo, run_id, 0)
    return PreflightConfig(
        run_id=run_id,
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
        optional_tools=("codex", "claude", "pytest"),
    )


def tool_probe(name: str) -> ToolInfo:
    """外部toolの決定論的test double。"""
    return ToolInfo(name=name, available=True, path=f"/test/{name}", version="test-1")


def plan(path_scope: list[str], *, command: str = "pytest -q") -> dict[str, object]:
    """§9の全必須フィールドを持つplanを返す。"""
    return {
        "tasks": [
            {
                "task_id": "task-1",
                "role": "implementer",
                "objective": "対象を変更する",
                "path_scope": path_scope,
                "acceptance": ["pytestが成功する"],
                "depends_on": [],
                "size_estimate": {
                    "estimated_files": len(path_scope),
                    "estimated_diff_lines": 20,
                    "estimated_invocations": 1,
                },
                "scope_confidence": "high",
                "commands": [command],
            }
        ],
        "planner_size": "S",
        "deterministic_size": "S",
        "final_size": "S",
    }


def test_stage1_records_git_tools_authority_and_zero_budget(git_repo: Path) -> None:
    """課金前Stage 1がrepo事実と固定policyをmanifest/checkpointへ保存する。"""
    service = PreflightService(git_repo, tool_probe=tool_probe)

    result = service.stage1(config(git_repo, "run-1"))

    assert result.state is RunState.PLANNING
    assert result.git_facts.branch == "main"
    assert result.git_facts.head == git(git_repo, "rev-parse", "HEAD")
    assert set(result.tools) == {"git", "codex", "claude", "pytest"}
    checkpoint = result.store.verify_integrity()
    assert checkpoint.budget["tokens_used"] == 0
    assert checkpoint.budget["invocations"] == 0


def test_duplicate_run_id_is_rejected_before_lease_or_existing_run_change(
    git_repo: Path,
) -> None:
    """既存run-idは拒否artifactやleaseを作らず、既存bytesを変更しない。"""
    service = PreflightService(git_repo, tool_probe=tool_probe)
    created = service.stage1(config(git_repo, "run-1"))
    assert created.store is not None
    release_run(created.store)
    paths = StatePaths.for_run(git_repo, "run-1")
    before = {
        path.relative_to(paths.run_dir): path.read_bytes()
        for path in paths.run_dir.rglob("*")
        if path.is_file()
    }

    with pytest.raises(DuplicateRunId, match="duplicate_run_id"):
        service.stage1(config(git_repo, "run-1"))

    after = {
        path.relative_to(paths.run_dir): path.read_bytes()
        for path in paths.run_dir.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert (paths.lock_dir / "lease.json").exists() is False


def test_state_root_inside_repo_is_rejected_before_any_state_write(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """repo配下のORC_STATE_DIRはdirectory作成前にfail-fastする。"""
    configured = git_repo / ".orc-state"
    monkeypatch.setenv("ORC_STATE_DIR", str(configured))

    with pytest.raises(PreflightError, match="outside repository"):
        PreflightService(git_repo, tool_probe=tool_probe).stage1(config(git_repo, "run-1"))

    assert configured.exists() is False


def test_stage1_failure_after_lease_releases_without_masking_primary(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_initialize(self: RunStateStore, manifest: dict[str, object]) -> None:
        raise KeyboardInterrupt("primary-stage1-failure")

    monkeypatch.setattr(RunStateStore, "initialize", fail_initialize)
    service = PreflightService(git_repo, tool_probe=tool_probe)

    with pytest.raises(KeyboardInterrupt, match="primary-stage1-failure"):
        service.stage1(config(git_repo, "run-1"))

    paths = StatePaths.for_run(git_repo, "run-1")
    assert (paths.lock_dir / "lease.json").exists() is False


def test_stage1_cleanup_failure_is_logged_without_masking_primary(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail_initialize(self: RunStateStore, manifest: dict[str, object]) -> None:
        raise ValueError("primary-stage1-failure")

    def fail_release(self: LeaseManager, lease: Lease) -> None:
        raise RuntimeError("cleanup-stage1-failure")

    monkeypatch.setattr(RunStateStore, "initialize", fail_initialize)
    monkeypatch.setattr(LeaseManager, "release", fail_release)

    with pytest.raises(ValueError, match="primary-stage1-failure") as caught:
        PreflightService(git_repo, tool_probe=tool_probe).stage1(config(git_repo, "run-1"))

    assert any("RuntimeError" in note for note in caught.value.__notes__)
    assert "stage1 lease cleanup failed" in caplog.text


def test_stage1_uses_design_budget_defaults_when_caps_are_omitted(git_repo: Path) -> None:
    """FINAL_DESIGN §7.1 defaults are fixed into the manifest, not inferred later."""
    # Arrange
    configured = config(git_repo, "run-1")
    defaulted = PreflightConfig(
        run_id=configured.run_id,
        goal=configured.goal,
        acceptance_criteria=configured.acceptance_criteria,
        forbidden=configured.forbidden,
        authority_sources=configured.authority_sources,
        budget_source=configured.budget_source,
        reviewer_policy=configured.reviewer_policy,
        gates=configured.gates,
        safety_policy_version=configured.safety_policy_version,
        required_tools=("git",),
        optional_tools=(),
    )

    # Act
    result = PreflightService(git_repo, tool_probe=tool_probe).stage1(defaulted)

    # Assert
    assert result.store is not None
    caps = result.store.read_manifest()["caps"]
    assert caps["tokens_soft"] == 2_000_000
    assert caps["tokens_hard"] == 4_000_000
    assert caps["manager_input_tokens_hard"] == 20_000
    assert caps["timeouts_seconds"]["implement"] == 1200


def test_planner_to_preflight2_keeps_one_immutable_plan_record(
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Planner output flows into Stage 2 without a second overwrite event."""
    # Arrange
    service = PreflightService(git_repo, tool_probe=tool_probe)
    stage1 = service.stage1(config(git_repo, "run-1"))
    assert stage1.store is not None
    worktree = planner_worktree(stage1.store)
    monkeypatch.setenv("FAKE_PLANNER_COUNTER", str(tmp_path / "planner-counter"))
    monkeypatch.setenv("FAKE_EXEC_MARKER", str(worktree.path / "spawned.marker"))
    planner = PlannerService(
        stage1.store,
        CodexExecAdapter(
            make_fake_planner(tmp_path, "valid"), environment=os.environ.copy()
        ),
        BudgetMeter(stage1.store),
        ManagerContextBuilder(stage1.store),
        dlp_ingestor=make_clean_ingestor(stage1.store),
    )

    # Act
    planned = planner.generate(worktree, pinned_context(), model_window_tokens=100_000)
    assert planned.plan is not None
    result = service.stage2(stage1, planned.plan, worktree_probe=lambda: True)

    # Assert
    assert result.state is RunState.RUNNING
    assert sum(
        event["type"] == "plan_recorded" for event in stage1.store.verify_events()
    ) == 1


def test_direct_preflight_plan_is_dlp_scanned_before_persistence(git_repo: Path) -> None:
    """Stage 2 cannot persist a schema-valid plan that bypassed Planner DLP."""
    service = PreflightService(
        git_repo,
        tool_probe=tool_probe,
        dlp_ingestor_factory=make_clean_ingestor,
    )
    stage1 = service.stage1(config(git_repo, "run-1"))
    polluted = plan(["tracked.py"])
    polluted["tasks"][0]["objective"] = dummy_api_key()  # type: ignore[index]

    with pytest.raises(PreflightError, match="plan_dlp_blocked"):
        service.stage2(stage1, polluted, worktree_probe=lambda: True)

    assert stage1.store is not None
    assert stage1.store.plan_path.exists() is False


@pytest.mark.parametrize(
    "polluted",
    [
        {"unexpected": dummy_api_key()},
        plan([dummy_api_key()]),
    ],
    ids=("schema-invalid", "secret-scope"),
)
def test_direct_preflight_scans_raw_plan_before_validation_or_scope_resolution(
    git_repo: Path,
    polluted: dict[str, object],
) -> None:
    """schema errorやscope errorより先に入力planのsecretを遮断する。"""
    secret = dummy_api_key()
    service = PreflightService(
        git_repo,
        tool_probe=tool_probe,
        dlp_ingestor_factory=make_clean_ingestor,
    )
    stage1 = service.stage1(config(git_repo, "run-1"))

    with pytest.raises(PreflightError, match="plan_dlp_blocked:input") as caught:
        service.stage2(stage1, polluted, worktree_probe=lambda: True)

    assert secret not in str(caught.value)
    assert stage1.store is not None
    assert stage1.store.plan_path.exists() is False


def test_at17_lease_contender_records_refused_with_no_llm_event(git_repo: Path) -> None:
    """後発runはREFUSEDを監査記録し、child/LLM eventとbudget消費を残さない。"""
    first = PreflightService(git_repo, tool_probe=tool_probe)
    second = PreflightService(git_repo, tool_probe=tool_probe)
    first.stage1(config(git_repo, "run-1"))

    refused = second.stage1(config(git_repo, "run-2"))

    assert refused.state is RunState.REFUSED
    events = [json.loads(line) for line in refused.events_path.read_text(encoding="utf-8").splitlines()]
    assert all(event["type"] not in {"child_spawn", "llm_call"} for event in events)
    checkpoint = json.loads(refused.checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["budget"]["tokens_used"] == 0
    assert checkpoint["budget"]["invocations"] == 0


def test_at11_dirty_scope_overlap_is_refused(git_repo: Path) -> None:
    """dirty変更とplan scopeが重複すればStage 2でREFUSEDにする。"""
    (git_repo / "tracked.py").write_text("VALUE = 9\n", encoding="utf-8")
    service = PreflightService(git_repo, tool_probe=tool_probe)
    stage1 = service.stage1(config(git_repo, "run-1"))

    result = service.stage2(stage1, plan(["tracked.py"]), worktree_probe=lambda: True)

    assert result.state is RunState.REFUSED
    assert result.reason == "dirty_overlap"
    assert result.dirty_overlaps == ("tracked.py",)


def test_dirty_nonoverlap_continues_with_warning(git_repo: Path) -> None:
    """dirtyがscope外なら警告を保持してS/Mフローを続行する。"""
    (git_repo / "other.py").write_text("VALUE = 9\n", encoding="utf-8")
    service = PreflightService(git_repo, tool_probe=tool_probe)
    stage1 = service.stage1(config(git_repo, "run-1"))

    result = service.stage2(stage1, plan(["tracked.py"]), worktree_probe=lambda: True)

    assert result.state is RunState.RUNNING
    assert result.dirty_warning == ("other.py",)


def test_outside_repo_scope_is_fail_loud(git_repo: Path) -> None:
    """realpathがrepo外へ出るscopeをLLM出力の安全境界に使わない。"""
    service = PreflightService(git_repo, tool_probe=tool_probe)
    stage1 = service.stage1(config(git_repo, "run-1"))

    with pytest.raises(PreflightError, match="outside repo"):
        service.stage2(stage1, plan(["../escape.py"]), worktree_probe=lambda: True)


def test_secret_scope_is_denied_before_read(git_repo: Path) -> None:
    """秘密path deny patternは内容を読まずにStage 2で遮断する。"""
    service = PreflightService(git_repo, tool_probe=tool_probe)
    stage1 = service.stage1(config(git_repo, "run-1"))

    with pytest.raises(PreflightError, match="denied path scope"):
        service.stage2(stage1, plan([".env.example"]), worktree_probe=lambda: True)


def test_unknown_command_escalates_to_l_approval(git_repo: Path) -> None:
    """副作用不明commandはLへ昇格し開始前承認へ送る。"""
    service = PreflightService(git_repo, tool_probe=tool_probe)
    stage1 = service.stage1(config(git_repo, "run-1"))

    result = service.stage2(
        stage1,
        plan(["tracked.py"], command="custom-build --fast"),
        worktree_probe=lambda: True,
    )

    assert result.state is RunState.AWAITING_START_APPROVAL
    assert result.final_size == "L"


def test_worktree_probe_failure_is_refused(git_repo: Path) -> None:
    """worktree作成可否を実検査できなければREFUSEDへ遷移する。"""
    service = PreflightService(git_repo, tool_probe=tool_probe)
    stage1 = service.stage1(config(git_repo, "run-1"))

    result = service.stage2(
        stage1,
        plan(["tracked.py"]),
        worktree_probe=lambda: False,
    )

    assert result.state is RunState.REFUSED
    assert result.reason == "worktree_probe_failed"


def test_multiple_repo_plan_is_xl_plan_only(git_repo: Path) -> None:
    """複数repo flagは実装せずHALTED(xl_plan_only)へ送る。"""
    service = PreflightService(git_repo, tool_probe=tool_probe)
    stage1 = service.stage1(config(git_repo, "run-1"))

    result = service.stage2(
        stage1,
        plan(["tracked.py"]),
        worktree_probe=lambda: True,
        multiple_repos=True,
    )

    assert result.state is RunState.HALTED
    assert result.reason == "xl_plan_only"


def test_plan_missing_required_field_is_schema_error(git_repo: Path) -> None:
    """plan必須field欠落を補修せずjsonschema errorにする。"""
    invalid = plan(["tracked.py"])
    del invalid["tasks"][0]["scope_confidence"]  # type: ignore[index]
    service = PreflightService(git_repo, tool_probe=tool_probe)
    stage1 = service.stage1(config(git_repo, "run-1"))

    with pytest.raises(ValidationError):
        service.stage2(stage1, invalid, worktree_probe=lambda: True)


def test_plan_duplicate_task_id_is_schema_error(git_repo: Path) -> None:
    """依存schedulerの一意性を壊すtask_id重複を受理しない。"""
    invalid = plan(["tracked.py"])
    invalid["tasks"].append(invalid["tasks"][0].copy())  # type: ignore[index]
    service = PreflightService(git_repo, tool_probe=tool_probe)
    stage1 = service.stage1(config(git_repo, "run-1"))

    with pytest.raises(ValidationError, match="task_id must be unique"):
        service.stage2(stage1, invalid, worktree_probe=lambda: True)


def test_stage2_snapshot_is_audited(git_repo: Path) -> None:
    """Stage 2のscope/size判定根拠をeventsへ固定する。"""
    service = PreflightService(git_repo, tool_probe=tool_probe)
    stage1 = service.stage1(config(git_repo, "run-1"))

    service.stage2(stage1, plan(["tracked.py"]), worktree_probe=lambda: True)

    events = stage1.store.verify_events()
    snapshots = [event for event in events if event["type"] == "preflight2_snapshot"]
    assert len(snapshots) == 1
    assert snapshots[0]["data"]["final_size"] == "S"


def test_missing_required_tool_refuses_after_lease(git_repo: Path) -> None:
    """required tool不在はPREFLIGHT1からREFUSEDへ遷移して明示する。"""

    def missing_probe(name: str) -> ToolInfo:
        if name == "git":
            return ToolInfo(name, False, None, None)
        return tool_probe(name)

    service = PreflightService(git_repo, tool_probe=missing_probe)

    result = service.stage1(config(git_repo, "run-1"))

    assert result.state is RunState.REFUSED
    assert result.reason == "tool_missing"
