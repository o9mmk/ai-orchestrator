"""設計書§4 S/M/L/XL classifierの境界テスト。"""

import pytest

from orc.command_risk import CommandRisk, classify_command
from orc.sizing import Size, SizingInput, classify_size


@pytest.mark.parametrize(
    ("files", "lines", "expected"),
    [
        (2, 50, Size.S),
        (2, 51, Size.M),
        (10, 500, Size.M),
        (11, 1, Size.L),
    ],
)
def test_size_boundaries(files: int, lines: int, expected: Size) -> None:
    """S/M/Lの境界値を安全側へ取り違えない。"""
    decision = classify_size(
        SizingInput(
            planner_size=Size.S,
            estimated_files=files,
            estimated_diff_lines=lines,
            path_scopes=[f"src/file-{index}.py" for index in range(files)],
        )
    )

    assert decision.deterministic_size is expected
    assert decision.final_size is expected


@pytest.mark.parametrize(
    "path",
    [
        "pyproject.toml",
        "schemas/event.schema.json",
        ".github/workflows/test.yml",
        "src/auth/session.py",
    ],
)
def test_escalation_paths_are_l(path: str) -> None:
    """dependency/schema/CI/security pathは1件でもLへ昇格する。"""
    decision = classify_size(
        SizingInput(
            planner_size=Size.S,
            estimated_files=1,
            estimated_diff_lines=1,
            path_scopes=[path],
        )
    )

    assert decision.deterministic_size is Size.L
    assert decision.escalation_flags


def test_low_confidence_and_unknown_scope_escalate_to_l() -> None:
    """低confidence・未解決globは判定不能としてLへ昇格する。"""
    decision = classify_size(
        SizingInput(
            planner_size=Size.S,
            estimated_files=1,
            estimated_diff_lines=10,
            path_scopes=["src/*.py"],
            scope_confidences=["low"],
            unresolved_scopes=["src/*.py"],
        )
    )

    assert decision.final_size is Size.L


def test_production_hint_is_xl() -> None:
    """本番示唆はplan-onlyのXLとする。"""
    production = classify_size(
        SizingInput(
            planner_size=Size.S,
            estimated_files=1,
            estimated_diff_lines=1,
            path_scopes=["src/a.py"],
            commands=["deploy production"],
        )
    )

    assert production.final_size is Size.XL


def test_invocation_estimate_over_cap_escalates_to_l_not_xl() -> None:
    """invocation見積り超過は承認ゲート止まり(実行時hard capが本来の強制点)。"""
    over_cap = classify_size(
        SizingInput(
            planner_size=Size.S,
            estimated_files=1,
            estimated_diff_lines=1,
            estimated_invocations=21,
            invocation_cap=20,
            path_scopes=["src/a.py"],
        )
    )

    assert over_cap.final_size is Size.L
    assert "invocation_estimate_exceeds_cap" in over_cap.escalation_flags
    assert over_cap.xl_flags == ()


def test_planner_size_can_only_raise_final_size() -> None:
    """最終規模=max(Planner申告, 決定論判定)を維持する。"""
    decision = classify_size(
        SizingInput(
            planner_size=Size.L,
            estimated_files=1,
            estimated_diff_lines=1,
            path_scopes=["src/a.py"],
        )
    )

    assert decision.deterministic_size is Size.S
    assert decision.final_size is Size.L


def test_absolute_python_pytest_command_is_known_safe() -> None:
    """実行可能ファイルの絶対pathでも安全なpytest invocationを認識する。"""
    assert classify_command("/usr/local/bin/python3.12 -m pytest -q") is CommandRisk.SAFE


@pytest.mark.parametrize("flag", ["multiple_repos", "large_migration"])
def test_structured_xl_flags_stop_implementation(flag: str) -> None:
    """複数repo・大規模migrationの構造化flagをXLへ分類する。"""
    kwargs = {flag: True}
    decision = classify_size(
        SizingInput(
            planner_size=Size.S,
            estimated_files=1,
            estimated_diff_lines=1,
            path_scopes=["src/a.py"],
            **kwargs,  # type: ignore[arg-type]
        )
    )

    assert decision.final_size is Size.XL
