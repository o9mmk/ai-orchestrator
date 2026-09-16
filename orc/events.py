"""追記専用events.jsonl hash chain。"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import ValidationError

from orc.errors import TamperDetected
from orc.io_utils import canonical_json
from orc.schemas import validate_event

ZERO_HASH = "0" * 64


def _timestamp() -> str:
    """監査event用UTC timestampを返す。"""
    return datetime.now(UTC).isoformat()


class EventLog:
    """eventのappendと全chain検証を担う。"""

    def __init__(
        self,
        path: Path,
        run_id: str,
        *,
        timestamp: Callable[[], str] = _timestamp,
    ) -> None:
        self.path = path
        self.run_id = run_id
        self.timestamp = timestamp

    def append(
        self,
        event_type: str,
        actor: str,
        data: dict[str, Any],
        *,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """既存chainを先に検証し、次の1行をfsync付きで追記する。"""
        events = self.verify()
        previous = events[-1]["hash"] if events else ZERO_HASH
        body: dict[str, Any] = {
            "ts": self.timestamp(),
            "seq": len(events) + 1,
            "prev_hash": previous,
            "run_id": self.run_id,
            "type": event_type,
            "actor": actor,
            "data": data,
        }
        if task_id is not None:
            body["task_id"] = task_id
        digest = hashlib.sha256(previous.encode("ascii") + canonical_json(body)).hexdigest()
        event = {**body, "hash": digest}
        validate_event(event)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "ab") as handle:
            handle.write(canonical_json(event) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.path.chmod(0o600)
        return event

    def verify(self) -> list[dict[str, Any]]:
        """seq/prev_hash/hash/schemaの不一致を1件でも検出したら拒否する。"""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        events: list[dict[str, Any]] = []
        previous = ZERO_HASH
        for expected_seq, line in enumerate(lines, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise TamperDetected("tamper_detected: invalid events JSON") from error
            if not isinstance(event, dict):
                raise TamperDetected("tamper_detected: event must be an object")
            try:
                validate_event(event)
            except ValidationError as error:
                raise TamperDetected("tamper_detected: invalid event schema") from error
            supplied_hash = event["hash"]
            body = {key: value for key, value in event.items() if key != "hash"}
            expected_hash = hashlib.sha256(previous.encode("ascii") + canonical_json(body)).hexdigest()
            if (
                event["run_id"] != self.run_id
                or event["seq"] != expected_seq
                or event["prev_hash"] != previous
                or supplied_hash != expected_hash
            ):
                raise TamperDetected("tamper_detected: events hash chain mismatch")
            events.append(event)
            previous = supplied_hash
        return events
