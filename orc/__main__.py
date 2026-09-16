"""`python -m orc` entrypoint."""

from __future__ import annotations

from orc.cli import main

if __name__ == "__main__":  # pragma: no cover - module entrypoint
    raise SystemExit(main())
