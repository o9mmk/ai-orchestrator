"""設計書AT-20 baseline/flaky分類のテスト。"""

import sys
from collections import defaultdict
from pathlib import Path

import pytest

from orc.baseline import BaselineVerifier, GateAction, GateClassification, GateSpec
from orc.lease import LeaseManager
from orc.sandbox import SandboxResult, SandboxRunner
from orc.store import RunStateStore
from tests.helpers import manifest_data


def sandbox_result(exit_code: int, *, timed_out: bool = False) -> SandboxResult:
    """gate runner用の最小結果を返す。"""
    return SandboxResult(
        command=("pytest", "-q"),
        exit_code=exit_code,
        timed_out=timed_out,
        stdout="",
        stderr="",
        profile_id="test-profile",
        denial_detected=False,
    )


class SequenceRunner:
    """cwdごとに結果列を返すdeterministic test double。"""

    profile_id = "test-profile"

    def __init__(self, results: dict[Path, list[SandboxResult]]) -> None:
        self.results = {path: list(values) for path, values in results.items()}
        self.calls: defaultdict[Path, int] = defaultdict(int)

    def run(
        self,
        command: list[str] | tuple[str, ...],
        worktree: Path,
        *,
        timeout_seconds: int,
    ) -> SandboxResult:
        """登録順に結果を返す。"""
        del command, timeout_seconds
        self.calls[worktree] += 1
        return self.results[worktree].pop(0)


@pytest.fixture
def store(repo: Path) -> RunStateStore:
    """baseline cacheを書けるlease取得済みstoreを返す。"""
    manager = LeaseManager(repo)
    lease = manager.acquire("run-1")
    result = RunStateStore(repo, "run-1", manager, lease)
    result.initialize(manifest_data(repo, "run-1", lease.fencing_token))
    return result


def verify(
    store: RunStateStore,
    tmp_path: Path,
    base_results: list[SandboxResult],
    candidate_results: list[SandboxResult],
) -> tuple[GateClassification, SequenceRunner]:
    """1 gateを指定result列で分類する。"""
    base = tmp_path / "base"
    candidate = tmp_path / "candidate"
    base.mkdir()
    candidate.mkdir()
    runner = SequenceRunner({base: base_results, candidate: candidate_results})
    verifier = BaselineVerifier(store, runner)
    decision = verifier.verify(
        GateSpec("pytest", ("pytest", "-q"), timeout_seconds=10),
        base_commit="a" * 40,
        base_worktree=base,
        candidate_worktree=candidate,
    )
    return decision.classification, runner


def test_at20_existing_failure_is_not_regression(store: RunStateStore, tmp_path: Path) -> None:
    """base/candidate同一failはBASELINE_FAILEDとして人間判断へ残す。"""
    classification, _runner = verify(
        store,
        tmp_path,
        [sandbox_result(1)],
        [sandbox_result(1), sandbox_result(1)],
    )

    assert classification is GateClassification.BASELINE_FAILED


def test_at20_flipped_rerun_is_flaky(store: RunStateStore, tmp_path: Path) -> None:
    """candidate fail後の1回再実行でpassへ反転したらFLAKYとする。"""
    classification, _runner = verify(
        store,
        tmp_path,
        [sandbox_result(0)],
        [sandbox_result(1), sandbox_result(0)],
    )

    assert classification is GateClassification.FLAKY


def test_flaky_action_escalates_and_summary_discloses_classification(
    store: RunStateStore,
    tmp_path: Path,
) -> None:
    """FLAKYはESCALATEとなり、人間向けsummaryから分類を落とさない。"""
    base = tmp_path / "base"
    candidate = tmp_path / "candidate"
    base.mkdir()
    candidate.mkdir()
    runner = SequenceRunner({base: [sandbox_result(0)], candidate: [sandbox_result(1), sandbox_result(0)]})
    decision = BaselineVerifier(store, runner).verify(
        GateSpec("pytest", ("pytest", "-q"), timeout_seconds=10),
        "a" * 40,
        base,
        candidate,
    )

    assert decision.action is GateAction.ESCALATE
    assert "FLAKY" in decision.summary_line()


def test_baseline_failed_summary_is_explicit(store: RunStateStore, tmp_path: Path) -> None:
    """既存failを自動passにせずsummary行へ明記する。"""
    base = tmp_path / "base"
    candidate = tmp_path / "candidate"
    base.mkdir()
    candidate.mkdir()
    runner = SequenceRunner({base: [sandbox_result(1)], candidate: [sandbox_result(1), sandbox_result(1)]})
    decision = BaselineVerifier(store, runner).verify(
        GateSpec("pytest", ("pytest", "-q"), timeout_seconds=10),
        "a" * 40,
        base,
        candidate,
    )

    assert "BASELINE_FAILED" in decision.summary_line()


@pytest.mark.parametrize(
    ("base_exit", "candidate_exits", "expected"),
    [
        (0, [1, 1], GateClassification.REGRESSION),
        (1, [0], GateClassification.FIXED_EXISTING_FAILURE),
        (0, [0], GateClassification.PASS),
    ],
)
def test_baseline_classification_matrix(
    store: RunStateStore,
    tmp_path: Path,
    base_exit: int,
    candidate_exits: list[int],
    expected: GateClassification,
) -> None:
    """base/candidateの全安定組合せを正しく分類する。"""
    classification, _runner = verify(
        store,
        tmp_path,
        [sandbox_result(base_exit)],
        [sandbox_result(code) for code in candidate_exits],
    )

    assert classification is expected


def test_baseline_result_is_cached_once_per_commit_and_gate(
    store: RunStateStore,
    tmp_path: Path,
) -> None:
    """同一commit+gate設定のbaselineは1回だけ実行する。"""
    base = tmp_path / "base"
    candidate = tmp_path / "candidate"
    base.mkdir()
    candidate.mkdir()
    runner = SequenceRunner(
        {
            base: [sandbox_result(0)],
            candidate: [sandbox_result(0), sandbox_result(0)],
        }
    )
    verifier = BaselineVerifier(store, runner)
    gate = GateSpec("pytest", ("pytest", "-q"), timeout_seconds=10)

    verifier.verify(gate, "a" * 40, base, candidate)
    verifier.verify(gate, "a" * 40, base, candidate)

    assert runner.calls[base] == 1
    assert runner.calls[candidate] == 2


def test_baseline_gate_runs_inside_real_sandbox(store: RunStateStore, tmp_path: Path) -> None:
    """base/candidateの同一commandを実機sandbox profileで実行する。"""
    base = tmp_path / "base"
    candidate = tmp_path / "candidate"
    base.mkdir()
    candidate.mkdir()
    (base / "status.txt").write_text("pass", encoding="utf-8")
    (candidate / "status.txt").write_text("pass", encoding="utf-8")
    gate = GateSpec(
        "python-smoke",
        (
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "raise SystemExit(0 if Path('status.txt').read_text() == 'pass' else 1)"
            ),
        ),
        timeout_seconds=10,
    )

    decision = BaselineVerifier(store, SandboxRunner()).verify(
        gate,
        "a" * 40,
        base,
        candidate,
    )

    assert decision.classification is GateClassification.PASS
    assert (store.run_dir / "baseline" / ("a" * 40) / "gate-python-smoke.json").is_file()
