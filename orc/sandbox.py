"""sandbox-exec + env sanitize + boundary scan + rlimit runner。"""

from __future__ import annotations

import hashlib
import os
import resource
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from orc.errors import SandboxUnavailable, SandboxViolation


@dataclass(frozen=True)
class SandboxLimits:
    """Verifier processへ適用するresource上限。"""

    cpu_seconds: int = 900
    # macOS maps the shared cache into ~34 GiB VSZ before user code runs.
    memory_bytes: int = 64 * 1024**3
    processes: int = 64
    file_bytes: int = 512 * 1024**2
    open_files: int = 256
    disk_growth_bytes: int = 1024**3


# platformが構造的に強制できないresource上限。運用者の選択ではなくOSの制約なので、
# 実行ごとの判断に委ねず、ここで一度だけ名前を付けて宣言する。
# macOSはRLIMIT_ASを実質サポートしておらず、setrlimitがValueErrorになる。
PLATFORM_UNENFORCEABLE_LIMITS: frozenset[str] = (
    frozenset({"RLIMIT_AS"}) if sys.platform == "darwin" else frozenset()
)


@dataclass(frozen=True)
class SandboxResult:
    """sandbox commandの非永続raw結果。"""

    command: tuple[str, ...]
    exit_code: int
    timed_out: bool
    stdout: str
    stderr: str
    profile_id: str
    denial_detected: bool
    # platformが適用を拒否したresource上限。空でない場合、その上限は効いていない。
    unsupported_limits: tuple[str, ...] = ()
    # childのlimit適用報告を回収できたか。Falseは「上限が効いている保証がない」を意味する。
    # 既定をFalseにしてあるのは、報告を確認していない経路を「適用済み」と誤読させないため。
    limits_verified: bool = False

    @property
    def log_digest(self) -> str:
        """stdout/stderrを露出せず照合するdigestを返す。"""
        return hashlib.sha256((self.stdout + "\0" + self.stderr).encode()).hexdigest()


def scan_worktree_boundary(worktree: Path) -> None:
    """外向きsymlinkとfile hardlinkを実行前に拒否する。"""
    root = worktree.resolve(strict=True)
    for path in root.rglob("*"):
        if path.is_symlink():
            target = path.resolve(strict=False)
            if not target.is_relative_to(root):
                raise SandboxViolation(f"external symlink: {path.relative_to(root)}")
            continue
        if path.is_file() and path.stat(follow_symlinks=False).st_nlink > 1:
            raise SandboxViolation(f"hardlink detected: {path.relative_to(root)}")


def _scheme_string(path: Path) -> str:
    value = str(path)
    if any(character in value for character in ("\n", "\r", "\0")):
        raise SandboxViolation("sandbox path contains control characters")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_profile(worktree: Path) -> str:
    """networkとworktree外書込をdenyする最小Seatbelt profileを返す。"""
    root = worktree.resolve(strict=True)
    home = Path.home().resolve()
    secret_denies = [home / ".ssh", home / ".aws", home / ".config" / "gcloud"]
    secret_denies.extend(path.resolve(strict=False) for path in root.rglob("*") if _secret_path(path))
    deny_rules = " ".join(f'(deny file-read* (subpath "{_scheme_string(path)}"))' for path in secret_denies)
    return " ".join(
        (
            "(version 1)",
            "(deny default)",
            "(allow process*)",
            "(allow file-read*)",
            "(allow sysctl-read)",
            "(allow signal (target self))",
            f'(allow file-write* (subpath "{_scheme_string(root)}"))',
            "(deny network*)",
            deny_rules,
        )
    )


def _secret_path(path: Path) -> bool:
    name = path.name.lower()
    return (
        name.startswith(".env")
        or name.endswith(".pem")
        or name in {"credentials", ".ssh"}
        or "secret" in name
        or "token" in name
    )


def _disk_usage(root: Path) -> int:
    return sum(
        path.stat(follow_symlinks=False).st_size
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    )


def _limit_process(limits: SandboxLimits, report_path: Path) -> None:
    """child側でresource上限を適用し、platformが拒否した上限名をreport_pathへ残す。

    macOSはRLIMIT_ASを実質サポートしておらず、getrlimitがRLIM_INFINITYを返しても
    setrlimitはValueErrorになる。ここで例外をそのまま出すとpreexec_fnの失敗として
    sandbox起動自体が不能になり、逆に握り潰すと「上限を課したつもり」のまま
    untrusted processを走らせることになる。どちらも取らず、適用できなかった上限は
    必ず親へ伝えて呼び出し側の判断材料にする。
    """
    unapplied: list[str] = []
    for limit_label, limit_name, requested in (
        ("RLIMIT_CPU", resource.RLIMIT_CPU, limits.cpu_seconds),
        ("RLIMIT_AS", resource.RLIMIT_AS, limits.memory_bytes),
        ("RLIMIT_NPROC", resource.RLIMIT_NPROC, limits.processes),
        ("RLIMIT_FSIZE", resource.RLIMIT_FSIZE, limits.file_bytes),
        ("RLIMIT_NOFILE", resource.RLIMIT_NOFILE, limits.open_files),
    ):
        _soft, hard = resource.getrlimit(limit_name)
        effective = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
        try:
            resource.setrlimit(limit_name, (effective, effective))
        except (ValueError, OSError):
            unapplied.append(limit_label)
    report_path.write_text("\n".join(unapplied), encoding="utf-8")


