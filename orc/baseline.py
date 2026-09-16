"""base/candidate gate比較とflaky再実行分類。"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from orc.errors import TamperDetected
from orc.io_utils import canonical_json
from orc.sandbox import SandboxResult
from orc.store import RunStateStore


class GateClassification(StrEnum):
    """設計書§10.1のbaseline分類。"""

    PASS = "PASS"
    REGRESSION = "REGRESSION"
    BASELINE_FAILED = "BASELINE_FAILED"
    FIXED_EXISTING_FAILURE = "FIXED_EXISTING_FAILURE"
    FLAKY = "FLAKY"
    INCONCLUSIVE = "INCONCLUSIVE"


class GateAction(StrEnum):
    """task状態機械へ渡す決定論action。"""

    CONTINUE = "CONTINUE"
    RETRY = "RETRY"
    ESCALATE = "ESCALATE"


@dataclass(frozen=True)
class GateSpec:
    """同一条件でbase/candidateへ適用するgate。"""

    name: str
    command: tuple[str, ...]
    timeout_seconds: int = 900

    def __post_init__(self) -> None:
        if not self.name or not self.command or self.timeout_seconds <= 0:
            raise ValueError("gate name, command and positive timeout are required")


@dataclass(frozen=True)
class GateRun:
    """raw logを永続化しないgate実行記録。"""

    exit_code: int
    timed_out: bool
    log_digest: str
    denial_detected: bool
    profile_id: str

    @classmethod
    def from_sandbox(cls, result: SandboxResult) -> GateRun:
        """sandbox結果をdigest中心の監査recordへ変換する。"""
        return cls(
            result.exit_code,
            result.timed_out,
            result.log_digest,
            result.denial_detected,
            result.profile_id,
        )


@dataclass(frozen=True)
class GateDecision:
    """baseline 1回とcandidate最大2回の分類結果。"""

    spec: GateSpec
    baseline: GateRun
    candidate_attempts: tuple[GateRun, ...]
    classification: GateClassification

    @property
    def action(self) -> GateAction:
        """flaky/inconclusiveを自動成功させずESCALATEへ送る。"""
        if self.classification is GateClassification.REGRESSION:
            return GateAction.RETRY
        if self.classification in {
            GateClassification.FLAKY,
            GateClassification.INCONCLUSIVE,
        }:
            return GateAction.ESCALATE
        return GateAction.CONTINUE

    def summary_line(self) -> str:
        """人間承認summaryへ分類を欠落なく渡す1行を返す。"""
        return f"- {self.spec.name}: {self.classification.value} ({self.action.value})"

    def as_verify_gate(self) -> dict[str, Any]:
        """verify.json gate schemaへ変換する。"""
        latest = self.candidate_attempts[-1]
        combined_digest = hashlib.sha256(
            canonical_json(
                [self.baseline.log_digest] + [attempt.log_digest for attempt in self.candidate_attempts]
            )
        ).hexdigest()
        return {
            "name": self.spec.name,
            "command": list(self.spec.command),
            "exit_code": latest.exit_code,
            "baseline_result": {
                "exit_code": self.baseline.exit_code,
                "timed_out": self.baseline.timed_out,
            },
            "candidate_result": {
                "attempts": [
                    {"exit_code": run.exit_code, "timed_out": run.timed_out}
                    for run in self.candidate_attempts
                ]
            },
            "classification": self.classification.value,
            "log_digest": combined_digest,
        }


class GateRunner(Protocol):
    """SandboxRunnerとtest doubleの共通interface。"""

    profile_id: str

    def run(
        self,
        command: list[str] | tuple[str, ...],
        worktree: Path,
        *,
        timeout_seconds: int,
    ) -> SandboxResult:
        """gateを指定worktree内で実行する。"""


class BaselineVerifier:
    """baseline cacheとcandidate rerunを決定論的に制御する。"""

    def __init__(self, store: RunStateStore, runner: GateRunner) -> None:
        self.store = store
        self.runner = runner

    def verify(
        self,
        spec: GateSpec,
        base_commit: str,
        base_worktree: Path,
        candidate_worktree: Path,
    ) -> GateDecision:
        """同一gateをbase/candidateへ適用し、条件表どおり分類する。"""
        baseline = self._baseline(spec, base_commit, base_worktree)
        attempts = [self._run(spec, candidate_worktree)]
        first = attempts[0]
        if baseline.timed_out or first.timed_out:
            classification = GateClassification.INCONCLUSIVE
        elif first.exit_code == 0:
            classification = (
                GateClassification.PASS
                if baseline.exit_code == 0
                else GateClassification.FIXED_EXISTING_FAILURE
            )
        else:
            attempts.append(self._run(spec, candidate_worktree))
            second = attempts[1]
            if second.timed_out:
                classification = GateClassification.INCONCLUSIVE
            elif second.exit_code == 0:
                classification = GateClassification.FLAKY
            elif baseline.exit_code == 0:
                classification = GateClassification.REGRESSION
            else:
                classification = GateClassification.BASELINE_FAILED
        return GateDecision(spec, baseline, tuple(attempts), classification)

    def _baseline(self, spec: GateSpec, base_commit: str, worktree: Path) -> GateRun:
        config_hash = hashlib.sha256(
            canonical_json(
                {
                    "name": spec.name,
                    "command": spec.command,
                    "timeout_seconds": spec.timeout_seconds,
                    "profile_id": self.runner.profile_id,
                }
            )
        ).hexdigest()
        self.store.verify_integrity()
        cached = self.store.read_baseline_cache(base_commit, spec.name)
        if cached is not None and cached.get("config_hash") == config_hash:
            return self._cached_run(cached)
        result = self._run(spec, worktree)
        self.store.write_baseline_cache(
            base_commit,
            spec.name,
            {"config_hash": config_hash, "result": asdict(result)},
        )
        return result

    def _run(self, spec: GateSpec, worktree: Path) -> GateRun:
        return GateRun.from_sandbox(
            self.runner.run(
                spec.command,
                worktree,
                timeout_seconds=spec.timeout_seconds,
            )
        )

    def _cached_run(self, cached: dict[str, Any]) -> GateRun:
        result = cached.get("result")
        required = {"exit_code", "timed_out", "log_digest", "denial_detected", "profile_id"}
        if not isinstance(result, dict) or set(result) != required:
            raise TamperDetected("tamper_detected: invalid baseline cache result")
        if (
            not isinstance(result["exit_code"], int)
            or isinstance(result["exit_code"], bool)
            or not isinstance(result["timed_out"], bool)
            or not isinstance(result["denial_detected"], bool)
            or not isinstance(result["profile_id"], str)
            or result["profile_id"] != self.runner.profile_id
            or not isinstance(result["log_digest"], str)
            or len(result["log_digest"]) != 64
            or any(character not in "0123456789abcdef" for character in result["log_digest"])
        ):
            raise TamperDetected("tamper_detected: invalid baseline log digest")
        return GateRun(**result)
