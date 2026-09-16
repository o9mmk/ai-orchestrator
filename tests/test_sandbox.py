"""Verifier sandboxとsandbox-exec PoCのテスト。"""

import os
import sys
import time
from pathlib import Path

import pytest

from orc.errors import SandboxViolation
from orc.sandbox import SandboxLimits, SandboxRunner, scan_worktree_boundary
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


def test_output_flood_is_stopped_at_the_cap(tmp_path: Path) -> None:
    """子が上限を超える出力を吐いたら、蓄積し続けずprocess groupごと停止する。"""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    flood = "import sys\nwhile True:\n    sys.stdout.write('x' * 65536)\n    sys.stdout.flush()\n"
    runner = SandboxRunner(limits=SandboxLimits(output_bytes=256 * 1024))
    started = time.monotonic()
    with pytest.raises(SandboxViolation, match="output limit"):
        runner.run([sys.executable, "-c", flood], worktree, timeout_seconds=60)
    # timeout(60s)を待たずに出力超過で止まっていること。
    assert time.monotonic() - started < 30


def test_disk_growth_is_detected_while_the_process_is_still_running(tmp_path: Path) -> None:
    """RLIMIT_FSIZEに収まる多数ファイルでの肥大化を、終了を待たず実行中に検知する。"""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    grow = (
        "import pathlib, time\n"
        "for i in range(64):\n"
        "    pathlib.Path(f'f{i}').write_bytes(b'0' * 65536)\n"
        "time.sleep(60)\n"
    )
    runner = SandboxRunner(
        limits=SandboxLimits(disk_growth_bytes=1024 * 1024, disk_check_seconds=0.5)
    )
    started = time.monotonic()
    with pytest.raises(SandboxViolation, match="disk growth"):
        runner.run([sys.executable, "-c", grow], worktree, timeout_seconds=60)
    assert time.monotonic() - started < 30


def test_limits_are_applied_by_the_launcher_not_by_preexec_fn() -> None:
    """上限適用がfork後・exec前のPythonコールバックに依存していないこと。"""
    source = Path(SandboxRunner.__module__.replace(".", "/") + ".py").read_text(encoding="utf-8")
    assert "preexec_fn=" not in source
    assert "limit_launcher" in source


def test_child_that_closes_its_streams_still_hits_the_deadline(tmp_path: Path) -> None:
    """stdout/stderrを閉じて走り続ける子に対しても、timeoutを期限どおり適用する。"""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    linger = "import os, time\nos.close(1)\nos.close(2)\ntime.sleep(60)\n"
    started = time.monotonic()
    result = SandboxRunner().run([sys.executable, "-c", linger], worktree, timeout_seconds=2)
    assert result.timed_out is True
    assert time.monotonic() - started < 15


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("", ((), False)),
        ("not json", ((), False)),
        ('{"version": 1, "unapplied": ["RLIMIT_AS"], "complete": false}', ((), False)),
        ('{"version": 2, "unapplied": [], "complete": true}', ((), False)),
        ('{"version": 1, "unapplied": ["RLIMIT_AS"], "complete": true}', (("RLIMIT_AS",), True)),
        ('{"version": 1, "unapplied": [], "complete": true}', ((), True)),
    ],
)
def test_torn_or_incomplete_limit_report_is_never_read_as_enforced(
    tmp_path: Path, content: str, expected: tuple[tuple[str, ...], bool]
) -> None:
    """空ファイル・壊れたJSON・未完成の報告を「未適用なし」と読み替えない。"""
    from orc.sandbox import _read_unapplied_limits

    report = tmp_path / "report"
    report.write_text(content, encoding="utf-8")
    assert _read_unapplied_limits(report) == expected
    assert _read_unapplied_limits(tmp_path / "missing") == ((), False)


def test_disk_growth_is_detected_after_the_child_closes_its_streams(tmp_path: Path) -> None:
    """stdout/stderrを閉じた後に肥大化する子も、終了を待たずに検知する。"""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    grow_after_close = (
        "import os, pathlib, time\n"
        "os.close(1)\nos.close(2)\n"
        "for i in range(64):\n"
        "    pathlib.Path(f'f{i}').write_bytes(b'0' * 65536)\n"
        "time.sleep(60)\n"
    )
    runner = SandboxRunner(
        limits=SandboxLimits(disk_growth_bytes=1024 * 1024, disk_check_seconds=0.5)
    )
    started = time.monotonic()
    with pytest.raises(SandboxViolation, match="disk growth") as caught:
        runner.run([sys.executable, "-c", grow_after_close], worktree, timeout_seconds=60)
    assert time.monotonic() - started < 30
    # 停止までの部分結果を伴い、監査記録へ残せる。
    assert caught.value.result is not None