def _read_unapplied_limits(report_path: Path) -> tuple[tuple[str, ...], bool]:
    """childが残したlimit適用報告を読み、(未適用limit名, 回収できたか)を返す。

    報告が読めない場合に空tupleだけを返すと「未適用ゼロ＝全て適用できた」と区別が付かない。
    確認できたかどうかを 第2要素で分けて返し、呼び出し側が取り違えられないようにする。
    """
    try:
        raw = report_path.read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        # forkはしたがpreexec_fnが最後まで走らなかった、報告が壊れている等。
        return ((), False)
    return tuple(line for line in raw.splitlines() if line), True


class SandboxRunner:
    """untrusted test/buildをsandbox-exec内で実行する。"""

    def __init__(
        self,
        *,
        sandbox_exec: str = "/usr/bin/sandbox-exec",
        limits: SandboxLimits | None = None,
    ) -> None:
        if not Path(sandbox_exec).is_file() or not os.access(sandbox_exec, os.X_OK):
            raise SandboxUnavailable("sandbox-exec is unavailable")
        self.sandbox_exec = sandbox_exec
        self.limits = limits or SandboxLimits()
        self.profile_id = "sandbox-exec:v1-deny-network-outside-write"

    def run(
        self,
        command: list[str] | tuple[str, ...],
        worktree: Path,
        *,
        timeout_seconds: int,
    ) -> SandboxResult:
        """allowlist envとresource capを付け、timeoutを明示結果へ変換する。"""
        if not command:
            raise ValueError("sandbox command must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("sandbox timeout must be positive")
        if any(not isinstance(argument, str) or not argument or "\0" in argument for argument in command):
            raise ValueError("sandbox command arguments must be non-empty strings without NUL")
        if worktree.is_symlink():
            raise SandboxViolation("worktree root must not be a symlink")
        root = worktree.resolve(strict=True)
        if not root.is_dir():
            raise SandboxViolation("worktree root must be a directory")
        scan_worktree_boundary(root)
        before = _disk_usage(root)
        profile = build_profile(root)
        with tempfile.TemporaryDirectory(prefix=".orc-sandbox-", dir=root) as temporary:
            temp_root = Path(temporary)
            env = {
                "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                **{key: os.environ[key] for key in ("LANG", "LC_ALL", "TZ") if key in os.environ},
            }
            env.update(
                {
                    "HOME": str(temp_root / "home"),
                    "TMPDIR": str(temp_root / "tmp"),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONNOUSERSITE": "1",
                }
            )
            (temp_root / "home").mkdir()
            (temp_root / "tmp").mkdir()
            argv = [self.sandbox_exec, "-p", profile, *command]
            # limit適用報告はworktreeの外へ置く。profileはworktree配下への書込みを
            # untrusted processへ許可しているため、worktree内に置くと「封じ込めが
            # 効いているかの報告」を被検査プロセス自身が改ざんできてしまう。
            with tempfile.TemporaryDirectory(prefix=".orc-limits-") as limits_dir:
                limits_report = Path(limits_dir) / "rlimit-unapplied"
                process = subprocess.Popen(
                    argv,
                    cwd=root,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                    preexec_fn=lambda: _limit_process(self.limits, limits_report),
                )
                try:
                    stdout, stderr = process.communicate(timeout=timeout_seconds)
                    exit_code, timed_out = process.returncode, False
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        if process.poll() is None:
                            raise
                    stdout, stderr = process.communicate()
                    exit_code, timed_out = -9, True
                unsupported, limits_verified = _read_unapplied_limits(limits_report)
            if _disk_usage(root) - before > self.limits.disk_growth_bytes:
                raise SandboxViolation("sandbox disk growth limit exceeded")
        denial = "deny(" in stderr or "operation not permitted" in stderr.lower()
        return SandboxResult(
            tuple(command),
            exit_code,
            timed_out,
            stdout,
            stderr,
            self.profile_id,
            denial,
            unsupported,
            limits_verified,
        )
