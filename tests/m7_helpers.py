"""Fake reviewer CLIs and bounded M7 fixtures."""

import stat
import sys
import textwrap
from pathlib import Path

from orc.review_bundle import ReviewBundle, build_review_bundle
from tests.strict_schema_helpers import STRICT_SCHEMA_CHECK_SOURCE


def make_review_bundle(*, objective: str = "Review the bounded change") -> ReviewBundle:
    """Return a small allowlisted review bundle."""
    return build_review_bundle(
        task_id="task-1",
        objective=objective,
        acceptance=("tests pass",),
        patch="diff --git a/app.py b/app.py\n+value = 1\n",
        related_context=("app.py:1",),
        verify={"classification": "PASS"},
    )


def make_fake_claude(tmp_path: Path, mode: str, input_digest: str) -> Path:
    """Create a fake Claude CLI with a real probe call and structured wrapper."""
    executable = tmp_path / f"fake-claude-{mode}"
    help_text = " ".join(
        [
            "--print",
            "--output-format",
            "--json-schema",
            "--tools",
            "--permission-mode",
            "--safe-mode",
            "--no-session-persistence",
            "--strict-mcp-config",
        ]
    )
    if mode == "missing_flags":
        help_text = "--print --json-schema"
    script = f"""\
    #!{sys.executable}
    import json
    import os
    import sys
    from pathlib import Path

    MODE = {mode!r}
    if sys.argv[1:] == ["--version"]:
        print("fake-claude 1.0")
        raise SystemExit(0)
    if sys.argv[1:] == ["--help"]:
        print({help_text!r})
        raise SystemExit(0)

    args = sys.argv[1:]
    if "FAKE_REVIEW_ARGV_LOG" in os.environ:
        path = Path(os.environ["FAKE_REVIEW_ARGV_LOG"])
        previous = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(previous + json.dumps(args) + "\\n", encoding="utf-8")
    schema = json.loads(args[args.index("--json-schema") + 1])
    prompt = sys.stdin.read()
    if "ok" in schema.get("properties", {{}}):
        if MODE == "probe_fail":
            raise SystemExit(9)
        print(json.dumps({{"structured_output": {{"ok": True}}}}))
        raise SystemExit(0)
    if MODE == "review_fail":
        raise SystemExit(8)
    if MODE == "invalid_review":
        print(json.dumps({{"structured_output": {{"body": "UNTRUSTED_REVIEW_BODY"}}}}))
        raise SystemExit(0)
    report = {{
        "verdict": "approve",
        "findings": [],
        "reviewed_by": "claude",
        "input_digest": {input_digest!r},
    }}
    print(json.dumps({{"structured_output": report}}))
    """
    executable.write_text(textwrap.dedent(script), encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def make_fake_codex_reviewer(tmp_path: Path, mode: str, input_digest: str) -> Path:
    """Create a fake Codex CLI that writes review.json to output-last-message."""
    executable = tmp_path / f"fake-codex-reviewer-{mode}"
    help_text = " ".join(
        [
            "--output-schema",
            "--sandbox",
            "--json",
            "--output-last-message",
            "--cd",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
        ]
    )
    script = f"""\
    #!{sys.executable}
    import json
    import os
    import sys
    from pathlib import Path

    MODE = {mode!r}
    if sys.argv[1:] == ["--version"]:
        print("fake-codex 1.0")
        raise SystemExit(0)
    if sys.argv[1:] == ["exec", "--help"]:
        print({help_text!r})
        raise SystemExit(0)

    if MODE == "review_fail":
        raise SystemExit(7)
    args = sys.argv[1:]
    assert "--ignore-rules" in args
    output = Path(args[args.index("--output-last-message") + 1])
    schema = Path(args[args.index("--output-schema") + 1])
    schema_data = json.loads(schema.read_text(encoding="utf-8"))
    # STRICT_SCHEMA_CHECK
    reject_unsupported_schema(schema_data)
    sys.stdin.read()
    if "FAKE_CODEX_REVIEW_MARKER" in os.environ:
        Path(os.environ["FAKE_CODEX_REVIEW_MARKER"]).write_text("spawned", encoding="utf-8")
    if MODE == "invalid_review":
        output.write_text("UNTRUSTED_CODEX_REVIEW_BODY{{", encoding="utf-8")
    else:
        output.write_text(json.dumps({{
            "verdict": "approve",
            "findings": [],
            "reviewed_by": "codex",
            "input_digest": {input_digest!r},
        }}), encoding="utf-8")
    """
    script = textwrap.dedent(script).replace(
        "# STRICT_SCHEMA_CHECK", STRICT_SCHEMA_CHECK_SOURCE
    )
    executable.write_text(script, encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable
