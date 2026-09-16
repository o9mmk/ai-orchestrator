"""CLI fail-fast and cleanup exception contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import orc.cli as cli_module
from orc.cli import build_parser, main
from orc.errors import LeaseLost
from orc.manager import discover_codex
from orc.paths import StatePaths
from tests.m9_helpers import make_repo


def test_run_help_explains_repeatable_caps_forbidden_and_planner_consumption(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["run", "--help"])
    help_text = capsys.readouterr().out

    assert exit_info.value.code == 0
    assert "KEY=JSON_INTEGER" in help_text
    assert "repeatable" in help_text
    assert "--forbidden .env" in help_text
    assert ".git/config" in help_text
    assert "required and consumed only by Planner" in help_text
    assert "Planner consumes one child" in help_text
    assert "invocation" in help_text
    assert "EXECUTABLE_PATH" in help_text


def test_explicit_codex_value_must_be_a_path_not_a_command_or_path_name() -> None:
    with pytest.raises(ValueError, match="executable path"):
        discover_codex("codex")
    with pytest.raises(ValueError, match="executable path"):
        discover_codex("codex --model unsafe")


def test_planner_hard_cap_below_two_fails_before_state_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    repo = make_repo(tmp_path / "repo")
    monkeypatch.setenv("ORC_STATE_DIR", str(tmp_path / "state"))

    exit_code = main(
        [
            "run",
            "--repo",
            str(repo),
            "--run-id",
            "run-1",
            "--goal",
            "bounded goal",
            "--acceptance",
            "bounded acceptance",
            "--model-window-tokens",
            "100000",
            "--cap",
            "child_invocations_soft=1",
            "--cap",
            "child_invocations_hard=1",
        ]
    )

    error = json.loads(capsys.readouterr().err)
    assert exit_code == 2
    assert error["reason"] == "Planner requires child_invocations_hard >= 2"
    assert StatePaths.for_run(repo, "run-1").run_dir.exists() is False


def test_cleanup_failure_does_not_mask_primary_and_is_not_silent_on_success(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail_release(_store: object) -> None:
        raise LeaseLost("lease_lost: cleanup")

    monkeypatch.setattr(cli_module, "release_run", fail_release)

    def primary_failure() -> None:
        try:
            raise ValueError("primary")
        finally:
            cli_module._release_preserving_primary(object())

    with pytest.raises(ValueError, match="primary") as caught:
        primary_failure()
    assert any("LeaseLost" in note for note in caught.value.__notes__)
    assert "lease release cleanup failed" in caplog.text

    with pytest.raises(LeaseLost, match="cleanup"):
        cli_module._release_preserving_primary(object())
