"""End-to-end candidate verification assembled from M3 components."""

from __future__ import annotations

import hashlib
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orc.artifact_ingest import ArtifactIngestor
from orc.baseline import BaselineVerifier, GateDecision, GateSpec
from orc.candidate import CandidatePatch
from orc.errors import VerificationError
from orc.sandbox import PLATFORM_UNENFORCEABLE_LIMITS, SandboxRunner
from orc.store import RunStateStore
from orc.verifier import (
    SecretScan,
    build_verify_report,
    check_patch_scope,
    require_mandatory_gates,
)
from orc.worktree import Worktree, WorktreeManager


@dataclass(frozen=True)
class VerificationOutcome:
    """Validated report and its individual deterministic gate decisions."""

    report: dict[str, Any]
    decisions: tuple[GateDecision, ...]


class TaskVerifierService:
    """Run identical baseline/candidate gates and persist a clean verify report."""

    def __init__(
        self,
        store: RunStateStore,
        ingestor: ArtifactIngestor,
        *,
        runner: SandboxRunner | None = None,
        allow_unenforced_limits: frozenset[str] = PLATFORM_UNENFORCEABLE_LIMITS,
    ) -> None:
        self.store = store
        self.ingestor = ingestor
        self.runner = runner or SandboxRunner()
        # 既定はplatformが強制できない上限だけを許す。これ以外の未適用と、
        # 適用状況を確認できなかった実行はgate側でINCONCLUSIVEへ落ちる。
        self.allow_unenforced_limits = allow_unenforced_limits

    def verify(
        self,
        candidate: Worktree,
        patch: CandidatePatch,
        task: dict[str, Any],
        *,
        attempt: int,
        child_changed_files: list[str],
    ) -> VerificationOutcome:
        """Verify scope, mandatory gates, and exact child-vs-Git file identity."""
        task_id = task["task_id"]
        scope = check_patch_scope(
            list(patch.changed_files),
            child_changed_files,
            task["path_scope"],
        )
        specs = _gate_specs(task)
        require_mandatory_gates(specs)
        base_commit = self.store.read_manifest()["base_commit"]
        baseline_id = (
            f"baseline-{hashlib.sha256(task_id.encode()).hexdigest()[:16]}-{attempt}"
        )
        baseline = WorktreeManager(self.store.repo_path, self.store.run_id).create(
            baseline_id,
            base_commit,
        )
        verifier = BaselineVerifier(
            self.store,
            self.runner,
            allow_unenforced_limits=self.allow_unenforced_limits,
        )
        decisions = tuple(
            verifier.verify(spec, base_commit, baseline.path, candidate.path) for spec in specs
        )
        report = build_verify_report(
            list(decisions),
            scope,
            SecretScan("gitleaks", 0, False),
            self.runner.profile_id,
        )
        assessment = self.ingestor.assess_manager_payload(
            "verify",
            f"{task_id}-attempt-{attempt}",
            report,
        )
        if not assessment.clean or assessment.clearance is None:
            raise VerificationError(f"verify_report_dlp_blocked:{assessment.reason_code}")
        self.store.write_verify_report(
            task_id,
            attempt,
            report,
            clearance=assessment.clearance,
        )
        return VerificationOutcome(report, decisions)


def _gate_specs(task: dict[str, Any]) -> list[GateSpec]:
    specs: list[GateSpec] = []
    for index, command in enumerate(task.get("commands", []), start=1):
        argv = tuple(shlex.split(command, posix=True))
        if not argv:
            raise VerificationError("empty task gate command")
        specs.append(GateSpec(f"task-{index}", argv))
    gitleaks = shutil.which("gitleaks")
    if gitleaks is None:
        raise VerificationError("mandatory gitleaks executable unavailable")
    specs.extend(
        (
            GateSpec(
                "gitleaks",
                (gitleaks, "dir", ".", "--no-banner", "--no-color", "--redact=100"),
            ),
            GateSpec(
                "glassworm",
                (
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("glassworm_gate.py")),
                    ".",
                ),
            ),
        )
    )
    return specs
