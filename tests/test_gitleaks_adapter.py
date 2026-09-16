"""M6 gitleaks subprocess adapter fail-closed tests."""

import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from orc.dlp_models import ScannerStatus
from orc.gitleaks_adapter import GitleaksScanner
from tests.m4_helpers import process_exists, wait_pid_file


def _fake_gitleaks(tmp_path: Path, mode: str) -> Path:
    executable = tmp_path / f"fake-gitleaks-{mode}"
    descendant_pid = tmp_path / f"gitleaks-descendant-{mode}.pid"
    script = f"""\
#!{sys.executable}
import json
import subprocess
import stat
import sys
import time
from pathlib import Path

MODE = {mode!r}
if sys.argv[1:] == ["version"]:
    print("8.30.1" if MODE != "unsupported" else "7.9.0")
    raise SystemExit(0)
if sys.argv[1:] == ["dir", "--help"]:
    print("--report-format --report-path --redact --no-banner" if MODE != "missing_flag" else "--report-path")
    raise SystemExit(0)
if MODE == "timeout":
    time.sleep(30)
if MODE == "descendant":
    subprocess.Popen([
        sys.executable,
        "-c",
        "import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        + "open({str(descendant_pid)!r}, 'w').write(str(os.getpid())); time.sleep(30)",
    ])
    deadline = time.monotonic() + 2
    while not Path({str(descendant_pid)!r}).exists() and time.monotonic() < deadline:
        time.sleep(0.01)
report = Path(sys.argv[sys.argv.index("--report-path") + 1])
assert stat.S_IMODE(report.stat().st_mode) == 0o600
if MODE == "malformed":
    report.write_text("{{", encoding="utf-8")
elif MODE == "oversize":
    report.write_text("[" + (" " * 5000) + "]", encoding="utf-8")
elif MODE == "finding":
    report.write_text(
        json.dumps([{{"RuleID": "generic-api-key", "Secret": "raw-must-not-escape"}}]),
        encoding="utf-8",
    )
    raise SystemExit(1)
else:
    report.write_text("[]", encoding="utf-8")
if MODE == "output_spam":
    print("x" * 5000)
if MODE == "unknown_exit":
    print("raw scanner failure must not escape", file=sys.stderr)
    raise SystemExit(2)
"""
    executable.write_text(textwrap.dedent(script), encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def test_gitleaks_clean_and_finding_results_are_safely_reduced(tmp_path: Path) -> None:
    target = tmp_path / "artifact.txt"
    target.write_text("harmless", encoding="utf-8")

    clean = GitleaksScanner(_fake_gitleaks(tmp_path, "clean")).scan(target)
    finding = GitleaksScanner(_fake_gitleaks(tmp_path, "finding")).scan(target)

    assert clean.status is ScannerStatus.CLEAN
    assert finding.status is ScannerStatus.FINDINGS
    assert dict(finding.category_counts) == {"SECRET": 1}
    assert "raw-must-not-escape" not in repr(finding)


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("timeout", "SCANNER_TIMEOUT"),
        ("unknown_exit", "SCANNER_EXIT_UNKNOWN"),
        ("malformed", "SCANNER_REPORT_MALFORMED"),
        ("oversize", "SCANNER_REPORT_LIMIT"),
        ("output_spam", "SCANNER_OUTPUT_LIMIT"),
        ("unsupported", "SCANNER_VERSION_UNSUPPORTED"),
        ("missing_flag", "SCANNER_CAPABILITY_UNSUPPORTED"),
    ],
)
def test_gitleaks_failures_never_become_clean(
    tmp_path: Path,
    mode: str,
    reason: str,
) -> None:
    target = tmp_path / "artifact.txt"
    target.write_text("harmless", encoding="utf-8")
    scanner = GitleaksScanner(
        _fake_gitleaks(tmp_path, mode),
        timeout_seconds=0.05 if mode == "timeout" else 3.0,
        output_limit_bytes=128,
        report_limit_bytes=1024,
    )

    result = scanner.scan(target)

    assert result.status is ScannerStatus.FAILED
    assert result.reason_code == reason
    assert "raw scanner failure" not in repr(result)


def test_missing_gitleaks_executable_is_fail_closed(tmp_path: Path) -> None:
    target = tmp_path / "artifact.txt"
    target.write_text("harmless", encoding="utf-8")

    result = GitleaksScanner(tmp_path / "missing").scan(target)

    assert result.status is ScannerStatus.FAILED
    assert result.reason_code == "SCANNER_UNAVAILABLE"


def test_gitleaks_start_race_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An executable disappearing after capability path checks is not clean."""
    target = tmp_path / "artifact.txt"
    target.write_text("harmless", encoding="utf-8")
    scanner = GitleaksScanner(_fake_gitleaks(tmp_path, "clean"))

    def fail_start(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError("disappeared")

    monkeypatch.setattr(subprocess, "Popen", fail_start)

    result = scanner.scan(target)

    assert result.status is ScannerStatus.FAILED
    assert result.reason_code == "SCANNER_START_FAILED"


def test_gitleaks_reaps_residual_process_group(tmp_path: Path) -> None:
    """A scanner leader cannot return clean while its descendant remains alive."""
    target = tmp_path / "artifact.txt"
    target.write_text("harmless", encoding="utf-8")
    scanner = GitleaksScanner(_fake_gitleaks(tmp_path, "descendant"))

    result = scanner.scan(target)
    descendant = wait_pid_file(tmp_path / "gitleaks-descendant-descendant.pid")

    assert result.status is ScannerStatus.CLEAN
    assert process_exists(descendant) is False
