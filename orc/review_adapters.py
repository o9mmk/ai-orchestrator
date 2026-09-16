"""M7 structured reviewer CLI adapters with capability probes."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validate

from orc.codex_adapter import CodexCapabilities, CodexExecAdapter
from orc.errors import ClaudeCapabilityError, ReviewExecutionError
from orc.io_utils import canonical_json
from orc.usage import UsageValue, measure_usage

CLAUDE_REQUIRED_FLAGS = (
    "--print",
    "--output-format",
    "--json-schema",
    "--tools",
    "--permission-mode",
    "--safe-mode",
    "--no-session-persistence",
    "--strict-mcp-config",
)

PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok"],
    "properties": {"ok": {"const": True}},
}


@dataclass(frozen=True)
class ClaudeCapabilities:
    """Exact executable/version/flags and bounded probe usage."""

    executable: str
    version: str
    required_flags: tuple[str, ...]
    probe_usage: UsageValue


class ClaudeReviewAdapter:
    """Run Claude as a tool-less, non-persistent, schema-bound reviewer."""

    reviewer_name = "claude"
    result_source = "stdout"

    def __init__(
        self,
        executable: str | Path,
        *,
        probe_timeout_seconds: float = 30.0,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        path = Path(executable).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ClaudeCapabilityError(f"claude executable is unavailable: {path}")
        self.executable = path.resolve(strict=True)
        self.probe_timeout_seconds = probe_timeout_seconds
        self.environment = dict(environment) if environment is not None else _safe_environment()
        self._capabilities: ClaudeCapabilities | None = None
        self._probe_usage_pending = False

    def probe(self) -> ClaudeCapabilities:
        """Require the exact safe CLI surface and one minimal structured model call."""
        if self._capabilities is not None:
            return self._capabilities
        version = self._run_probe([str(self.executable), "--version"])
        help_text = self._run_probe([str(self.executable), "--help"])
        missing = [flag for flag in CLAUDE_REQUIRED_FLAGS if flag not in help_text]
        if missing:
            raise ClaudeCapabilityError(
                f"claude required flags unavailable: {','.join(missing)}"
            )
        prompt = "Return only the schema value indicating availability."
        output = self._run_probe(self._command(PROBE_SCHEMA), stdin=prompt)
        payload = self._extract_wrapper(output.encode("utf-8"))
        try:
            validate(instance=payload, schema=PROBE_SCHEMA)
        except ValidationError as error:
            raise ClaudeCapabilityError("claude minimal structured probe was invalid") from error
        first_line = version.splitlines()[0][:500] if version.splitlines() else "unknown"
        usage = measure_usage(
            validated_usage=None,
            byte_count=len(prompt.encode("utf-8")) + len(output.encode("utf-8")),
            invocation_count=1,
        )
        self._capabilities = ClaudeCapabilities(
            str(self.executable),
            first_line,
            CLAUDE_REQUIRED_FLAGS,
            usage,
        )
        self._probe_usage_pending = True
        return self._capabilities

    def take_probe_usage(self) -> UsageValue | None:
        """Return probe usage once so a run cannot double-count cached probing."""
        if not self._probe_usage_pending or self._capabilities is None:
            return None
        self._probe_usage_pending = False
        return self._capabilities.probe_usage

    def spawn(
        self,
        *,
        worktree: Path,
        role: str,
        schema_path: Path,
        result_path: Path,
    ) -> subprocess.Popen[bytes]:
        """Start a read-only reviewer with no tools, MCPs, hooks, or persistence."""
        if role != "reviewer":
            raise ValueError("Claude review adapter only accepts reviewer role")
        self.probe()
        root = worktree.resolve(strict=True)
        for artifact in (schema_path, result_path):
            if not artifact.resolve(strict=False).is_relative_to(root):
                raise ClaudeCapabilityError("Claude adapter artifact path escaped worktree")
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ClaudeCapabilityError("Claude review schema is unreadable") from error
        return subprocess.Popen(
            self._command(schema),
            cwd=root,
            env=self.environment.copy(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    def extract_payload(self, raw: bytes) -> bytes:
        """Extract structured_output without propagating invalid wrapper content."""
        return canonical_json(self._extract_wrapper(raw))

    def _command(self, schema: dict[str, Any]) -> list[str]:
        return [
            str(self.executable),
            "--print",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, separators=(",", ":"), ensure_ascii=False),
            "--tools",
            "",
            "--permission-mode",
            "dontAsk",
            "--safe-mode",
            "--no-session-persistence",
            "--strict-mcp-config",
        ]

    def _run_probe(self, command: list[str], *, stdin: str | None = None) -> str:
        try:
            result = subprocess.run(
                command,
                input=stdin,
                env=self.environment.copy(),
                check=False,
                capture_output=True,
                text=True,
                timeout=self.probe_timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise ClaudeCapabilityError("claude capability probe timed out") from error
        if result.returncode != 0:
            raise ClaudeCapabilityError(
                f"claude capability probe failed: exit={result.returncode}"
            )
        return result.stdout or result.stderr

    @staticmethod
    def _extract_wrapper(raw: bytes) -> dict[str, Any]:
        try:
            wrapper = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ReviewExecutionError("review wrapper was not valid JSON") from error
        payload = wrapper.get("structured_output") if isinstance(wrapper, dict) else None
        if not isinstance(payload, dict):
            raise ReviewExecutionError("review wrapper omitted structured_output")
        return payload


class CodexReviewAdapter:
    """Adapt the existing independent ephemeral Codex session to review output."""

    reviewer_name = "codex"
    result_source = "result"

    def __init__(self, adapter: CodexExecAdapter) -> None:
        self.adapter = adapter

    def probe(self) -> CodexCapabilities:
        return self.adapter.probe()

    def take_probe_usage(self) -> None:
        return None

    def spawn(
        self,
        *,
        worktree: Path,
        role: str,
        schema_path: Path,
        result_path: Path,
    ) -> subprocess.Popen[bytes]:
        return self.adapter.spawn(
            worktree=worktree,
            role=role,
            schema_path=schema_path,
            result_path=result_path,
        )

    @staticmethod
    def extract_payload(raw: bytes) -> bytes:
        return raw


def _safe_environment() -> dict[str, str]:
    allowed = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "TERM")
    return {key: os.environ[key] for key in allowed if key in os.environ}
