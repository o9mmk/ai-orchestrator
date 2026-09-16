"""Preflight用git HEAD/branch/dirty事実収集。"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from orc.errors import PreflightError


@dataclass(frozen=True)
class DirtyEntry:
    """porcelain status 1件。"""

    status: str
    path: str
    original_path: str | None = None


@dataclass(frozen=True)
class GitFacts:
    """Stage 1/2間で比較するrepo snapshot。"""

    repo_path: Path
    head: str
    branch: str
    dirty: tuple[DirtyEntry, ...]

    @property
    def dirty_paths(self) -> tuple[str, ...]:
        """rename元も含むdirty pathを重複なく返す。"""
        paths = {
            path for entry in self.dirty for path in (entry.path, entry.original_path) if path is not None
        }
        return tuple(sorted(paths))


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        timeout=10,
    )
    if check and result.returncode != 0:
        message = os.fsdecode(result.stderr).strip()
        raise PreflightError(f"git preflight failed: {message or args[0]}")
    return result


def collect_git_facts(repo_path: Path) -> GitFacts:
    """repoのHEAD/branch/porcelain statusを副作用なく取得する。"""
    repo = repo_path.resolve(strict=True)
    head = os.fsdecode(_git(repo, "rev-parse", "HEAD").stdout).strip()
    branch_result = _git(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False)
    branch = os.fsdecode(branch_result.stdout).strip() if branch_result.returncode == 0 else "HEAD"
    raw = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    records = raw.split(b"\0")
    dirty: list[DirtyEntry] = []
    index = 0
    while index < len(records) and records[index]:
        record = records[index]
        if len(record) < 4:
            raise PreflightError("invalid git status porcelain record")
        status = os.fsdecode(record[:2])
        path = os.fsdecode(record[3:])
        original: str | None = None
        if "R" in status or "C" in status:
            index += 1
            if index >= len(records) or not records[index]:
                raise PreflightError("invalid git rename porcelain record")
            original = os.fsdecode(records[index])
        dirty.append(DirtyEntry(status, path, original))
        index += 1
    return GitFacts(repo, head, branch, tuple(dirty))
