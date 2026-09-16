"""M7 Claude/Codex reviewer adapter capability and isolation tests."""

import json
import os
from pathlib import Path

import pytest

from orc.errors import ClaudeCapabilityError
from orc.review_adapters import ClaudeReviewAdapter
from tests.m4_helpers import make_owned_worktree, make_store
from tests.m7_helpers import make_fake_claude, make_review_bundle


def test_claude_probe_runs_minimal_call_once_and_requires_safe_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_store(repo)
    worktree = make_owned_worktree(store)
    bundle = make_review_bundle()
    argv_log = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_REVIEW_ARGV_LOG", str(argv_log))
    adapter = ClaudeReviewAdapter(
        make_fake_claude(tmp_path, "valid", bundle.input_digest),
        environment=os.environ.copy(),
    )

    first = adapter.probe()
    second = adapter.probe()

    assert first == second
    commands = [json.loads(line) for line in argv_log.read_text(encoding="utf-8").splitlines()]
    assert len(commands) == 1
    probe_args = commands[0]
    assert "--safe-mode" in probe_args
    assert "--strict-mcp-config" in probe_args
    assert "--no-session-persistence" in probe_args
    assert probe_args[probe_args.index("--tools") + 1] == ""
    assert worktree.path.exists()


def test_missing_claude_flags_fail_before_model_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = make_review_bundle()
    argv_log = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_REVIEW_ARGV_LOG", str(argv_log))
    adapter = ClaudeReviewAdapter(
        make_fake_claude(tmp_path, "missing_flags", bundle.input_digest),
        environment=os.environ.copy(),
    )

    with pytest.raises(ClaudeCapabilityError, match="required flags unavailable"):
        adapter.probe()

    assert argv_log.exists() is False
