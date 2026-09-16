"""orcがfail-loudに通知する例外。"""


class OrchestratorError(RuntimeError):
    """orcの基底例外。"""


class InvalidTransition(OrchestratorError):
    """状態機械に存在しない遷移。"""


class LeaseHeld(OrchestratorError):
    """生存中または期限内のleaseが存在する。"""


class LeaseLost(OrchestratorError):
    """fencing token不一致により旧Managerが停止した。"""


class DuplicateRunId(OrchestratorError):
    """既存run-idの監査領域を上書きせず開始を拒否した。"""


class PathLockConflict(OrchestratorError):
    """要求scopeが既存path lockと重複する。"""


class PreflightError(OrchestratorError):
    """安全境界を確定できないPreflight失敗。"""


class WorktreeError(OrchestratorError):
    """専有worktreeの作成・検証失敗。"""


class SandboxUnavailable(OrchestratorError):
    """Verifier sandboxを機械強制できない。"""


class SandboxViolation(OrchestratorError):
    """worktree境界・resource上限違反。

    実行中に検知した違反（出力超過・ディスク増分超過）は、停止までに得た部分結果を
    ``result`` に伴う。監査記録から違反の事実と出力digestが消えないようにするため。
    起動前に検知した違反（symlink等）は結果を持たない。
    """

    def __init__(self, message: str, *, result: object | None = None) -> None:
        super().__init__(message)
        self.result = result


class VerificationError(OrchestratorError):
    """Verifierの必須gate・scope相互照合失敗。"""


class CodexCapabilityError(OrchestratorError):
    """実機Codex CLIの必須flagを確認できず子起動を拒否した。"""


class ClaudeCapabilityError(OrchestratorError):
    """実機Claude CLIの必須flagまたは最小probeを確認できない。"""


class ReviewExecutionError(OrchestratorError):
    """Reviewer出力を安全に取得・検証できない。"""


class ResumeBlocked(OrchestratorError):
    """旧runを変更せずnew generation作成を安全上拒否した。"""


class IntegrationError(OrchestratorError):
    """隔離integration branchを安全に構築できない。"""


class ChildExecutionError(OrchestratorError):
    """子processの安全な起動・監視・cleanupに失敗した。"""


class ProcessLedgerError(OrchestratorError):
    """worktrees.json台帳の破損またはPID/PGID不一致。"""


class TamperDetected(OrchestratorError):
    """hash chainまたはartifact digestの不一致。"""


class CheckpointCorrupt(OrchestratorError):
    """checkpointがJSON/schemaとして復元不能。"""


class DlpBoundaryError(OrchestratorError):
    """DLP sourceのpath・type・size境界を安全に確定できない。"""
