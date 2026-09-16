"""テストデータ生成ヘルパー。"""

from pathlib import Path
from typing import Any

from orc.budget_models import default_budget_caps


def manifest_data(repo: Path, run_id: str, fencing_token: int) -> dict[str, Any]:
    """設計書§9の全必須フィールドを持つmanifestを返す。"""
    return {
        "run_id": run_id,
        "generation": 1,
        "created_at": "2026-07-16T00:00:00Z",
        "goal": "M1を検証する",
        "acceptance_criteria": ["hash chainが検証できる"],
        "forbidden": ["push"],
        "authority_sources": ["user", "AGENTS.md"],
        "repo_path": str(repo.resolve()),
        "base_commit": "a" * 40,
        "size": "M",
        "caps": default_budget_caps(),
        "budget_source": "measured",
        "reviewer_policy": "claude_then_codex_then_none",
        "gates": ["pytest", "gitleaks", "glassworm"],
        "safety_policy_version": "1.0",
        "state": "INIT",
        "fencing_token": fencing_token,
    }
