"""sandbox-exec + env sanitize + boundary scan + rlimit runner。"""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import stat as stat_module
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
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
    # stdout/stderrそれぞれの上限。子のメモリ上限が効いていても、大量出力で
    # 制限対象外の親を圧迫できるため、親側で読み取り量を打ち切る。
    output_bytes: int = 8 * 1024**2
    # 実行中にworktreeの増分を検査する間隔。RLIMIT_FSIZEはファイル単位の上限で
    # 多数ファイルによる枯渇を防げないため、定期検知で補う（quotaほど厳密ではない）。
    disk_check_seconds: float = 5.0


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
    """worktree配下の通常ファイル合計を返す。走査中に消えたファイルは0として扱う。"""
    total = 0
    for path in root.rglob("*"):
        try:
            info = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            # 実行中の走査では、子が作って消した一時ファイルと競合する。消失は増分ではない。
            continue
        if stat_module.S_ISREG(info.st_mode):
            total += info.st_size
    return total


def _read_unapplied_limits(report_path: Path) -> tuple[tuple[str, ...], bool]:
    """childが残したlimit適用報告を読み、(未適用limit名, 回収できたか)を返す。

    報告が読めない場合に空tupleだけを返すと「未適用ゼロ＝全て適用できた」と区別が付かない。
    確認できたかどうかを 第2要素で分けて返し、呼び出し側が取り違えられないようにする。
    """
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        # ランチャーが報告を公開する前に終了した、報告が壊れている等。
        return ((), False)
    unapplied = payload.get("unapplied") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or payload.get("complete") is not True
        or not isinstance(unapplied, list)
        or any(not isinstance(item, str) or not item for item in unapplied)
    ):
        return ((), False)
    return tuple(unapplied), True


_LAUNCHER = Path(__file__).with_name("limit_launcher.py")


def _pump_output(
    process: subprocess.Popen[bytes],
    *,
    deadline: float,
    output_cap: int,
    watch: _DiskWatch,
) -> tuple[bytes, bytes, bool, str | None]:
    """子の出力を上限つきで読み、timeout・出力超過・ディスク超過で停止する。

    communicate()は出力を無制限に蓄積するため、上限を超えた時点で
    process groupごと停止して理由を返す。子がstdout/stderrを閉じても
    終了するまで期限とディスク監視を続ける。戻り値は(stdout, stderr, timed_out, violation)。
    """
    assert process.stdout is not None and process.stderr is not None
    buffers = {process.stdout: bytearray(), process.stderr: bytearray()}
    selector = selectors.DefaultSelector()
    for stream in buffers:
        selector.register(stream, selectors.EVENT_READ)
    timed_out = False
    violation: str | None = None
    try:
        while True:
            now = time.monotonic()
            if now >= deadline:
                timed_out = True
                break
            if watch.violation is not None:
                violation = watch.violation
                break
            slice_seconds = min(deadline - now, _POLL_SECONDS)
            if selector.get_map():
                for key, _events in selector.select(timeout=slice_seconds):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    buffer = buffers[key.fileobj]  # type: ignore[index]
                    buffer.extend(chunk)
                    if len(buffer) > output_cap:
                        violation = "sandbox output limit exceeded"
                        break
                if violation:
                    break
            else:
                # 出力は閉じたが子はまだ生きている。終了を短い期限で待ちながら監視を続ける。
                try:
                    process.wait(timeout=slice_seconds)
                except subprocess.TimeoutExpired:
                    continue
                break
            if process.poll() is not None and not selector.get_map():
                break
    except BaseException:
        # 監視側の例外で子を生かしたまま抜けない。
        _kill_group(process)
        process.wait()
        raise
    finally:
        selector.close()
    if timed_out or violation:
        _kill_group(process)
    process.wait()
    return bytes(buffers[process.stdout]), bytes(buffers[process.stderr]), timed_out, violation


_POLL_SECONDS = 0.25


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        if process.poll() is None:
            raise


class _DiskWatch:
    """worktreeの増分を別スレッドで定期走査し、超過や走査失敗を違反として掲げる。

    走査を監視ループの中で同期実行すると、大きなworktreeでは走査中に出力も期限も
    見られなくなる。スレッドへ逃がし、ループ側はフラグを読むだけにする。
    """

    def __init__(self, root: Path, before: int, limit: int, interval: float) -> None:
        self.root = root
        self.before = before
        self.limit = limit
        self.interval = interval
        self.violation: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="orc-disk-watch", daemon=True)

    def __enter__(self) -> _DiskWatch:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                exceeded = _disk_usage(self.root) - self.before > self.limit
            except OSError as error:
                # 消失以外のI/O障害は「増分を確認できない」なので、安全側に倒して停止させる。
                self.violation = f"sandbox disk monitor failed: {error.__class__.__name__}"
                return
            if exceeded:
                self.violation = "sandbox disk growth limit exceeded"
                return


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
            # limit適用報告はworktreeの外へ置く。profileはworktree配下への書込みを
            # untrusted processへ許可しているため、worktree内に置くと「封じ込めが
            # 効いているかの報告」を被検査プロセス自身が改ざんできてしまう。
            with tempfile.TemporaryDirectory(prefix=".orc-limits-") as limits_dir:
                limits_report = Path(limits_dir) / "rlimit-unapplied"
                # 上限の適用はpreexec_fnではなく、exec後に動く信頼済みランチャーが行う。
                # ランチャーはsandbox-execの前段で走るのでworktree外の報告を書けるが、
                # 対象コマンドはsandbox-execを経てから起動するため報告に触れない。
                argv = [
                    sys.executable,
                    "-I",
                    "-S",
                    str(_LAUNCHER),
                    str(limits_report),
                    json.dumps(asdict(self.limits)),
                    "--",
                    self.sandbox_exec,
                    "-p",
                    profile,
                    *command,
                ]
                process = subprocess.Popen(
                    argv,
                    cwd=root,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                    start_new_session=True,
                )
                with _DiskWatch(
                    root, before, self.limits.disk_growth_bytes, self.limits.disk_check_seconds
                ) as watch:
                    raw_stdout, raw_stderr, timed_out, violation = _pump_output(
                        process,
                        deadline=time.monotonic() + timeout_seconds,
                        output_cap=self.limits.output_bytes,
                        watch=watch,
                    )
                exit_code = -9 if timed_out else process.returncode
                unsupported, limits_verified = _read_unapplied_limits(limits_report)
            if violation is None and _disk_usage(root) - before > self.limits.disk_growth_bytes:
                violation = "sandbox disk growth limit exceeded"
        stdout = raw_stdout.decode("utf-8", errors="replace")
        stderr = raw_stderr.decode("utf-8", errors="replace")
        denial = "deny(" in stderr or "operation not permitted" in stderr.lower()
        result = SandboxResult(
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
        if violation is not None:
            # 停止までの部分結果を例外に載せ、上位が監査記録へ残せるようにする。
            raise SandboxViolation(violation, result=result)
        return result
