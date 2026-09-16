"""M6 AT-15/AT-21 safe-metadata and Manager boundary acceptance tests."""

import json
from pathlib import Path

import pytest

from orc.artifact_ingest import ArtifactIngestor
from orc.dlp_models import DlpClearance, DlpStatus
from orc.manager_context import ValidatedVariable
from tests.m4_helpers import make_store, run_fake
from tests.m6_helpers import FakeScanner, dummy_api_key, dummy_email, dummy_phone


def _write(root: Path, name: str, body: str) -> None:
    (root / name).write_text(body, encoding="utf-8")


def _surface_bytes(root: Path, *, exclude_quarantine: bool = False) -> bytes:
    payload = bytearray()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if exclude_quarantine and "quarantine" in path.parts:
            continue
        payload.extend(path.read_bytes())
    return bytes(payload)


def test_at15_secret_patch_is_blocked_and_claimed_status_is_not_forwarded(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "parent"
    repo.mkdir()
    tracked = repo / "tracked.txt"
    tracked.write_bytes(b"parent-before\n")
    before = tracked.read_bytes()
    store = make_store(repo)
    staging = tmp_path / "child-staging"
    staging.mkdir()
    secret = dummy_api_key()
    _write(staging, "patch.diff", "+credential=" + secret)

    result = ArtifactIngestor(
        store, FakeScanner(), allowed_source_roots=(staging,)
    ).ingest(
        staging, Path("patch.diff"), "patch", "at15-patch"
    )

    assert result.status is DlpStatus.QUARANTINED
    assert result.clean is False
    assert tracked.read_bytes() == before
    surfaces = json.dumps(store.verify_events(), ensure_ascii=False)
    surfaces += (store.checkpoint_path.read_text(encoding="utf-8"))
    assert secret not in surfaces
    assert not (store.run_dir / "artifacts/at15-patch/patch.artifact").exists()


def test_at21_clean_patch_and_polluted_artifacts_are_separated(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_store(repo)
    staging = tmp_path / "staging"
    staging.mkdir()
    secret = dummy_api_key()
    email = dummy_email()
    phone = dummy_phone()
    _write(staging, "patch.diff", "+VALUE = 1\n")
    _write(staging, "stdout.log", "out=" + secret)
    _write(staging, "transcript.log", "contact=" + email)
    _write(staging, "findings.json", json.dumps({"phone": phone}))
    ingestor = ArtifactIngestor(store, FakeScanner(), allowed_source_roots=(staging,))

    results = (
        ingestor.ingest(staging, Path("patch.diff"), "patch", "at21-patch"),
        ingestor.ingest(staging, Path("stdout.log"), "stdout", "at21-stdout"),
        ingestor.ingest(staging, Path("transcript.log"), "transcript", "at21-transcript"),
        ingestor.ingest(staging, Path("findings.json"), "findings", "at21-findings"),
    )

    assert results[0].status is DlpStatus.CLEAN
    assert all(result.status is DlpStatus.QUARANTINED for result in results[1:])
    assert {category for result in results for category, _ in result.category_counts} >= {
        "API_KEY",
        "EMAIL",
        "PHONE",
    }
    normal = store.run_dir / "artifacts"
    assert (normal / "at21-patch/patch.artifact").exists()
    assert not (normal / "at21-stdout/stdout.artifact").exists()
    public = json.dumps(store.verify_events(), ensure_ascii=False)
    public += store.checkpoint_path.read_text(encoding="utf-8")
    assert all(value not in public for value in (secret, email, phone))


def test_manager_result_requires_matching_clean_dlp_clearance(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_store(repo)
    result_payload = {
        "task_id": "task-1",
        "role": "implementer",
        "attempt": 1,
        "claimed_status": "done",
        "summary": "bounded clean summary",
        "changed_files": [],
        "references": [],
        "truncated": False,
        "context_requests_used": 0,
    }
    ingestor = ArtifactIngestor(store, FakeScanner())
    assessed = ingestor.assess_manager_payload("result", "manager-result", result_payload)
    assert assessed.clearance is not None
    clearance = assessed.clearance

    variable = ValidatedVariable.from_result(
        result_payload,
        sequence=1,
        task_id="task-1",
        role="implementer",
        attempt=1,
        clearance=clearance,
        clearance_registry=store,
    )

    assert variable.kind == "result"
    with pytest.raises(TypeError):
        ValidatedVariable.from_result(  # type: ignore[call-arg]
            result_payload,
            sequence=1,
            task_id="task-1",
            role="implementer",
            attempt=1,
        )
    polluted = dict(result_payload)
    polluted["summary"] = dummy_api_key()
    blocked = ingestor.assess_manager_payload("result", "blocked-result", polluted)
    assert blocked.clean is False
    assert blocked.clearance is None
    forged = DlpClearance._issue(
        "f" * 64,
        clearance.artifact_kind,
        clearance.artifact_id,
        clearance.digest,
    )
    with pytest.raises(ValueError, match="not registered"):
        ValidatedVariable.from_result(
            result_payload,
            sequence=1,
            task_id="task-1",
            role="implementer",
            attempt=1,
            clearance=forged,
            clearance_registry=store,
        )
    with pytest.raises(TypeError):
        store.write_result_report(  # type: ignore[call-arg]
            "task-1", "implementer", 1, result_payload
        )


def test_dlp_processing_does_not_change_budget_counters(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_store(repo)
    before = store.read_budget()
    staging = tmp_path / "staging"
    staging.mkdir()
    _write(staging, "summary.txt", "harmless")

    ArtifactIngestor(
        store, FakeScanner(), allowed_source_roots=(staging,)
    ).ingest(
        staging, Path("summary.txt"), "summary", "budget-neutral"
    )

    assert store.read_budget() == before


def test_at15_fake_child_secret_patch_blocks_claimed_done(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = dummy_api_key()

    store, outcome = run_fake(tmp_path, monkeypatch, "secret_patch")

    assert outcome.status == "DLP_BLOCKED"
    assert outcome.manager_result is None
    assert outcome.task_state == "ESCALATED"
    assert outcome.dlp_status == DlpStatus.QUARANTINED.value
    assert not list((store.run_dir / "artifacts").rglob("patch.artifact"))
    assert secret.encode() not in _surface_bytes(store.run_dir, exclude_quarantine=True)
    quarantine_payloads = list((store.run_dir / "quarantine").rglob("payload"))
    assert any(secret.encode() in path.read_bytes() for path in quarantine_payloads)
    assert all((path.stat().st_mode & 0o777) == 0o600 for path in quarantine_payloads)


def test_unicode_escaped_result_secret_is_blocked_after_canonicalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A secret hidden from the raw scan by JSON escapes cannot reach Manager output."""
    secret = dummy_api_key()

    store, outcome = run_fake(tmp_path, monkeypatch, "escaped_result_secret")

    assert outcome.status == "DLP_BLOCKED"
    assert outcome.manager_result is None
    assert not list((store.run_dir / "tasks").rglob("result.json"))
    assert secret.encode() not in _surface_bytes(store.run_dir, exclude_quarantine=True)


def test_at21_fake_child_separates_clean_patch_from_polluted_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = (dummy_api_key(), dummy_email(), dummy_phone())

    store, outcome = run_fake(tmp_path, monkeypatch, "at21")

    assert outcome.status == "DLP_BLOCKED"
    assert outcome.manager_result is None
    patches = list((store.run_dir / "artifacts").rglob("patch.artifact"))
    assert len(patches) == 1
    assert patches[0].read_text(encoding="utf-8") == "+VALUE = 1\n"
    public = _surface_bytes(store.run_dir, exclude_quarantine=True)
    assert all(value.encode() not in public for value in values)
    events = store.verify_events()
    assert sum(event["type"] == "child_started" for event in events) == 1
    assert not any(
        event["type"] == "child_started"
        for event in events[1:]
        if event.get("task_id") != "task-1"
    )


def test_streaming_redaction_prevents_raw_stdout_and_stderr_disk_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = dummy_api_key()
    email = dummy_email()

    store, outcome = run_fake(tmp_path, monkeypatch, "stream_secret")

    assert outcome.status == "DLP_BLOCKED"
    surfaces = _surface_bytes(store.run_dir)
    assert secret.encode() not in surfaces
    assert email.encode() not in surfaces
    quarantine_payloads = [
        path.read_text(encoding="utf-8")
        for path in (store.run_dir / "quarantine").rglob("payload")
    ]
    assert any("[REDACTED:API_KEY]" in body for body in quarantine_payloads)
    assert any("[REDACTED:EMAIL]" in body for body in quarantine_payloads)


def test_simultaneous_large_stdout_stderr_does_not_deadlock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, outcome = run_fake(tmp_path, monkeypatch, "dual_large")

    assert outcome.status == "VALIDATED"
    stdout = list((store.run_dir / "artifacts").rglob("stdout.artifact"))
    stderr = list((store.run_dir / "artifacts").rglob("stderr.artifact"))
    assert len(stdout) == len(stderr) == 1
    assert stdout[0].stat().st_size == stderr[0].stat().st_size == 2048 * 512


def test_stream_output_limit_is_structured_and_blocks_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, outcome = run_fake(
        tmp_path,
        monkeypatch,
        "output_limit",
        stream_output_limit_bytes=128,
    )

    assert outcome.status == "DLP_BLOCKED"
    assert outcome.manager_result is None
    event = next(
        item
        for item in store.verify_events()
        if item["type"] == "dlp_artifact_processed"
        and item["data"]["artifact_kind"] == "stdout"
    )
    assert event["data"]["reason_code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert event["data"]["status"] == "QUARANTINED"
