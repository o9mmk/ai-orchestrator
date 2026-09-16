"""write scope(path_scope)とread scope(read_scope)の権限分離テスト。"""

from pathlib import Path
from typing import Any

import pytest

from orc.errors import PreflightError
from orc.plan_finalizer import finalize_plan
from orc.sizing import Size


def plan(
    path_scope: list[str],
    *,
    read_scope: list[str] | None = None,
    command: str = "pytest -q",
) -> dict[str, Any]:
    """§9必須フィールドを満たす最小planを返す。"""
    task: dict[str, Any] = {
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
    if read_scope is not None:
        task["read_scope"] = read_scope
    return {
        "tasks": [task],
        "planner_size": "S",
        "deterministic_size": "S",
        "final_size": "S",
    }


@pytest.fixture
def scope_repo(repo: Path) -> Path:
    """write対象1件とread専用manifestを持つrepoを返す。"""
    (repo / "calc.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    return repo


def test_read_only_manifest_does_not_escalate_size(scope_repo: Path) -> None:
    """読むだけのdependency manifestはescalation対象にしない。"""
    finalized = finalize_plan(
        scope_repo,
        plan(["calc.py"], read_scope=["pyproject.toml"]),
        invocation_cap=20,
    )

    assert finalized.decision.escalation_flags == ()
    assert finalized.decision.final_size is Size.S


def test_write_scope_manifest_still_escalates(scope_repo: Path) -> None:
    """書き換える宣言のdependency manifestは従来どおりLへ昇格する。"""
    finalized = finalize_plan(
        scope_repo,
        plan(["pyproject.toml"], read_scope=["calc.py"]),
        invocation_cap=20,
    )

    assert "dependency_manifest" in finalized.decision.escalation_flags
    assert finalized.decision.final_size is Size.L


def test_unresolved_read_glob_does_not_mark_scope_unknown(scope_repo: Path) -> None:
    """read scopeの未解決globはscope_unknownにしない(証跡には残す)。"""
    finalized = finalize_plan(
        scope_repo,
        plan(["calc.py"], read_scope=["tests/**"]),
        invocation_cap=20,
    )

    assert "scope_unknown" not in finalized.decision.escalation_flags
    assert finalized.read_resolution.unresolved == ("tests/**",)


def test_read_scope_never_becomes_write_allowlist(scope_repo: Path) -> None:
    """read scopeはpatch許可scopeへ混入しない。"""
    finalized = finalize_plan(
        scope_repo,
        plan(["calc.py"], read_scope=["pyproject.toml"]),
        invocation_cap=20,
    )

    assert finalized.resolution.resolved == ("calc.py",)
    assert "pyproject.toml" in finalized.read_resolution.resolved


def test_denied_read_scope_is_rejected(scope_repo: Path) -> None:
    """秘密deny patternはread scopeでも拒否する。"""
    with pytest.raises(PreflightError):
        finalize_plan(
            scope_repo,
            plan(["calc.py"], read_scope=[".env"]),
            invocation_cap=20,
        )


def test_plan_without_read_scope_keeps_previous_behavior(scope_repo: Path) -> None:
    """read_scope未指定のplanは従来と同じ判定を保つ。"""
    finalized = finalize_plan(scope_repo, plan(["pyproject.toml"]), invocation_cap=20)

    assert "dependency_manifest" in finalized.decision.escalation_flags
    assert finalized.read_resolution.resolved == ()


def test_child_contract_pins_task_id_and_read_scope() -> None:
    """子契約にtask_idを明示する(結果schemaが完全一致を要求するため推測させない)。"""
    from orc.manager import _task_prompt

    prompt = _task_prompt(
        {"base_commit": "0" * 40, "forbidden": [".env"]},
        {
            "task_id": "research-1",
            "role": "researcher",
            "objective": "読む",
            "path_scope": ["calc.py"],
            "read_scope": ["pyproject.toml"],
            "acceptance": ["読めた"],
        },
        1,
        None,
    )

    assert '"task_id":"research-1"' in prompt
    assert '"read_scope":["pyproject.toml"]' in prompt
