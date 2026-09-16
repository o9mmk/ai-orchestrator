"""設計書§9 verify.json必須fieldのschemaテスト。"""

from pathlib import Path

import pytest
from jsonschema import ValidationError

from orc.lease import LeaseManager
from orc.store import RunStateStore
from orc.verify_schema import validate_verify_report
from tests.helpers import manifest_data
from tests.m6_helpers import make_clean_ingestor


def valid_report() -> dict[str, object]:
    """全必須fieldを持つverify reportを返す。"""
    return {
        "gates": [
            {
                "name": "pytest",
                "command": ["pytest", "-q"],
                "exit_code": 0,
                "baseline_result": {"exit_code": 0, "timed_out": False},
                "candidate_result": {"attempts": [{"exit_code": 0, "timed_out": False}]},
                "classification": "PASS",
                "log_digest": "a" * 64,
                "limits": {"verified": True, "unsupported": []},
            }
        ],
        "scope_check": {"patch_files": ["src/a.py"], "in_scope": True},
        "secret_scan": {"tool": "gitleaks", "findings_count": 0, "quarantined": False},
        "sandbox_profile": "sandbox-exec:test",
    }


def test_verify_report_accepts_all_required_fields() -> None:
    """設計書§9を満たすreportを受理する。"""
    validate_verify_report(valid_report())


def test_verify_report_rejects_missing_required_field() -> None:
    """必須field欠落を補修せずschema errorにする。"""
    report = valid_report()
    del report["sandbox_profile"]

    with pytest.raises(ValidationError):
        validate_verify_report(report)


def test_verify_report_is_fenced_and_checkpointed(repo: Path) -> None:
    """verify.jsonをtask attempt領域へ保存しdigest照合対象にする。"""
    manager = LeaseManager(repo)
    lease = manager.acquire("run-1")
    store = RunStateStore(repo, "run-1", manager, lease)
    store.initialize(manifest_data(repo, "run-1", lease.fencing_token))

    report = valid_report()
    assessment = make_clean_ingestor(store).assess_manager_payload(
        "verify", "verify-report", report
    )
    assert assessment.clearance is not None
    path = store.write_verify_report(
        "task-1",
        1,
        report,
        clearance=assessment.clearance,
    )

    assert path.is_file()
    assert path.stat().st_mode & 0o777 == 0o600
    store.verify_integrity()
