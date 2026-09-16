"""M9 user-facing CLI acceptance and explicit GC safety tests."""

import json
from pathlib import Path

from orc.cli import main
from orc.paths import StatePaths
from tests.m9_helpers import git, make_fake_codex, make_repo, plan


def test_at1_cli_e2e_stops_for_approval_without_touching_user_tree(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    repo = make_repo(tmp_path / "repo")
    state_root = tmp_path / "state"
    monkeypatch.setenv("ORC_STATE_DIR", str(state_root))
    fake = make_fake_codex(tmp_path / "fake-codex")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan()), encoding="utf-8")
    before = (repo / "app.py").read_bytes()
    before_status = git(repo, "status", "--porcelain=v1", "--untracked-files=all")

    exit_code = main(
        [
            "run",
            "--repo",
            str(repo),
            "--run-id",
            "run-1",
            "--goal",
            "Change VALUE from 1 to 2",
            "--acceptance",
            "candidate contains VALUE = 2",
            "--plan-file",
            str(plan_path),
            "--codex",
            str(fake),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0, captured.err
    payload = json.loads(captured.out)
    assert payload["state"] == "AWAITING_APPROVAL"
    assert payload["branch"] == "orc/run-1"
    assert payload["merge_command"] == "git merge --no-ff orc/run-1"
    assert (repo / "app.py").read_bytes() == before
    assert git(repo, "status", "--porcelain=v1", "--untracked-files=all") == before_status
    assert git(repo, "show", "orc/run-1:app.py") == "VALUE = 2"
    run_dir = StatePaths.for_run(repo, "run-1").run_dir
    assert (run_dir / "tasks/task-1/attempt-1/verify.json").is_file()
    summary = (run_dir / "summary.md").read_text(encoding="utf-8")
    assert "external_review_skipped: true" in summary
    assert "automatic_merge: false" in summary
    assert "automatic_push: false" in summary

    assert main(["status", "--repo", str(repo), "run-1"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["state"] == "AWAITING_APPROVAL"
    assert status["tasks"]["task-1"]["state"] == "DONE"

    assert main(["approve", "--repo", str(repo), "run-1"]) == 0
    approved = json.loads(capsys.readouterr().out)
    assert approved["state"] == "COMPLETED"
    assert git(repo, "branch", "--show-current") == "main"
    assert (repo / "app.py").read_bytes() == before


def test_gc_requires_exact_confirmation_and_retains_branch(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    repo = make_repo(tmp_path / "repo")
    monkeypatch.setenv("ORC_STATE_DIR", str(tmp_path / "state"))
    fake = make_fake_codex(tmp_path / "fake-codex")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan()), encoding="utf-8")
    run_args = [
        "run",
        "--repo",
        str(repo),
        "--run-id",
        "run-1",
        "--goal",
        "Change VALUE",
        "--acceptance",
        "candidate is ready",
        "--plan-file",
        str(plan_path),
        "--codex",
        str(fake),
    ]
    assert main(run_args) == 0
    capsys.readouterr()
    assert main(["approve", "--repo", str(repo), "run-1"]) == 0
    capsys.readouterr()
    run_dir = StatePaths.for_run(repo, "run-1").run_dir

    assert (
        main(
            [
                "gc",
                "--repo",
                str(repo),
                "run-1",
                "--confirm-run-id",
                "wrong-run",
            ]
        )
        == 2
    )
    capsys.readouterr()
    assert run_dir.is_dir()

    assert (
        main(
            [
                "gc",
                "--repo",
                str(repo),
                "run-1",
                "--confirm-run-id",
                "run-1",
            ]
        )
        == 0
    )
    deleted = json.loads(capsys.readouterr().out)
    assert deleted["run_deleted"] is True
    assert deleted["branch_retained"] == "orc/run-1"
    assert run_dir.exists() is False
    assert git(repo, "branch", "--list", "orc/run-1") == "orc/run-1"


def test_resume_creates_new_generation_and_does_not_rerun_done_task(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    repo = make_repo(tmp_path / "repo")
    monkeypatch.setenv("ORC_STATE_DIR", str(tmp_path / "state"))
    fake = make_fake_codex(tmp_path / "fake-codex")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan()), encoding="utf-8")
    run_args = [
        "run",
        "--repo",
        str(repo),
        "--run-id",
        "run-1",
        "--goal",
        "Change VALUE",
        "--acceptance",
        "candidate is ready",
        "--plan-file",
        str(plan_path),
        "--codex",
        str(fake),
    ]
    assert main(run_args) == 0
    capsys.readouterr()
    counter = Path(str(fake) + ".count")
    calls_before_resume = int(counter.read_text(encoding="utf-8"))
    assert main(["cancel", "--repo", str(repo), "run-1"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "resume",
                "--repo",
                str(repo),
                "run-1",
                "--new-run-id",
                "run-2",
                "--codex",
                str(fake),
            ]
        )
        == 0
    )

    resumed = json.loads(capsys.readouterr().out)
    assert resumed["state"] == "AWAITING_APPROVAL"
    assert resumed["branch"] == "orc/run-2"
    assert int(counter.read_text(encoding="utf-8")) == calls_before_resume
    status_exit = main(["status", "--repo", str(repo), "run-2"])
    status = json.loads(capsys.readouterr().out)
    assert status_exit == 0
    assert status["generation"] == 2
    assert status["tasks"]["task-1"]["state"] == "DONE"
    assert git(repo, "show", "orc/run-2:app.py") == "VALUE = 2"


def test_cli_nonpositive_cap_is_rejected_before_lease_or_spawn(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    repo = make_repo(tmp_path / "repo")
    monkeypatch.setenv("ORC_STATE_DIR", str(tmp_path / "state"))
    fake = make_fake_codex(tmp_path / "fake-codex")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan()), encoding="utf-8")

    exit_code = main(
        [
            "run",
            "--repo",
            str(repo),
            "--run-id",
            "run-soft",
            "--goal",
            "Change VALUE",
            "--acceptance",
            "candidate is ready",
            "--plan-file",
            str(plan_path),
            "--codex",
            str(fake),
            "--cap",
            "child_invocations_soft=0",
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 2
    assert error["error"] == "ValueError"
    assert "positive integer" in error["reason"]
    assert Path(str(fake) + ".count").exists() is False
    assert git(repo, "branch", "--list", "orc/run-soft") == ""
    assert StatePaths.for_run(repo, "run-soft").run_dir.exists() is False


def test_cli_hard_budget_halts_after_completed_child_without_integration(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    repo = make_repo(tmp_path / "repo")
    monkeypatch.setenv("ORC_STATE_DIR", str(tmp_path / "state"))
    fake = make_fake_codex(tmp_path / "fake-codex")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan()), encoding="utf-8")

    exit_code = main(
        [
            "run",
            "--repo",
            str(repo),
            "--run-id",
            "run-hard",
            "--goal",
            "Change VALUE",
            "--acceptance",
            "candidate is ready",
            "--plan-file",
            str(plan_path),
            "--codex",
            str(fake),
            "--cap",
            "tokens_soft=1",
            "--cap",
            "tokens_hard=1",
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result["state"] == "HALTED"
    assert int(Path(str(fake) + ".count").read_text(encoding="utf-8")) == 1
    assert git(repo, "branch", "--list", "orc/run-hard") == ""
    assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_l_run_requires_start_approval_then_continues_with_bounded_fallback(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    repo = make_repo(tmp_path / "repo")
    monkeypatch.setenv("ORC_STATE_DIR", str(tmp_path / "state"))
    fake = make_fake_codex(tmp_path / "fake-codex")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan(size="L")), encoding="utf-8")
    run_args = [
        "run",
        "--repo",
        str(repo),
        "--run-id",
        "run-l",
        "--goal",
        "Change VALUE",
        "--acceptance",
        "candidate is ready",
        "--plan-file",
        str(plan_path),
        "--codex",
        str(fake),
    ]

    assert main(run_args) == 0
    waiting = json.loads(capsys.readouterr().out)
    assert waiting["state"] == "AWAITING_START_APPROVAL"
    assert Path(str(fake) + ".count").exists() is False

    assert (
        main(
            [
                "approve",
                "--repo",
                str(repo),
                "run-l",
                "--codex",
                str(fake),
            ]
        )
        == 0
    )
    continued = json.loads(capsys.readouterr().out)
    assert continued["state"] == "AWAITING_APPROVAL"
    assert continued["branch"] == "orc/run-l"
    assert int(Path(str(fake) + ".count").read_text(encoding="utf-8")) == 2


def test_cli_failure_releases_lease_without_silently_repairing_plan(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    repo = make_repo(tmp_path / "repo")
    monkeypatch.setenv("ORC_STATE_DIR", str(tmp_path / "state"))
    fake = make_fake_codex(tmp_path / "fake-codex")
    invalid_plan = tmp_path / "invalid-plan.json"
    invalid_plan.write_text(json.dumps({"tasks": []}), encoding="utf-8")

    exit_code = main(
        [
            "run",
            "--repo",
            str(repo),
            "--run-id",
            "run-invalid",
            "--goal",
            "bounded goal",
            "--acceptance",
            "must fail loud",
            "--plan-file",
            str(invalid_plan),
            "--codex",
            str(fake),
        ]
    )

    assert exit_code == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "ValidationError"
    paths = StatePaths.for_run(repo, "run-invalid")
    assert (paths.lock_dir / "lease.json").exists() is False
    assert (paths.run_dir / "plan.json").exists() is False
