"""M7 reviewer input allowlist and pre-spend sizing tests."""

from orc.review_bundle import MAX_REVIEW_INPUT_TOKENS, build_review_bundle


def test_review_bundle_contains_only_allowlisted_fields_and_stable_digest() -> None:
    first = build_review_bundle(
        task_id="task-1",
        objective="Implement the bounded change",
        acceptance=("tests pass",),
        patch="diff --git a/app.py b/app.py\n",
        related_context=("app.py:1-20",),
        verify={"gates": []},
    )
    second = build_review_bundle(
        task_id="task-1",
        objective="Implement the bounded change",
        acceptance=("tests pass",),
        patch="diff --git a/app.py b/app.py\n",
        related_context=("app.py:1-20",),
        verify={"gates": []},
    )

    assert set(first.payload) == {
        "task_id",
        "objective",
        "acceptance",
        "patch",
        "related_context",
        "verify",
    }
    assert first.input_digest == second.input_digest
    assert first.prompt == second.prompt
    assert first.estimated_tokens > 0


def test_review_bundle_marks_oversized_input_without_truncating_it() -> None:
    bundle = build_review_bundle(
        task_id="task-1",
        objective="x" * (MAX_REVIEW_INPUT_TOKENS * 4 + 1),
        acceptance=(),
        patch="",
        related_context=(),
        verify={},
    )

    assert bundle.estimated_tokens > MAX_REVIEW_INPUT_TOKENS
    assert len(bundle.payload["objective"]) == MAX_REVIEW_INPUT_TOKENS * 4 + 1
