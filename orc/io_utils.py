"""監査artifact向けcanonical JSONと原子的書込。"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any


def canonical_json(data: Any) -> bytes:
    """hash入力用の決定論的UTF-8 JSONを返す。"""
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def atomic_write_json(path: Path, data: Any) -> None:
    """同一directory内の一時fileから0600で原子的に置換する。"""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.tmp-{secrets.token_hex(8)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        payload = canonical_json(data) + b"\n"
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
