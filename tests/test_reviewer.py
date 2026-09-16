"""M7 reviewer fallback, DLP pre-spend, and artifact tests."""

import json
import os
from pathlib import Path

import pytest

from orc.budget import BudgetMeter
from orc.codex_adapter import CodexExecAdapter
from orc.lease import LeaseManager
from orc.review_adapters import ClaudeReviewAdapter, CodexReviewAdapter
from orc.review_runner import ReviewRunner
from orc.reviewer import ReviewerService
from orc.store import RunStateStore
from tests.helpers import manifest_data
from tests.m4_helpers import make_owned_worktree, make_store
from tests.m6_helpers import make_clean_ingestor
from tests.m7_helpers import (
    make_fake_claude,
    make_fake_codex_reviewer,
    make_review_bundle,
)


def _service(
    store,
    claude_path: Path,
    codex_path: Path,
    *,
    environment: dict[str, str],
):  # type: ignore[no-untyped-def]
    ingestor = make_clean_ingestor(store)
    claude = ReviewRunner(
        store,
        ClaudeReviewAdapter(claude_path, environment=environment),
        dlp_ingestor=ingestor,
    )
    codex = ReviewRunner(
        store,
        CodexReviewAdapter(
            CodexExecAdapter(codex_path, environment=environment)
        ),
        dlp_ingestor=ingestor,
    )
    return ReviewerService(store, ingestor, claude_runner=claude, codex_runner=codex)


def test_at14_claude_unavailable_falls_back_to_independent_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_store(repo)
    worktree = make_owned_worktree(store)
    bundle = make_review_bundle()
    marker = tmp_path / "codex.marker"
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MARKER", str(marker))
    environment = os.environ.copy()
    service = _service(
        store,
        make_fake_claude(tmp_path, "probe_fail", bundle.input_digest),
        make_fake_codex_reviewer(tmp_path, "valid", bundle.input_digest),
        environment=environment,
    )

    outcome = service.review(worktree, task_id="task-1", attempt=1, bundle=bundle)

    assert outcome.reviewed_by == "codex"
    assert outcome.report is not None
    assert outcome.report["verdict"] == "approve"
    assert marker.exists()
    artifact = store.run_dir / "tasks/task-1/attempt-1/review.json"
    assert artifact.stat().st_mode & 0o777 == 0o600
    assert json.loads(artifact.read_text(encoding="utf-8"))["reviewed_by"] == "codex"
    store.verify_integrity()


def test_both_reviewers_unavailable_records_none_and_requires_human(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_store(repo)
    worktree = make_owned_worktree(store)
    bundle = make_review_bundle()
    service = _service(
        store,
        make_fake_claude(tmp_path, "probe_fail", bundle.input_digest),
        make_fake_codex_reviewer(tmp_path, "review_fail", bundle.input_digest),
        environment=os.environ.copy(),
    )

    outcome = service.review(worktree, task_id="task-1", attempt=1, bundle=bundle)

    assert outcome.reviewed_by == "none"
    assert outcome.report is None
    assert outcome.external_review_skipped is True
    assert store.verify_events()[-1]["type"] == "external_review_skipped"


def test_dlp_detected_bundle_is_sent_to_neither_reviewer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_store(repo)
    worktree = make_owned_worktree(store)
    secret = "sk" + "_" + ("m6safe" * 6)
    bundle = make_review_bundle(objective=secret)
    claude_log = tmp_path / "claude.jsonl"
    codex_marker = tmp_path / "codex.marker"
    monkeypatch.setenv("FAKE_REVIEW_ARGV_LOG", str(claude_log))
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MARKER", str(codex_marker))
    service = _service(
        store,
        make_fake_claude(tmp_path, "valid", bundle.input_digest),
        make_fake_codex_reviewer(tmp_path, "valid", bundle.input_digest),
        environment=os.environ.copy(),
    )

    outcome = service.review(worktree, task_id="task-1", attempt=1, bundle=bundle)

    assert outcome.reviewed_by == "none"
    assert outcome.reason == "review_bundle_dlp_blocked"
    assert claude_log.exists() is False
    assert codex_marker.exists() is False
    assert secret not in json.dumps(store.verify_events())


def test_invalid_review_body_is_reduced_to_digest_before_none_fallback(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_store(repo)
    worktree = make_owned_worktree(store)
    bundle = make_review_bundle()
    service = _service(
        store,
        make_fake_claude(tmp_path, "invalid_review", bundle.input_digest),
        make_fake_codex_reviewer(tmp_path, "invalid_review", bundle.input_digest),
        environment=os.environ.copy(),
    )

    outcome = service.review(worktree, task_id="task-1", attempt=1, bundle=bundle)

    events = json.dumps(store.verify_events())
    assert outcome.reviewed_by == "none"
    assert "UNTRUSTED_REVIEW_BODY" not in events
    assert "UNTRUSTED_CODEX_REVIEW_BODY" not in events


def test_claude_probe_hard_budget_halts_without_codex_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = LeaseManager(repo)
    lease = manager.acquire("run-1")
    manifest = manifest_data(repo, "run-1", lease.fencing_token)
    manifest["caps"]["tokens_soft"] = 1
    manifest["caps"]["tokens_hard"] = 1
    store = RunStateStore(repo, "run-1", manager, lease)
    store.initialize(manifest)
    store.transition("lease_acquired")
    store.transition("checks_passed")
    store.transition("plan_valid")
    store.transition("size_sm")
    worktree = make_owned_worktree(store)
    bundle = make_review_bundle()
    codex_marker = tmp_path / "codex.marker"
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MARKER", str(codex_marker))
    environment = os.environ.copy()
    ingestor = make_clean_ingestor(store)
    budget = BudgetMeter(store)
    service = ReviewerService(
        store,
        ingestor,
        claude_runner=ReviewRunner(
            store,
            ClaudeReviewAdapter(
                make_fake_claude(tmp_path, "valid", bundle.input_digest),
                environment=environment,
            ),
            budget=budget,
            dlp_ingestor=ingestor,
        ),
        codex_runner=ReviewRunner(
            store,
            CodexReviewAdapter(
                CodexExecAdapter(
                    make_fake_codex_reviewer(tmp_path, "valid", bundle.input_digest),
                    environment=environment,
                )
            ),
            budget=budget,
            dlp_ingestor=ingestor,
        ),
    )

    outcome = service.review(worktree, task_id="task-1", attempt=1, bundle=bundle)

    assert outcome.reason == "review_budget_blocked"
    assert store.read_manifest()["state"] == "HALTED"
    assert codex_marker.exists() is False
