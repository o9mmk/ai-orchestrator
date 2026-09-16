"""M5 Planner one-shot, retry, context, and sizing acceptance tests."""

import copy
import json
import os
from pathlib import Path

import pytest

from orc.budget import BudgetMeter
from orc.codex_adapter import CodexExecAdapter
from orc.manager_context import ManagerContextBuilder, ValidatedVariable
from orc.planner import PlannerService
from orc.state_machine import RunState
from orc.usage import BudgetSource, UsageValue
from tests.m5_helpers import (
    make_fake_planner,
    make_planning_store,
    pinned_context,
    planner_worktree,
    valid_plan,
)
from tests.m6_helpers import (
    FixtureClearanceRegistry,
    dummy_api_key,
    make_clean_ingestor,
)


def character_estimator(value: str) -> UsageValue:
    """Deterministic test estimator."""
    return UsageValue(len(value), BudgetSource.COUNT_PROXY, "test_characters")


def _service(store, executable: Path) -> PlannerService:  # type: ignore[no-untyped-def]
    return PlannerService(
        store,
        CodexExecAdapter(executable, environment=os.environ.copy()),
        BudgetMeter(store),
        ManagerContextBuilder(store, estimator=character_estimator),
        term_grace_seconds=0.05,
        poll_interval=0.005,
        dlp_ingestor=make_clean_ingestor(store),
    )


def _fake_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worktree: Path,
) -> Path:
    counter = tmp_path / "planner-counter"
    monkeypatch.setenv("FAKE_PLANNER_COUNTER", str(counter))
    monkeypatch.setenv("FAKE_EXEC_MARKER", str(worktree / "spawned.marker"))
    return counter


def _plan_variable(plan, sequence: int, store):  # type: ignore[no-untyped-def]
    assessment = make_clean_ingestor(store).assess_manager_payload(
        "plan", f"variable-{sequence}", plan
    )
    assert assessment.clearance is not None
    return ValidatedVariable.from_plan(
        plan,
        sequence=sequence,
        clearance=assessment.clearance,
        clearance_registry=store,
    )


