"""Version-probed codex exec child adapter."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from orc.errors import CodexCapabilityError

REQUIRED_EXEC_FLAGS = (
    "--output-schema",
    "--sandbox",
    "--json",
    "--output-last-message",
    "--cd",
    "--ephemeral",
    "--ignore-user-config",
    "--ignore-rules",
    "--strict-config",
)


@dataclass(frozen=True)
class CodexCapabilities:
    """Exact executable/version/flag snapshot used for one Manager run."""

    executable: str
    version: str
    required_flags: tuple[str, ...]


class CodexExecAdapter:
    """Build only the non-interactive Codex command verified on this host."""

    def __init__(
        self,
        executable: str | Path,
        *,
        probe_timeout_seconds: float = 10.0,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        path = Path(executable).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise CodexCapabilityError(f"codex executable is unavailable: {path}")
        self.executable = path.resolve(strict=True)
        self.probe_timeout_seconds = probe_timeout_seconds
        self.environment = dict(environment) if environment is not None else _safe_environment()
        self._capabilities: CodexCapabilities | None = None

    def probe(self) -> CodexCapabilities:
        """Require every flag used by spawn; version drift is a hard stop."""
        if self._capabilities is not None:
            return self._capabilities
        version = self._run_probe([str(self.executable), "--version"])
        help_text = self._run_probe([str(self.executable), "exec", "--help"])
        missing = [flag for flag in REQUIRED_EXEC_FLAGS if flag not in help_text]
        if missing:
            raise CodexCapabilityError(
                f"codex exec required flags unavailable: {','.join(missing)}"
            )
        first_line = version.splitlines()[0][:500] if version.splitlines() else "unknown"
        self._capabilities = CodexCapabilities(
            str(self.executable),
            first_line,
            REQUIRED_EXEC_FLAGS,
        )
        return self._capabilities

    def spawn(
        self,
        *,
        worktree: Path,
        role: str,
        schema_path: Path,
        result_path: Path,
    ) -> subprocess.Popen[bytes]:
        """Start one new session with no writable directory beyond the workspace."""
        self.probe()
        sandbox_mode = {
            "planner": "read-only",
            "researcher": "read-only",
            "implementer": "workspace-write",
            "reviewer": "read-only",
        }.get(role)
        if sandbox_mode is None:
            raise ValueError(f"unsupported Codex child role: {role}")
        root = worktree.resolve(strict=True)
        for artifact in (schema_path, result_path):
            if not artifact.resolve(strict=False).is_relative_to(root):
                raise CodexCapabilityError("Codex adapter artifact path escaped worktree")
        command = [
            str(self.executable),
            "exec",
            "--strict-config",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "--sandbox",
            sandbox_mode,
            "--cd",
            str(root),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(result_path),
            "--json",
            "-",
        ]
        return subprocess.Popen(
            command,
            cwd=root,
            env=self.environment.copy(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    def _run_probe(self, command: list[str]) -> str:
        try:
            result = subprocess.run(
                command,
                env=self.environment.copy(),
                check=False,
                capture_output=True,
                text=True,
                timeout=self.probe_timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise CodexCapabilityError("codex capability probe timed out") from error
        if result.returncode != 0:
            raise CodexCapabilityError(
                f"codex capability probe failed: exit={result.returncode}"
            )
        return result.stdout or result.stderr


def _safe_environment() -> dict[str, str]:
    """Pass runtime essentials while excluding credentials and arbitrary repo env."""
    allowed = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "TERM")
    return {key: os.environ[key] for key in allowed if key in os.environ}
