"""M8 new-generation resume integrity and reuse tests."""

import json
from pathlib import Path

import pytest

from orc.errors import ResumeBlocked, TamperDetected
from orc.resume import ResumeConfig, ResumeService
from tests.m8_helpers import (
    git,
    make_committed_repo,
    make_halted_store,
    run_files,
)


def _release(store) -> None:  # type: ignore[no-untyped-def]
    store.lease_manager.release(store.lease)


def test_resume_creates_new_generation_and_reuses_done_artifacts(
    tmp_path: Path,
) -> None:
    repo = make_committed_repo(tmp_path / "repo")
    source = make_halted_store(repo)
    before = run_files(source.run_dir)
    _release(source)

    outcome = ResumeService(repo).resume(
        "run-1",
        ResumeConfig(
            new_run_id="run-2",
            safety_policy_version="1.0",
            gates=("pytest", "gitleaks", "glassworm"),
        ),
    )

    manifest = outcome.store.read_manifest()
    checkpoint = outcome.store.verify_integrity()
    assert outcome.generation == 2
    assert manifest["parent_run_id"] == "run-1"
    assert manifest["resumed_from"] > 0
    assert manifest["fencing_token"] != source.fencing_token
    assert checkpoint.budget["tokens_used"] == 0
    assert checkpoint.budget["child_invocations"] == 0
    assert checkpoint.tasks["task-1"]["state"] == "DONE"
    assert checkpoint.tasks["task-1"]["reused_from"] == "run-1"
    assert checkpoint.tasks["task-1"]["reused_artifacts"]
    assert run_files(source.run_dir) == before


@pytest.mark.parametrize("target", ["manifest", "gates", "artifact", "events"])
def test_tamper_refuses_resume_without_new_generation(
    tmp_path: Path,
    target: str,
) -> None:
    repo = make_committed_repo(tmp_path / "repo")
    source = make_halted_store(repo)
    _release(source)
    if target in {"manifest", "gates"}:
        manifest = json.loads(source.manifest_path.read_text(encoding="utf-8"))
        if target == "manifest":
            manifest["goal"] = "tampered goal"
        else:
            manifest["gates"].append("tampered-gate")
        source.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif target == "artifact":
        (source.run_dir / "tasks/task-1/attempt-1/patch.diff").write_text(
            "tampered\n", encoding="utf-8"
        )
    else:
        lines = source.events_path.read_text(encoding="utf-8").splitlines()
        event = json.loads(lines[0])
        event["data"]["state"] = "COMPLETED"
        lines[0] = json.dumps(event)
        source.events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(TamperDetected, match="tamper_detected"):
        ResumeService(repo).resume(
            "run-1",
            ResumeConfig("run-2", "1.0", ("pytest", "gitleaks", "glassworm")),
        )

    assert (source.paths.root / "runs" / source.paths.repo_fp / "run-2").exists() is False


def test_at7_stale_head_refuses_resume_without_rebase_or_new_run(
    tmp_path: Path,
) -> None:
    repo = make_committed_repo(tmp_path / "repo")
    source = make_halted_store(repo)
    _release(source)
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    git(repo, "add", "app.py")
    git(repo, "commit", "-m", "advance head")

    with pytest.raises(ResumeBlocked, match="stale_head"):
        ResumeService(repo).resume(
            "run-1",
            ResumeConfig("run-2", "1.0", ("pytest", "gitleaks", "glassworm")),
        )

    assert git(repo, "log", "--oneline", "--all").count("advance head") == 1
    assert git(repo, "branch", "--list", "orc/*") == ""
    assert (source.paths.root / "runs" / source.paths.repo_fp / "run-2").exists() is False


def test_policy_gate_or_implicit_cap_raise_is_refused(tmp_path: Path) -> None:
    repo = make_committed_repo(tmp_path / "repo")
    source = make_halted_store(repo)
    parent_caps = source.read_manifest()["caps"]
    _release(source)

    with pytest.raises(ResumeBlocked, match="policy_or_gates_mismatch"):
        ResumeService(repo).resume(
            "run-1",
            ResumeConfig("run-2", "2.0", ("pytest", "gitleaks", "glassworm")),
        )

    raised = json.loads(json.dumps(parent_caps))
    raised["tokens_hard"] += 1
    with pytest.raises(ResumeBlocked, match="cap_raise_requires_approval"):
        ResumeService(repo).resume(
            "run-1",
            ResumeConfig(
                "run-2",
                "1.0",
                ("pytest", "gitleaks", "glassworm"),
                caps=raised,
            ),
        )


def test_missing_done_artifact_refuses_before_new_run_or_lease(tmp_path: Path) -> None:
    repo = make_committed_repo(tmp_path / "repo")
    source = make_halted_store(repo)
    checkpoint = json.loads(source.checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["artifact_digests"] = {
        path: digest
        for path, digest in checkpoint["artifact_digests"].items()
        if "tasks/task-1/" not in path
    }
    source.checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    (source.run_dir / "tasks/task-1/attempt-1/patch.diff").unlink()
    source.write_checkpoint(run_state="HALTED")
    _release(source)

    with pytest.raises(ResumeBlocked, match="done_task_artifacts_missing"):
        ResumeService(repo).resume(
            "run-1",
            ResumeConfig("run-2", "1.0", ("pytest", "gitleaks", "glassworm")),
        )

    assert (source.paths.root / "runs" / source.paths.repo_fp / "run-2").exists() is False
    assert source.lease_manager.lease_path.exists() is False
