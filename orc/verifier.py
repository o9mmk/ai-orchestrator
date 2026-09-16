"""Verifier必須gateとpatch scope相互照合。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any

from orc.baseline import GateDecision, GateSpec
from orc.errors import VerificationError
from orc.verify_schema import validate_verify_report

MANDATORY_GATES = frozenset({"gitleaks", "glassworm"})


@dataclass(frozen=True)
class ScopeCheck:
    """patch/changed_files/path_scopeの相互照合結果。"""

    patch_files: tuple[str, ...]
    in_scope: bool


@dataclass(frozen=True)
class SecretScan:
    """DLP ingestへ渡すgitleaks結果。"""

    tool: str
    findings_count: int
    quarantined: bool


def require_mandatory_gates(gates: list[GateSpec]) -> None:
    """gitleaks/不可視文字gateのskipを構造的に拒否する。"""
    names = {gate.name for gate in gates}
    missing = MANDATORY_GATES - names
    if missing:
        raise VerificationError(f"mandatory gates missing: {','.join(sorted(missing))}")


def check_patch_scope(
    patch_files: list[str],
    changed_files: list[str],
    path_scope: list[str],
    patch_modes: dict[str, str] | None = None,
) -> ScopeCheck:
    """changed_files=patch集合、scope内、mode 120000なしを機械照合する。"""
    patch_set = {_safe_relative(path) for path in patch_files}
    changed_set = {_safe_relative(path) for path in changed_files}
    scopes = [_safe_relative(path) for path in path_scope]
    modes = patch_modes or {}
    if patch_set != changed_set:
        raise VerificationError("changed_files do not match patch files")
    normalized_modes = {_safe_relative(path): mode for path, mode in modes.items()}
    if any(path not in patch_set for path in normalized_modes):
        raise VerificationError("patch mode entry does not match patch files")
    if any(mode == "120000" for mode in normalized_modes.values()):
        raise VerificationError("symlink mode 120000 is forbidden")
    out_of_scope = [path for path in patch_set if not any(_is_within(path, scope) for scope in scopes)]
    if out_of_scope:
        raise VerificationError(f"patch files outside scope: {','.join(sorted(out_of_scope))}")
    return ScopeCheck(tuple(sorted(patch_set)), True)


def build_verify_report(
    decisions: list[GateDecision],
    scope_check: ScopeCheck,
    secret_scan: SecretScan,
    sandbox_profile: str,
) -> dict[str, Any]:
    """分類結果を§9 verify.json schemaへまとめる。"""
    report = {
        "gates": [decision.as_verify_gate() for decision in decisions],
        "scope_check": {
            "patch_files": list(scope_check.patch_files),
            "in_scope": scope_check.in_scope,
        },
        "secret_scan": asdict(secret_scan),
        "sandbox_profile": sandbox_profile,
    }
    validate_verify_report(report)
    return report


def _safe_relative(path: str) -> str:
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise VerificationError(f"unsafe patch path: {path}")
    return pure.as_posix()


def _is_within(path: str, scope: str) -> bool:
    path_parts = PurePosixPath(path).parts
    scope_parts = PurePosixPath(scope).parts
    return path_parts[: len(scope_parts)] == scope_parts
