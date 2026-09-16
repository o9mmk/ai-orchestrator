"""Standalone invisible/bidi Unicode gate used inside the verifier sandbox."""

from __future__ import annotations

import sys
from pathlib import Path

_FORBIDDEN = (
    range(0x200B, 0x2010),
    range(0x2028, 0x2030),
    range(0x2060, 0x2065),
    range(0x2066, 0x206A),
    range(0xE0001, 0xE0080),
)


def _forbidden(character: str, *, offset: int) -> bool:
    codepoint = ord(character)
    if codepoint == 0xFEFF:
        return offset != 0
    return any(codepoint in values for values in _FORBIDDEN)


def scan(root: Path) -> tuple[str, ...]:
    """Return only affected relative paths; never echo file content."""
    base = root.resolve(strict=True)
    findings: list[str] = []
    for path in sorted(base.rglob("*")):
        if ".git" in path.parts or not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if any(_forbidden(character, offset=offset) for offset, character in enumerate(text)):
            findings.append(path.relative_to(base).as_posix())
    return tuple(findings)


def main(argv: list[str] | None = None) -> int:
    """Exit nonzero on invisible characters with bounded path-only output."""
    arguments = sys.argv[1:] if argv is None else argv
    root = Path(arguments[0]) if arguments else Path.cwd()
    findings = scan(root)
    if findings:
        sys.stdout.write(f"invisible_unicode_paths={len(findings)}\n")
        return 1
    sys.stdout.write("invisible_unicode_paths=0\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    raise SystemExit(main())
