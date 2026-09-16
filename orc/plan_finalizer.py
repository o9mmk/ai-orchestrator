"""Manager-owned plan scope normalization and deterministic sizing."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orc.plan_schema import validate_plan
from orc.scope import ScopeResolution, normalize_scopes
from orc.sizing import Size, SizeDecision, SizingInput, classify_size


@dataclass(frozen=True)
class FinalizedPlan:
    """Validated plan plus Manager-owned scope and sizing decisions."""

    plan: dict[str, Any]
    resolution: ScopeResolution
    decision: SizeDecision
    read_resolution: ScopeResolution = ScopeResolution((), ())


def finalize_plan(
    repo_path: Path,
    plan: dict[str, Any],
    *,
    invocation_cap: int,
    multiple_repos: bool = False,
    production_hint: bool = False,
    large_migration: bool = False,
) -> FinalizedPlan:
    """Revalidate Planner data, normalize scopes, and apply safe max sizing."""
    validate_plan(plan)
    tasks = plan["tasks"]
    scopes = [scope for task in tasks for scope in task["path_scope"]]
    resolution = normalize_scopes(repo_path, scopes)
    # read scopeもdeny patternとrepo外を同じ関数で拒否するが、書き込みallowlistにも
    # sizing判定にも混ぜない。読むだけのmanifestやglobでrunを昇格させないため。
    read_scopes = [scope for task in tasks for scope in task.get("read_scope", [])]
    read_resolution = normalize_scopes(repo_path, read_scopes)
    estimates = [task["size_estimate"] for task in tasks]
    commands = [command for task in tasks for command in task.get("commands", [])]
    decision = classify_size(
        SizingInput(
            planner_size=Size(plan["planner_size"]),
            estimated_files=max(
                sum(item["estimated_files"] for item in estimates),
                len(resolution.resolved),
            ),
            estimated_diff_lines=sum(item["estimated_diff_lines"] for item in estimates),
            estimated_invocations=sum(item["estimated_invocations"] for item in estimates),
            invocation_cap=invocation_cap,
            path_scopes=scopes,
            scope_confidences=[task["scope_confidence"] for task in tasks],
            unresolved_scopes=list(resolution.unresolved),
            commands=commands,
            multiple_repos=multiple_repos,
            production_hint=production_hint,
            large_migration=large_migration,
        )
    )
    finalized = copy.deepcopy(plan)
    finalized["deterministic_size"] = decision.deterministic_size.value
    finalized["final_size"] = decision.final_size.value
    validate_plan(finalized)
    return FinalizedPlan(finalized, resolution, decision, read_resolution)
