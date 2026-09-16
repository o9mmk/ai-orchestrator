"""Real Git fixture and fake schema-bound Codex executable for M9."""

import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

from tests.strict_schema_helpers import STRICT_SCHEMA_CHECK_SOURCE


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "orc-test")
    git(path, "config", "user.email", "orc-test@example.invalid")
    (path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(path, "add", "app.py")
    git(path, "commit", "-m", "initial")
    return path


def plan(*, size: str = "S") -> dict[str, Any]:
    return {
        "tasks": [
            {
                "task_id": "task-1",
                "role": "implementer",
                "objective": "Change VALUE from 1 to 2",
                "path_scope": ["app.py"],
                "acceptance": ["app.py contains VALUE = 2"],
                "depends_on": [],
                "size_estimate": {
                    "estimated_files": 1,
                    "estimated_diff_lines": 1,
                    "estimated_invocations": 1,
                },
                "scope_confidence": "high",
                "commands": [],
            }
        ],
        "planner_size": size,
        "deterministic_size": "S",
        "final_size": size,
    }


def make_fake_codex(path: Path) -> Path:
    flags = " ".join(
        (
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
    )
    script = f"""\
    #!{sys.executable}
    import json
    import sys
    from pathlib import Path

    if sys.argv[1:] == ["--version"]:
        print("fake-codex 1.0")
        raise SystemExit(0)
    if sys.argv[1:] == ["exec", "--help"]:
        print({flags!r})
        raise SystemExit(0)
    args = sys.argv[1:]
    counter = Path(str(Path(sys.argv[0])) + ".count")
    count = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
    counter.write_text(str(count + 1), encoding="utf-8")
    schema_path = Path(args[args.index("--output-schema") + 1])
    output = Path(args[args.index("--output-last-message") + 1])
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    # STRICT_SCHEMA_CHECK
    reject_unsupported_schema(schema)
    prompt = sys.stdin.read()
    properties = schema.get("properties", {{}})
    if "claimed_status" in properties:
        Path("app.py").write_text("VALUE = 2\\n", encoding="utf-8")
        result = {{
            "task_id": "task-1",
            "role": "implementer",
            "attempt": 1,
            "claimed_status": "done",
            "summary": "bounded change",
            "changed_files": ["app.py"],
            "truncated": False,
            "context_requests_used": 0,
        }}
    else:
        raise SystemExit(9)
    output.write_text(json.dumps(result), encoding="utf-8")
    """
    script = textwrap.dedent(script).replace(
        "# STRICT_SCHEMA_CHECK", STRICT_SCHEMA_CHECK_SOURCE
    )
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path
