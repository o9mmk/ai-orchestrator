"""repo fingerprintと状態領域の安全なpath生成。"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")


def validate_identifier(value: str, *, label: str) -> str:
    """state path segmentを単一の安全なidentifierへ制限する。"""
    if not IDENTIFIER_PATTERN.fullmatch(value) or value in {".", ".."}:
        raise ValueError(f"invalid {label}: {value}")
    return value


def state_root() -> Path:
    """ORC_STATE_DIR、未指定時は~/.orchestratorを返す。"""
    configured = os.environ.get("ORC_STATE_DIR", "~/.orchestrator")
    return Path(configured).expanduser().resolve()


def validate_state_root(repo_path: Path) -> Path:
    """state rootがrepo外にあることを、directory作成前に検証する。"""
    repo = repo_path.resolve(strict=True)
    root = state_root()
    if root == repo or root.is_relative_to(repo):
        raise ValueError("ORC_STATE_DIR must be outside repository")
    return root


def repo_identity(repo_path: Path) -> Path:
    """worktree間でも共通になるgit common dirをrepo識別子に使う。"""
    repo = repo_path.resolve(strict=True)
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return repo
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = repo / common
    return common.resolve(strict=True)


def repo_fingerprint(repo_path: Path) -> str:
    """repo identityから衝突しにくい固定長fingerprintを作る。"""
    identity = os.fsencode(repo_identity(repo_path))
    return hashlib.sha256(identity).hexdigest()[:24]


def ensure_private_dir(path: Path) -> Path:
    """state directoryを作成し0700を強制する。"""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


@dataclass(frozen=True)
class StatePaths:
    """1 repo/runに属する状態path集合。"""

    root: Path
    repo_fp: str
    run_id: str

    @classmethod
    def for_run(cls, repo_path: Path, run_id: str) -> StatePaths:
        """環境設定とrepoからpath集合を組み立てる。"""
        return cls(
            state_root(),
            repo_fingerprint(repo_path),
            validate_identifier(run_id, label="run_id"),
        )

    @property
    def lock_dir(self) -> Path:
        """repo単位lease領域。"""
        return self.root / "locks" / self.repo_fp

    @property
    def run_dir(self) -> Path:
        """run監査領域。"""
        return self.root / "runs" / self.repo_fp / self.run_id

    @property
    def worktree_root(self) -> Path:
        """run配下の専有worktree親領域。"""
        return self.root / "worktrees" / self.repo_fp / self.run_id
