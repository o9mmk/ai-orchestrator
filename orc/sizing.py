"""設計書§4の決定論S/M/L/XL classifier。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from orc.command_risk import CommandRisk, classify_command


class Size(StrEnum):
    """run規模。"""

    S = "S"
    M = "M"
    L = "L"
    XL = "XL"


SIZE_ORDER = {Size.S: 0, Size.M: 1, Size.L: 2, Size.XL: 3}


@dataclass(frozen=True)
class SizingInput:
    """Planner申告と機械検査値。"""

    planner_size: Size
    estimated_files: int
    estimated_diff_lines: int
    path_scopes: list[str]
    scope_confidences: list[str] | None = None
    unresolved_scopes: list[str] | None = None
    commands: list[str] | None = None
    estimated_invocations: int = 0
    invocation_cap: int = 20
    multiple_repos: bool = False
    production_hint: bool = False
    large_migration: bool = False


@dataclass(frozen=True)
class SizeDecision:
    """決定論判定・最終判定・昇格根拠。"""

    planner_size: Size
    deterministic_size: Size
    final_size: Size
    escalation_flags: tuple[str, ...]
    xl_flags: tuple[str, ...]
    command_risks: tuple[CommandRisk, ...]


def _path_flags(path: str) -> list[str]:
    lowered = path.lower()
    pure = PurePosixPath(lowered)
    name = pure.name
    flags: list[str] = []
    if (
        name == "pyproject.toml"
        or name.startswith("requirements")
        or name in {"package.json", "go.mod", "cargo.toml"}
    ):
        flags.append("dependency_manifest")
    if "schemas" in pure.parts or "migrations" in pure.parts or name.endswith(".schema.json"):
        flags.append("schema_or_migration")
    if ".github" in pure.parts or "ci" in pure.parts:
        flags.append("ci_or_security")
    if any("auth" in part or "crypto" in part or "secret" in part for part in pure.parts):
        flags.append("ci_or_security")
    return flags


def classify_size(data: SizingInput) -> SizeDecision:
    """境界・不明を安全側へ上げ、Planner申告より下げずに確定する。"""
    if (
        data.estimated_files < 0
        or data.estimated_diff_lines < 0
        or data.estimated_invocations < 0
        or data.invocation_cap <= 0
    ):
        raise ValueError("size estimates must be non-negative and invocation cap positive")
    escalation = {flag for path in data.path_scopes for flag in _path_flags(path)}
    confidences = data.scope_confidences or []
    if "low" in confidences or data.unresolved_scopes:
        escalation.add("scope_unknown")
    command_risks = tuple(classify_command(command) for command in data.commands or [])
    if any(risk is not CommandRisk.SAFE for risk in command_risks):
        escalation.add("command_side_effect_or_unknown")

    xl_flags: set[str] = set()
    if data.multiple_repos:
        xl_flags.add("multiple_repos")
    if data.production_hint or CommandRisk.PRODUCTION in command_risks:
        xl_flags.add("production_hint")
    if data.estimated_invocations > data.invocation_cap:
        # Planner見積りは申告値でしかなく、実際の上限はrun中のbudget meterが
        # spawn前に強制する。見積り超過だけでplan-onlyへ落とすと二重計上になるため、
        # 承認ゲート(L)までの昇格に留める。
        escalation.add("invocation_estimate_exceeds_cap")
    if data.large_migration:
        xl_flags.add("large_migration")

    if xl_flags:
        deterministic = Size.XL
    elif data.estimated_files > 10 or escalation:
        deterministic = Size.L
    elif data.estimated_files <= 2 and data.estimated_diff_lines <= 50 and not escalation:
        deterministic = Size.S
    else:
        deterministic = Size.M
    final = max((data.planner_size, deterministic), key=SIZE_ORDER.__getitem__)
    return SizeDecision(
        planner_size=data.planner_size,
        deterministic_size=deterministic,
        final_size=final,
        escalation_flags=tuple(sorted(escalation)),
        xl_flags=tuple(sorted(xl_flags)),
        command_risks=command_risks,
    )
