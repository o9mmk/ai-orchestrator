"""Verifier sandboxとsandbox-exec PoCのテスト。"""

import os
import sys
from pathlib import Path

import pytest

from orc.errors import SandboxViolation
from orc.sandbox import SandboxRunner, scan_worktree_boundary
from orc.sandbox_poc import run_sandbox_poc


def test_external_symlink_and_hardlink_are_rejected(tmp_path: Path) -> None:
    """実行前scanでsymlink/hardlink escapeをfail-loudに遮断する。"""
    worktree = tmp_path / "worktree"
    outside = tmp_path / "outside.txt"
    worktree.mkdir()
    outside.write_text("outside", encoding="utf-8")
    (worktree / "escape").symlink_to(outside)

    with pytest.raises(SandboxViolation, match="external symlink"):
        scan_worktree_boundary(worktree)
    (worktree / "escape").unlink()
    os.link(outside, worktree / "hardlink")
    with pytest.raises(SandboxViolation, match="hardlink"):
        scan_worktree_boundary(worktree)


def test_symlinked_worktree_root_is_rejected(tmp_path: Path) -> None:
    """root自体のsymlinkもrealpath境界検査前に拒否する。"""
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "worktree"
    link.symlink_to(actual, target_is_directory=True)

    with pytest.raises(SandboxViolation, match="root must not be a symlink"):
        SandboxRunner().run([sys.executable, "-c", "pass"], link, timeout_seconds=10)


def test_environment_is_allowlisted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """秘密envを継承せず、明示allowlistだけを子へ渡す。"""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setenv("DUMMY_SECRET_FOR_TEST", "must-not-pass")
    runner = SandboxRunner()

    result = runner.run(
        [
            sys.executable,
            "-c",
            "import os; raise SystemExit(0 if 'DUMMY_SECRET_FOR_TEST' not in os.environ else 9)",
        ],
        worktree,
        timeout_seconds=10,
    )

    assert result.exit_code == 0
    assert result.timed_out is False


def test_secret_file_read_is_denied_without_exposing_value(tmp_path: Path) -> None:
    """worktree内の秘密patternもprofileでread denyする。"""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    secret = worktree / ".env.test"
    secret.write_text("dummy-value", encoding="utf-8")
    runner = SandboxRunner()

    result = runner.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('.env.test').read_text()",
        ],
        worktree,
        timeout_seconds=10,
    )

    assert result.exit_code != 0
    assert result.denial_detected is True
    assert "dummy-value" not in result.stderr


def test_sandbox_exec_poc_blocks_network_and_outside_write() -> None:
    """実機sandbox-execでinside書込だけを許可しnetwork/外部書込を拒否する。"""
    result = run_sandbox_poc()

    assert result.sandbox_exec_available is True
    assert result.inside_write_allowed is True
    assert result.outside_write_blocked is True
    assert result.network_blocked is True
    assert result.local_socket_blocked is True
    assert result.passed is True
    assert result.outside_denial_observed is True
    assert result.network_denial_observed is True
    assert result.local_socket_denial_observed is True


def test_timeout_kills_sandbox_process_group(tmp_path: Path) -> None:
    """Verifier timeoutはprocess groupをkillし明示結果を返す。"""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    runner = SandboxRunner()

    result = runner.run(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        worktree,
        timeout_seconds=1,
    )

    assert result.timed_out is True
    assert result.exit_code == -9


def test_platform_rejected_limit_is_reported_not_swallowed(tmp_path: Path) -> None:
    """platformが拒否したresource上限は、起動失敗にも黙殺にもせず結果へ載せる。"""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    # macOSはRLIMIT_ASを実質サポートしないため、既定のmemory_bytesは適用できない。
    result = SandboxRunner().run([sys.executable, "-c", "pass"], worktree, timeout_seconds=30)
    assert result.exit_code == 0
    assert result.limits_verified is True
    if sys.platform == "darwin":
        assert "RLIMIT_AS" in result.unsupported_limits


def test_limit_report_is_not_writable_by_the_inspected_process(tmp_path: Path) -> None:
    """封じ込め状況の報告を、被検査プロセスがworktree経由で改ざんできないこと。

    報告をworktree内に置いていた頃は、profileがworktree配下への書込みを許すため
    子プロセスが報告を空にでき、親が「未適用ゼロ」と誤読できた。
    """
    worktree = tmp_path / "wt"
    worktree.mkdir()
    # 子はworktree配下を全て走査し、見つけた報告らしきファイルを空にしようとする。
    sabotage = (
        "import pathlib\n"
        "for p in pathlib.Path('.').rglob('*'):\n"
        "    if p.is_file():\n"
        "        try:\n"
        "            p.write_text('')\n"
        "        except OSError:\n"
        "            pass\n"
    )
    result = SandboxRunner().run([sys.executable, "-c", sabotage], worktree, timeout_seconds=30)
    assert result.limits_verified is True
    if sys.platform == "darwin":
        # 改ざんを試みても、報告はworktree外にあるため実態どおりのまま残る。
        assert "RLIMIT_AS" in result.unsupported_limits
