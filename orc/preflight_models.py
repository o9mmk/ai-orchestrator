"""Preflightの設定・結果・tool probe model。"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orc.budget_models import default_budget_caps
from orc.git_facts import GitFacts
from orc.lease import Lease
from orc.state_machine import RunState
from orc.store import RunStateStore


@dataclass(frozen=True)
class ToolInfo:
    """利用可能toolのpath/version snapshot。"""

    name: str
    available: bool
    path: str | None
    version: str | None


@dataclass(frozen=True)
class PreflightConfig:
    """Stage 1でmanifestへ固定するauthority/policy値。"""

    run_id: str
    goal: str
    acceptance_criteria: list[str]
    forbidden: list[str]
    authority_sources: list[str]
    budget_source: str
    reviewer_policy: str
    gates: list[str]
    safety_policy_version: str
    caps: dict[str, Any] | None = None
    required_tools: tuple[str, ...] = ("git", "pytest")
    optional_tools: tuple[str, ...] = ("codex", "claude")


@dataclass(frozen=True)
class Stage1Result:
    """Stage 1の監査可能な結果。"""

    state: RunState
    reason: str
    git_facts: GitFacts
    tools: dict[str, ToolInfo]
    store: RunStateStore | None
    lease: Lease | None
    events_path: Path
    checkpoint_path: Path
    config: PreflightConfig


@dataclass(frozen=True)
class Stage2Result:
    """Stage 2のscope/sizing/遷移結果。"""

    state: RunState
    reason: str
    final_size: str
    normalized_scopes: tuple[str, ...]
    unresolved_scopes: tuple[str, ...]
    dirty_overlaps: tuple[str, ...]
    dirty_warning: tuple[str, ...]
    command_risks: tuple[str, ...]
    plan: dict[str, Any]


def probe_tool(name: str) -> ToolInfo:
    """PATH上のtoolを副作用のない--versionで確認する。"""
    path = shutil.which(name)
    if path is None:
        return ToolInfo(name, False, None, None)
    try:
        result = subprocess.run(
            [path, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return ToolInfo(name, False, path, "timeout")
    output = (result.stdout or result.stderr).splitlines()
    version = output[0][:500] if output else f"exit={result.returncode}"
    return ToolInfo(name, result.returncode == 0, path, version)


def build_manifest(
    repo_path: Path,
    config: PreflightConfig,
    facts: GitFacts,
) -> dict[str, Any]:
    """Stage 1で固定する全必須manifest fieldを組み立てる。"""
    return {
        "run_id": config.run_id,
        "generation": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "goal": config.goal,
        "acceptance_criteria": config.acceptance_criteria,
        "forbidden": config.forbidden,
        "authority_sources": config.authority_sources,
        "repo_path": str(repo_path),
        "base_commit": facts.head,
        "size": "XL",
        "caps": config.caps if config.caps is not None else default_budget_caps(),
        "budget_source": config.budget_source,
        "reviewer_policy": config.reviewer_policy,
        "gates": config.gates,
        "safety_policy_version": config.safety_policy_version,
        "state": "INIT",
        "fencing_token": 0,
    }
