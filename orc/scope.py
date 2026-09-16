"""plan path scopeのrealpath正規化とdirty重複検査。"""

from __future__ import annotations

import glob
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from orc.errors import PreflightError


@dataclass(frozen=True)
class ScopeResolution:
    """正規化済みscopeと解決不能glob。"""

    resolved: tuple[str, ...]
    unresolved: tuple[str, ...]


def _denied_scope(scope: str) -> bool:
    """秘密読取deny patternに一致するpathを内容確認前に遮断する。"""
    pure = PurePosixPath(scope.lower())
    name = pure.name
    return (
        name.startswith(".env")
        or name.endswith(".pem")
        or any(part in {"credentials", ".ssh"} for part in pure.parts)
        or any("secret" in part or "token" in part for part in pure.parts)
    )


def _inside_repo(repo: Path, candidate: Path, original: str) -> Path:
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(repo):
        raise PreflightError(f"path scope outside repo: {original}")
    return resolved


def normalize_scopes(repo_path: Path, scopes: list[str]) -> ScopeResolution:
    """glob展開とsymlink解決後にrepo内allowlistを確定する。"""
    repo = repo_path.resolve(strict=True)
    resolved: set[str] = set()
    unresolved: set[str] = set()
    for scope in scopes:
        if _denied_scope(scope):
            raise PreflightError(f"denied path scope: {scope}")
        pure = PurePosixPath(scope)
        if pure.is_absolute() or ".." in pure.parts:
            raise PreflightError(f"path scope outside repo: {scope}")
        if not pure.parts:
            unresolved.add(scope)
        if glob.has_magic(scope):
            matches = glob.glob(scope, root_dir=repo, recursive=True, include_hidden=True)
            if not matches:
                unresolved.add(scope)
                continue
            candidates = [repo / match for match in matches]
        else:
            candidates = [repo / scope]
        for candidate in candidates:
            safe = _inside_repo(repo, candidate, scope)
            resolved.add(safe.relative_to(repo).as_posix())
    return ScopeResolution(tuple(sorted(resolved)), tuple(sorted(unresolved)))


def find_dirty_overlaps(
    scopes: tuple[str, ...],
    dirty_paths: tuple[str, ...],
) -> tuple[str, ...]:
    """scopeと同一またはその子にあるdirty pathを返す。"""
    overlaps: set[str] = set()
    scope_parts = [PurePosixPath(scope).parts for scope in scopes]
    for dirty in dirty_paths:
        dirty_parts = PurePosixPath(dirty).parts
        if any(dirty_parts[: len(parts)] == parts for parts in scope_parts):
            overlaps.add(dirty)
    return tuple(sorted(overlaps))