def test_planner_valid_plan_is_sized_saved_evented_and_checkpointed(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid Planner output is revalidated, finalized, and persisted once."""
    # Arrange
    (repo / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
    store = make_planning_store(repo)
    worktree = planner_worktree(store)
    counter = _fake_env(tmp_path, monkeypatch, worktree.path)
    service = _service(store, make_fake_planner(tmp_path, "valid"))

    # Act
    outcome = service.generate(
        worktree,
        pinned_context(),
        model_window_tokens=100_000,
    )

    # Assert
    assert outcome.status == "VALIDATED"
    assert outcome.attempts == 1
    assert outcome.plan is not None
    assert outcome.plan["final_size"] == "S"
    assert int(counter.read_text(encoding="utf-8")) == 1
    artifact = json.loads(store.plan_path.read_text(encoding="utf-8"))
    assert artifact == outcome.plan
    events = store.verify_events()
    assert any(event["type"] == "plan_recorded" for event in events)
    assert any(event["type"] == "manager_context_built" for event in events)
    store.verify_integrity()


def test_planner_size_uses_safe_max_with_deterministic_classifier(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dependency manifest scope raises Planner S to final L."""
    # Arrange
    (repo / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    store = make_planning_store(repo)
    worktree = planner_worktree(store)
    _fake_env(tmp_path, monkeypatch, worktree.path)
    monkeypatch.setenv("FAKE_PLAN_SCOPE", "pyproject.toml")

    # Act
    outcome = _service(store, make_fake_planner(tmp_path, "valid")).generate(
        worktree,
        pinned_context(),
        model_window_tokens=100_000,
    )

    # Assert
    assert outcome.plan is not None
    assert outcome.plan["planner_size"] == "S"
    assert outcome.plan["deterministic_size"] == "L"
    assert outcome.plan["final_size"] == "L"


def test_recorded_planner_plan_is_immutable(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later Manager path cannot silently replace checkpointed Planner output."""
    store = make_planning_store(repo)
    worktree = planner_worktree(store)
    _fake_env(tmp_path, monkeypatch, worktree.path)
    outcome = _service(store, make_fake_planner(tmp_path, "valid")).generate(
        worktree,
        pinned_context(),
        model_window_tokens=100_000,
    )
    assert outcome.plan is not None
    replacement = copy.deepcopy(outcome.plan)
    replacement["tasks"][0]["objective"] = "different valid objective"
    assessment = make_clean_ingestor(store).assess_manager_payload(
        "plan", "replacement-plan", replacement
    )
    assert assessment.clearance is not None

    with pytest.raises(FileExistsError, match="immutable"):
        store.record_plan(
            replacement,
            replacement["final_size"],
            clearance=assessment.clearance,
        )

    assert json.loads(store.plan_path.read_text(encoding="utf-8")) == outcome.plan


@pytest.mark.parametrize(
    ("mode", "forbidden_body"),
    [
        ("invalid_json", "UNTRUSTED_INVALID_PLAN_BODY"),
        ("invalid_schema", "UNTRUSTED_SCHEMA_PLAN_BODY"),
    ],
)
def test_invalid_plans_are_not_repaired_or_forwarded_and_fail_after_two_attempts(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    forbidden_body: str,
) -> None:
    """Two invalid Planner bodies become digest-only evidence and FAILED."""
    # Arrange
    store = make_planning_store(repo)
    worktree = planner_worktree(store)
    counter = _fake_env(tmp_path, monkeypatch, worktree.path)

    # Act
    outcome = _service(store, make_fake_planner(tmp_path, mode)).generate(
        worktree,
        pinned_context(),
        model_window_tokens=100_000,
    )

    # Assert
    assert outcome.status == "PLAN_FAILED"
    assert outcome.attempts == 2
    assert outcome.plan is None
    assert int(counter.read_text(encoding="utf-8")) == 2
    assert store.read_manifest()["state"] == RunState.FAILED.value
    assert store.plan_path.exists() is False
    serialized = json.dumps(store.verify_events(), ensure_ascii=False)
    assert forbidden_body not in serialized
    assert forbidden_body not in repr(outcome)
    assert sum(event["type"] == "planner_attempt_failed" for event in store.verify_events()) == 2


def test_invalid_first_attempt_can_retry_once_to_valid_plan(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bounded second attempt can succeed without interpreting attempt one."""
    store = make_planning_store(repo)
    worktree = planner_worktree(store)
    counter = _fake_env(tmp_path, monkeypatch, worktree.path)

    outcome = _service(store, make_fake_planner(tmp_path, "invalid_then_valid")).generate(
        worktree,
        pinned_context(),
        model_window_tokens=100_000,
    )

    assert outcome.status == "VALIDATED"
    assert outcome.attempts == 2
    assert int(counter.read_text(encoding="utf-8")) == 2


def test_pinned_overflow_refuses_with_zero_budget_and_no_llm(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """goal_too_large records REFUSED before Planner reservation or spawn."""
    # Arrange
    store = make_planning_store(repo, cap_overrides={"manager_input_tokens_hard": 20})
    worktree = planner_worktree(store)
    counter = _fake_env(tmp_path, monkeypatch, worktree.path)
    service = _service(store, make_fake_planner(tmp_path, "valid"))

    # Act
    outcome = service.generate(
        worktree,
        pinned_context(goal="x" * 500),
        model_window_tokens=100_000,
    )

    # Assert
    assert outcome.status == "REFUSED"
    assert outcome.reason == "goal_too_large"
    assert counter.exists() is False
    checkpoint = json.loads(store.checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["budget"]["tokens_used"] == 0
    assert checkpoint["budget"]["child_invocations"] == 0
    assert checkpoint["budget"]["manager_calls"] == 0
    assert store.read_manifest()["state"] == RunState.REFUSED.value


def test_unknown_model_window_refuses_before_spawn(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Planner does not guess the initial child model window."""
    store = make_planning_store(repo)
    worktree = planner_worktree(store)
    counter = _fake_env(tmp_path, monkeypatch, worktree.path)

    outcome = _service(store, make_fake_planner(tmp_path, "valid")).generate(
        worktree,
        pinned_context(),
        model_window_tokens=None,
    )

    assert outcome.status == "REFUSED"
    assert outcome.reason == "model_window_unknown"
    assert counter.exists() is False


def test_pinned_secret_is_dlp_blocked_before_spawn_and_budget(
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_planning_store(repo)
    worktree = planner_worktree(store)
    counter = _fake_env(tmp_path, monkeypatch, worktree.path)
    secret = dummy_api_key()

    outcome = _service(store, make_fake_planner(tmp_path, "valid")).generate(
        worktree,
        pinned_context(goal="do not forward " + secret),
        model_window_tokens=100_000,
    )

    assert outcome.status == "DLP_BLOCKED"
    assert counter.exists() is False
    assert store.read_budget()["child_invocations"] == 0
    assert secret not in json.dumps(store.verify_events())


def test_canonical_plan_dlp_block_stops_without_retry(
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unicode-escaped Planner content is rescanned after parse and never retried."""
    store = make_planning_store(repo)
    worktree = planner_worktree(store)
    counter = _fake_env(tmp_path, monkeypatch, worktree.path)
    secret = dummy_api_key()

    outcome = _service(store, make_fake_planner(tmp_path, "escaped_plan_secret")).generate(
        worktree,
        pinned_context(),
        model_window_tokens=100_000,
    )

    assert outcome.status == "DLP_BLOCKED"
    assert outcome.attempts == 1
    assert int(counter.read_text(encoding="utf-8")) == 1
    assert store.plan_path.exists() is False
    assert secret not in json.dumps(store.verify_events())


def test_stream_dlp_block_stops_planner_without_retry(
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redacted Planner stream is a terminal DLP boundary, not a retry reason."""
    store = make_planning_store(repo)
    worktree = planner_worktree(store)
    counter = _fake_env(tmp_path, monkeypatch, worktree.path)

    outcome = _service(store, make_fake_planner(tmp_path, "stream_secret")).generate(
        worktree,
        pinned_context(),
        model_window_tokens=100_000,
    )

    assert outcome.status == "DLP_BLOCKED"
    assert outcome.attempts == 1
    assert int(counter.read_text(encoding="utf-8")) == 1


def test_planner_records_only_count_and_digests_for_dropped_variables(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Context truncation evidence excludes every dropped variable body."""
    # Arrange
    builder = ManagerContextBuilder(
        FixtureClearanceRegistry(), estimator=character_estimator
    )
    pinned = pinned_context()
    pinned_only = builder.build(pinned, (), 100_000, 100_000)
    store = make_planning_store(
        repo,
        cap_overrides={"manager_input_tokens_hard": pinned_only.estimate.tokens},
    )
    worktree = planner_worktree(store)
    _fake_env(tmp_path, monkeypatch, worktree.path)
    variables = (
        _plan_variable(valid_plan(planner_size="M"), 1, store),
        _plan_variable(valid_plan(planner_size="L"), 2, store),
    )

    # Act
    outcome = _service(store, make_fake_planner(tmp_path, "valid")).generate(
        worktree,
        pinned,
        model_window_tokens=100_000,
        variables=variables,
    )

    # Assert
    assert outcome.status == "VALIDATED"
    context_event = next(
        event for event in store.verify_events() if event["type"] == "manager_context_built"
    )
    assert context_event["data"]["dropped_count"] == 2
    assert context_event["data"]["dropped_digests"] == [
        variables[0].digest,
        variables[1].digest,
    ]
    assert set(context_event["data"]) == {
        "prompt_digest",
        "estimated_tokens",
        "estimator",
        "budget_source",
        "dropped_count",
        "dropped_digests",
    }


def test_soft_budget_stops_planner_before_spawn(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Planner shares the pre-spawn child and Manager call caps."""
    store = make_planning_store(
        repo,
        cap_overrides={"child_invocations_soft": 1, "manager_calls_soft": 1},
    )
    BudgetMeter(store).reserve_planner()
    worktree = planner_worktree(store)
    counter = _fake_env(tmp_path, monkeypatch, worktree.path)

    outcome = _service(store, make_fake_planner(tmp_path, "valid")).generate(
        worktree,
        pinned_context(),
        model_window_tokens=100_000,
    )

    assert outcome.status == "HALTED"
    assert outcome.reason == "budget_soft"
    assert counter.exists() is False
    assert store.read_manifest()["state"] == RunState.HALTED.value


@pytest.mark.parametrize(
    ("tokens_soft", "tokens_hard", "expected_reason"),
    [(1, 10_000, "budget_soft"), (1, 1, "budget_hard")],
)
def test_completed_planner_usage_halts_before_task_launch(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tokens_soft: int,
    tokens_hard: int,
    expected_reason: str,
) -> None:
    """Final usage can preserve a valid plan while halting all pending tasks."""
    store = make_planning_store(
        repo,
        cap_overrides={"tokens_soft": tokens_soft, "tokens_hard": tokens_hard},
    )
    worktree = planner_worktree(store)
    _fake_env(tmp_path, monkeypatch, worktree.path)

    outcome = _service(store, make_fake_planner(tmp_path, "valid")).generate(
        worktree,
        pinned_context(),
        model_window_tokens=100_000,
    )

    assert outcome.status == "HALTED"
    assert outcome.reason == expected_reason
    assert outcome.plan is not None
    assert store.plan_path.exists()
    assert store.read_manifest()["state"] == RunState.HALTED.value
