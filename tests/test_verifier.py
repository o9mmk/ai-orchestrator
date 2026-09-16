"""Verifier mandatory gateとscope照合のテスト。"""

import pytest

from orc.baseline import GateSpec
from orc.errors import VerificationError
from orc.verifier import check_patch_scope, require_mandatory_gates


def test_gitleaks_and_glassworm_cannot_be_skipped() -> None:
    """必須2 gateが片方でも無ければVerifier設定を拒否する。"""
    gates = [GateSpec("pytest", ("pytest", "-q")), GateSpec("gitleaks", ("gitleaks",))]

    with pytest.raises(VerificationError, match="glassworm"):
        require_mandatory_gates(gates)


def test_patch_changed_files_and_scope_must_match() -> None:
    """patch集合とchanged_filesが一致し、全fileがscope内なら通す。"""
    result = check_patch_scope(
        ["src/a.py"],
        ["src/a.py"],
        ["src"],
        {"src/a.py": "100644"},
    )

    assert result.in_scope is True


@pytest.mark.parametrize(
    ("patch", "changed", "scope", "modes", "message"),
    [
        (["src/a.py"], [], ["src"], {}, "do not match"),
        (["other/a.py"], ["other/a.py"], ["src"], {}, "outside scope"),
        (
            ["src/link"],
            ["src/link"],
            ["src"],
            {"src/link": "120000"},
            "symlink mode",
        ),
        (
            ["src/a.py"],
            ["src/a.py"],
            ["src"],
            {"other.py": "120000"},
            "mode entry",
        ),
    ],
)
def test_scope_mismatch_and_symlink_mode_are_rejected(
    patch: list[str],
    changed: list[str],
    scope: list[str],
    modes: dict[str, str],
    message: str,
) -> None:
    """scope/changed/modeの不一致をfail-loudにする。"""
    with pytest.raises(VerificationError, match=message):
        check_patch_scope(patch, changed, scope, modes)
