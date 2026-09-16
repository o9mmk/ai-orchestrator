"""test/build commandの静的副作用分類。"""

from __future__ import annotations

import shlex
from enum import StrEnum
from pathlib import PurePosixPath


class CommandRisk(StrEnum):
    """Preflight Stage 2で扱うcommand分類。"""

    SAFE = "SAFE"
    NETWORK = "NETWORK"
    EXTERNAL_WRITE = "EXTERNAL_WRITE"
    LONG_RUNNING = "LONG_RUNNING"
    PRODUCTION = "PRODUCTION"
    UNKNOWN = "UNKNOWN"


def classify_command(command: str) -> CommandRisk:
    """allowlistを先に適用し、不明なcommandを安全扱いしない。"""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return CommandRisk.UNKNOWN
    if not tokens or any(marker in command for marker in (";", "&&", "||", "`", "$(")):
        return CommandRisk.UNKNOWN
    lowered = [token.lower() for token in tokens]
    if any(word in lowered for word in ("production", "prod", "deploy")):
        return CommandRisk.PRODUCTION
    if lowered[0] in {"curl", "wget", "ssh", "scp"} or (
        lowered[0] in {"pip", "npm", "uv"} and "install" in lowered
    ):
        return CommandRisk.NETWORK
    if any(word in lowered for word in ("push", "publish", "upload")):
        return CommandRisk.EXTERNAL_WRITE
    if lowered[0] in {"sleep", "watch"}:
        return CommandRisk.LONG_RUNNING
    executable = PurePosixPath(lowered[0]).name
    safe = (
        executable in {"pytest", "ruff", "mypy", "gitleaks"}
        or (executable in {"python", "python3", "python3.12"} and lowered[1:3] == ["-m", "pytest"])
        or (executable == "git" and len(lowered) > 1 and lowered[1] in {"status", "diff"})
        or (executable == "node" and len(lowered) > 1 and lowered[1] == "--check")
    )
    if safe:
        return CommandRisk.SAFE
    return CommandRisk.UNKNOWN
