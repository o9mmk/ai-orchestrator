"""Fail-closed gitleaks 8 directory/file scanner adapter."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from orc.dlp_models import ScannerResult, ScannerStatus
from orc.process_control import process_group_alive, terminate_known_group

_REQUIRED_HELP = ("--report-format", "--report-path", "--redact", "--no-banner")


@dataclass(frozen=True)
class _ProcessResult:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    output_limited: bool


class GitleaksScanner:
    """Invoke a verified local gitleaks binary and discard every raw report field."""

    def __init__(
        self,
        executable: str | Path,
        *,
        timeout_seconds: float = 30.0,
        output_limit_bytes: int = 256 * 1024,
        report_limit_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self.executable = Path(executable).expanduser()
        self.timeout_seconds = timeout_seconds
        self.output_limit_bytes = output_limit_bytes
        self.report_limit_bytes = report_limit_bytes
        self._version: str | None = None

    @classmethod
    def discover(cls) -> GitleaksScanner:
        """Create the production adapter without treating absence as clean."""
        found = shutil.which("gitleaks")
        return cls(found if found is not None else Path("/__orc_missing_gitleaks__"))

    def scan(self, path: Path) -> ScannerResult:
        """Scan one regular artifact and return safe metadata for every failure."""
        capability = self._probe()
        if capability is not None:
            return capability
        version = self._version or "unknown"
        try:
            source = self._read_regular_file(path)
        except OSError:
            return self._failed(version, "source", "SCANNER_SOURCE_UNREADABLE")
        with tempfile.TemporaryDirectory(prefix="orc-gitleaks-") as temporary_name:
            temporary = Path(temporary_name)
            temporary.chmod(0o700)
            target = temporary / "payload"
            report = temporary / "report.json"
            _write_private(target, source)
            _write_private(report, b"")
            command = [
                str(self.executable),
                "dir",
                "--no-banner",
                "--no-color",
                "--redact=100",
                "--ignore-gitleaks-allow",
                "--log-level",
                "error",
                "--report-format",
                "json",
                "--report-path",
                str(report),
                "--exit-code",
                "1",
                str(target),
            ]
            try:
                process = _run_bounded(
                    command,
                    timeout_seconds=self.timeout_seconds,
                    output_limit_bytes=self.output_limit_bytes,
                    cwd=temporary,
                )
            except OSError:
                return self._failed(version, "start", "SCANNER_START_FAILED")
            if process.timed_out:
                return self._failed(version, "timeout", "SCANNER_TIMEOUT")
            if process.output_limited:
                return self._failed(version, "output_limit", "SCANNER_OUTPUT_LIMIT")
            if process.exit_code not in {0, 1}:
                return self._failed(version, str(process.exit_code), "SCANNER_EXIT_UNKNOWN")
            try:
                if report.stat().st_size > self.report_limit_bytes:
                    return self._failed(version, str(process.exit_code), "SCANNER_REPORT_LIMIT")
                raw_report = report.read_bytes()
                parsed = json.loads(raw_report)
                if not isinstance(parsed, list) or any(not isinstance(item, dict) for item in parsed):
                    raise ValueError("report shape")
            except FileNotFoundError:
                return self._failed(version, str(process.exit_code), "SCANNER_REPORT_MISSING")
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                return self._failed(version, str(process.exit_code), "SCANNER_REPORT_MALFORMED")
            findings = len(parsed)
            if (process.exit_code == 0 and findings) or (process.exit_code == 1 and not findings):
                return self._failed(version, str(process.exit_code), "SCANNER_REPORT_INCONSISTENT")
            if findings:
                return ScannerResult(
                    ScannerStatus.FINDINGS,
                    "gitleaks",
                    version,
                    str(process.exit_code),
                    (("SECRET", findings),),
                    "SECRET_DETECTED",
                )
            return ScannerResult(
                ScannerStatus.CLEAN,
                "gitleaks",
                version,
                str(process.exit_code),
                (),
                "CLEAN",
            )

    def _probe(self) -> ScannerResult | None:
        if self._version is not None:
            return None
        if not self.executable.is_file() or not os.access(self.executable, os.X_OK):
            return self._failed("unavailable", "unavailable", "SCANNER_UNAVAILABLE")
        try:
            version_result = _run_bounded(
                [str(self.executable), "version"],
                timeout_seconds=self.timeout_seconds,
                output_limit_bytes=self.output_limit_bytes,
                cwd=self.executable.parent,
            )
        except OSError:
            return self._failed("unknown", "start", "SCANNER_START_FAILED")
        if version_result.timed_out:
            return self._failed("unknown", "timeout", "SCANNER_TIMEOUT")
        if version_result.output_limited:
            return self._failed("unknown", "output_limit", "SCANNER_OUTPUT_LIMIT")
        if version_result.exit_code != 0:
            return self._failed("unknown", str(version_result.exit_code), "SCANNER_PROBE_FAILED")
        version_text = (version_result.stdout or version_result.stderr).decode(
            "utf-8", errors="replace"
        )
        match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", version_text)
        if match is None:
            return self._failed("unknown", "version", "SCANNER_VERSION_UNSUPPORTED")
        version = ".".join(match.groups())
        if int(match.group(1)) != 8:
            return self._failed(version, "version", "SCANNER_VERSION_UNSUPPORTED")
        try:
            help_result = _run_bounded(
                [str(self.executable), "dir", "--help"],
                timeout_seconds=self.timeout_seconds,
                output_limit_bytes=self.output_limit_bytes,
                cwd=self.executable.parent,
            )
        except OSError:
            return self._failed(version, "start", "SCANNER_START_FAILED")
        if help_result.timed_out:
            return self._failed(version, "timeout", "SCANNER_TIMEOUT")
        if help_result.output_limited:
            return self._failed(version, "output_limit", "SCANNER_OUTPUT_LIMIT")
        help_text = (help_result.stdout or help_result.stderr).decode("utf-8", errors="replace")
        if help_result.exit_code != 0 or any(flag not in help_text for flag in _REQUIRED_HELP):
            return self._failed(version, "capability", "SCANNER_CAPABILITY_UNSUPPORTED")
        self._version = version
        return None

    @staticmethod
    def _read_regular_file(path: Path) -> bytes:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("scanner source boundary rejected")
        return path.read_bytes()

    @staticmethod
    def _failed(version: str, result_code: str, reason: str) -> ScannerResult:
        return ScannerResult(
            ScannerStatus.FAILED,
            "gitleaks",
            version,
            result_code,
            (),
            reason,
        )


def _run_bounded(
    command: list[str],
    *,
    timeout_seconds: float,
    output_limit_bytes: int,
    cwd: Path,
) -> _ProcessResult:
    if timeout_seconds <= 0 or output_limit_bytes <= 0:
        raise ValueError("scanner timeout and output limit must be positive")
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    pgid = os.getpgid(process.pid)
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    limited = threading.Event()

    def drain(name: str, pipe) -> None:  # type: ignore[no-untyped-def]
        try:
            while True:
                chunk = pipe.read(8192)
                if not chunk:
                    return
                remaining = output_limit_bytes - len(buffers[name])
                if remaining > 0:
                    buffers[name].extend(chunk[:remaining])
                if len(chunk) > remaining:
                    limited.set()
        finally:
            pipe.close()

    threads = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_known_group(
            process,
            pgid,
            grace_seconds=0.1,
            poll_interval=0.01,
        )
    else:
        if process_group_alive(pgid):
            terminate_known_group(
                process,
                pgid,
                grace_seconds=0.1,
                poll_interval=0.01,
            )
    for thread in threads:
        thread.join(timeout=2)
    if any(thread.is_alive() for thread in threads):
        limited.set()
    return _ProcessResult(
        process.returncode,
        bytes(buffers["stdout"]),
        bytes(buffers["stderr"]),
        timed_out,
        limited.is_set(),
    )


def _write_private(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
