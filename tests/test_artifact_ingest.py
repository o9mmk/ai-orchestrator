"""M6 artifact DLP ingest, path safety, and quarantine tests."""

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from orc.artifact_ingest import ArtifactIngestor
from orc.attempt_staging import AttemptStaging
from orc.dlp_models import DlpStatus, ScannerResult, ScannerStatus
from orc.errors import ChildExecutionError, DlpBoundaryError, TamperDetected
from tests.m4_helpers import make_store
from tests.m6_helpers import FakeScanner, dummy_api_key


def _source(tmp_path: Path, body: str = "harmless patch\n") -> tuple[Path, Path]:
    root = tmp_path / "staging"
    root.mkdir()
    path = root / "artifact.txt"
    path.write_text(body, encoding="utf-8")
    return root, path


def test_clean_ingest_atomically_publishes_deterministic_metadata(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_store(repo)
    root, source = _source(tmp_path)
    ingestor = ArtifactIngestor(store, FakeScanner(), allowed_source_roots=(root,))

    first = ingestor.ingest(root, Path("artifact.txt"), "patch", "artifact-001")

    published = store.run_dir / "artifacts/artifact-001/patch.artifact"
    assert first.status is DlpStatus.CLEAN
    assert first.digest is not None
    assert first.size_bytes == source.stat().st_size
    assert published.read_text(encoding="utf-8") == "harmless patch\n"
    assert stat.S_IMODE(published.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.run_dir.stat().st_mode) == 0o700
    store.verify_integrity()


def test_normal_run_requires_original_private_quarantine_key(tmp_path: Path) -> None:
    """quarantineが空でもkeyの欠損・権限変更・差し替えを拒否する。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_store(repo)
    key_path = store.run_dir / ".quarantine-hmac-key"
    original = key_path.read_bytes()
    events = store.verify_events()
    commitment_events = [
        event
        for event in events
        if event["type"] == "quarantine_integrity_key_committed"
    ]

    assert len(original) == 32
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert len(commitment_events) == 1
    assert original.hex() not in json.dumps(commitment_events)

    key_path.chmod(0o644)
    with pytest.raises(TamperDetected, match="invalid quarantine integrity key"):
        store.verify_integrity()
    key_path.chmod(0o600)
    key_path.unlink()
    with pytest.raises(TamperDetected, match="quarantine integrity key missing"):
        store.verify_integrity()
    key_path.write_bytes(os.urandom(32))
    key_path.chmod(0o600)
    with pytest.raises(TamperDetected, match="quarantine integrity key replaced"):
        store.verify_integrity()


def test_secret_artifact_is_quarantined_without_normal_publish(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_store(repo)
    secret = dummy_api_key()
    root, source = _source(tmp_path, "added=" + secret)

    result = ArtifactIngestor(store, FakeScanner(), allowed_source_roots=(root,)).ingest(
        root, Path("artifact.txt"), "patch", "artifact-002"
    )

    quarantine = store.run_dir / "quarantine/artifact-002"
    assert result.status is DlpStatus.QUARANTINED
    assert result.quarantined is True
    assert result.digest is None
    assert not (store.run_dir / "artifacts/artifact-002/patch.artifact").exists()
    assert source.exists() is False
    assert stat.S_IMODE((quarantine / "payload").stat().st_mode) == 0o600
    assert stat.S_IMODE((quarantine / "manifest.json").stat().st_mode) == 0o600
    manifest = json.loads((quarantine / "manifest.json").read_text(encoding="utf-8"))
    checkpoint = json.loads(store.checkpoint_path.read_text(encoding="utf-8"))
    assert secret not in json.dumps(manifest)
    assert manifest["retention_days"] == 14
    assert secret not in json.dumps(store.verify_events())
    quarantine_digests = {
        key: value
        for key, value in checkpoint["artifact_digests"].items()
        if key.startswith("quarantine/")
    }
    assert set(quarantine_digests) == {
        "quarantine/artifact-002/manifest.json",
        "quarantine/artifact-002/payload",
    }
    assert hashlib.sha256((quarantine / "payload").read_bytes()).hexdigest() not in set(
        quarantine_digests.values()
    )
    store.verify_integrity()
    (quarantine / "payload").write_bytes(b"tampered")
    with pytest.raises(TamperDetected, match="artifact digest mismatch"):
        store.verify_integrity()


def test_scanner_failure_is_quarantined_and_not_clean(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_store(repo)
    root, _ = _source(tmp_path)
    failed = ScannerResult(
        ScannerStatus.FAILED,
        "fake-gitleaks",
        "8.test",
        "timeout",
        (),
        "SCANNER_TIMEOUT",
    )

    result = ArtifactIngestor(
        store, FakeScanner(failed), allowed_source_roots=(root,)
    ).ingest(
        root, Path("artifact.txt"), "summary", "artifact-003"
    )

    assert result.status is DlpStatus.SCAN_FAILED
    assert result.quarantined is True
    assert result.reason_code == "SCANNER_TIMEOUT"
    assert not (store.run_dir / "artifacts/artifact-003/summary.artifact").exists()


def test_non_utf8_artifact_is_scan_failed_and_quarantined(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_store(repo)
    root, source = _source(tmp_path)
    source.write_bytes(bytes((0xFF, 0xFE, 0x00)))

    result = ArtifactIngestor(
        store, FakeScanner(), allowed_source_roots=(root,)
    ).ingest(root, Path("artifact.txt"), "transcript", "binary-artifact")

    assert result.status is DlpStatus.SCAN_FAILED
    assert result.reason_code == "CONTENT_DECODE_FAILED"
    assert result.quarantined is True
    assert not (store.run_dir / "artifacts/binary-artifact/transcript.artifact").exists()


@pytest.mark.parametrize("relative", [Path("/absolute"), Path("../escape"), Path("a/../../b")])
def test_path_escape_inputs_are_rejected(tmp_path: Path, relative: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_store(repo)
    root, _ = _source(tmp_path)

    with pytest.raises(DlpBoundaryError):
        ArtifactIngestor(
            store, FakeScanner(), allowed_source_roots=(root,)
        ).ingest(root, relative, "patch", "artifact-004")


def test_source_outside_manager_staging_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_store(repo)
    root, _ = _source(tmp_path)

    with pytest.raises(DlpBoundaryError, match="OUTSIDE_STAGING"):
        ArtifactIngestor(store, FakeScanner()).ingest(
            root, Path("artifact.txt"), "patch", "outside-staging"
        )


def test_secret_like_artifact_id_is_rejected_before_path_creation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_store(repo)
    root, _ = _source(tmp_path)

    with pytest.raises(ValueError, match="opaque"):
        ArtifactIngestor(
            store, FakeScanner(), allowed_source_roots=(root,)
        ).ingest(root, Path("artifact.txt"), "patch", dummy_api_key())

    assert not (store.run_dir / "artifacts").exists()


def test_symlink_hardlink_nonregular_and_oversize_sources_are_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_store(repo)
    root, source = _source(tmp_path)
    symlink = root / "link.txt"
    symlink.symlink_to(source)
    hardlink = root / "hard.txt"
    os.link(source, hardlink)
    fifo = root / "pipe"
    os.mkfifo(fifo)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "nested.txt").write_text("harmless", encoding="utf-8")
    (root / "nested").symlink_to(outside, target_is_directory=True)
    ingestor = ArtifactIngestor(
        store,
        FakeScanner(),
        max_artifact_bytes=4,
        allowed_source_roots=(root,),
    )

    for relative in (Path("link.txt"), Path("hard.txt"), Path("pipe"), Path("artifact.txt")):
        with pytest.raises(DlpBoundaryError):
            ingestor.ingest(root, relative, "patch", "artifact-005")
    with pytest.raises(DlpBoundaryError):
        ingestor.ingest(root, Path("nested/nested.txt"), "patch", "artifact-005")


def test_quarantine_failure_never_leaves_normal_artifact(tmp_path: Path) -> None:
    class FailingQuarantine:
        def save(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise OSError("unsafe raw detail must not escape")

    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_store(repo)
    root, source = _source(tmp_path, dummy_api_key())
    ingestor = ArtifactIngestor(
        store,
        FakeScanner(),
        quarantine=FailingQuarantine(),
        allowed_source_roots=(root,),
    )

    result = ingestor.ingest(root, Path("artifact.txt"), "patch", "artifact-006")

    assert result.status is DlpStatus.SCAN_FAILED
    assert result.reason_code == "QUARANTINE_FAILED"
    assert result.quarantined is False
    assert source.exists() is True
    assert not (store.run_dir / "artifacts/artifact-006/patch.artifact").exists()
    assert "unsafe raw detail" not in repr(result)


def test_staging_cleanup_never_follows_replaced_directory(tmp_path: Path) -> None:
    """A child directory swap cannot redirect Manager cleanup outside staging."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "result.json"
    sentinel.write_text("keep", encoding="utf-8")
    staging = AttemptStaging(worktree, 1)
    staging.create()
    moved = worktree / "moved-original"
    staging.path.rename(moved)
    staging.path.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ChildExecutionError, match="staging directory was replaced"):
        staging.cleanup()

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert staging.path.is_symlink()


def test_checkpoint_failure_rolls_back_normal_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artifact publication cannot survive a failed fenced event/checkpoint commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    store = make_store(repo)
    root, _ = _source(tmp_path)
    ingestor = ArtifactIngestor(store, FakeScanner(), allowed_source_roots=(root,))
    def fail_checkpoint(**_kwargs):  # type: ignore[no-untyped-def]
        raise OSError("checkpoint failed")

    monkeypatch.setattr(store, "_write_checkpoint", fail_checkpoint)

    with pytest.raises(OSError, match="checkpoint failed"):
        ingestor.ingest(root, Path("artifact.txt"), "patch", "rollback-clean")

    assert not (store.run_dir / "artifacts/rollback-clean").exists()
    events = store.verify_events()
    assert events[-2]["type"] == "dlp_artifact_processed"
    assert events[-1]["type"] == "dlp_artifact_rolled_back"
    assert events[-1]["data"] == {
        "artifact_kind": "patch",
        "artifact_id": "rollback-clean",
        "reason_code": "CHECKPOINT_FAILED",
    }
