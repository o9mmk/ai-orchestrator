"""sandbox-exec実機PoC: networkとworktree外書込の遮断。"""

from __future__ import annotations

import json
import platform
import socket
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from orc.errors import SandboxUnavailable, SandboxViolation
from orc.sandbox import SandboxRunner


@dataclass(frozen=True)
class SandboxPocResult:
    """再実行可能なPoC判定。"""

    platform: str
    python: str
    sandbox_exec_available: bool
    profile_id: str
    inside_write_allowed: bool
    outside_write_blocked: bool
    network_blocked: bool
    local_socket_blocked: bool
    outside_denial_observed: bool
    network_denial_observed: bool
    local_socket_denial_observed: bool
    error: str | None = None

    @property
    def passed(self) -> bool:
        """4つの必須probeが全て期待どおりかを返す。"""
        return (
            self.sandbox_exec_available
            and self.inside_write_allowed
            and self.outside_write_blocked
            and self.network_blocked
            and self.local_socket_blocked
            and self.outside_denial_observed
            and self.network_denial_observed
            and self.local_socket_denial_observed
        )


def run_sandbox_poc() -> SandboxPocResult:
    """外部networkに依存せずlocalhost listenerでnetwork denyを実証する。"""
    try:
        runner = SandboxRunner()
    except SandboxUnavailable:
        return SandboxPocResult(
            platform.platform(),
            platform.python_version(),
            False,
            "unavailable",
            False,
            False,
            False,
            False,
            False,
            False,
            False,
        )
    try:
        with tempfile.TemporaryDirectory(prefix="orc-sandbox-poc-") as temporary:
            root = Path(temporary)
            worktree = root / "worktree"
            worktree.mkdir()
            inside = worktree / "inside.txt"
            outside = root / "outside.txt"
            inside_result = runner.run(
                [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(inside)!r}).write_text('inside')",
                ],
                worktree,
                timeout_seconds=10,
            )
            outside_result = runner.run(
                [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(outside)!r}).write_text('outside')",
                ],
                worktree,
                timeout_seconds=10,
            )
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
                port = listener.getsockname()[1]
                network_result = runner.run(
                    [
                        sys.executable,
                        "-c",
                        (f"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=1)"),
                    ],
                    worktree,
                    timeout_seconds=10,
                )
            finally:
                listener.close()
            socket_path = worktree / "local.sock"
            local_socket_result = runner.run(
                [
                    sys.executable,
                    "-c",
                    (f"import socket; s=socket.socket(socket.AF_UNIX); s.bind({str(socket_path)!r})"),
                ],
                worktree,
                timeout_seconds=10,
            )
            return SandboxPocResult(
                platform.platform(),
                platform.python_version(),
                True,
                runner.profile_id,
                inside_result.exit_code == 0 and inside.read_text(encoding="utf-8") == "inside",
                outside_result.exit_code != 0 and not outside.exists(),
                network_result.exit_code != 0,
                local_socket_result.exit_code != 0 and not socket_path.exists(),
                outside_result.denial_detected,
                network_result.denial_detected,
                local_socket_result.denial_detected,
            )
    except (OSError, SandboxViolation) as error:
        return SandboxPocResult(
            platform.platform(),
            platform.python_version(),
            True,
            runner.profile_id,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            error=str(error),
        )


def main() -> int:
    """PoC JSONをstdoutへ返し、不合格を非0にする。"""
    result = run_sandbox_poc()
    print(json.dumps({**asdict(result), "passed": result.passed}, sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
