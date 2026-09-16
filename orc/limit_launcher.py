"""resource上限を適用してから対象コマンドへexecする信頼済みランチャー。

親プロセスの ``preexec_fn`` でrlimitを設定する方式は、fork後・exec前に
Pythonコードを走らせるため、スレッドを持つ親からはデッドロックし得る
（Python公式がpreexec_fnを「スレッドがある場合は安全でない」と明記している）。
この方式では、親は通常どおりexecだけを行い、上限の適用・報告は
exec後に独立したプロセスとして動くこのスクリプトが担う。

このファイルは ``python -I <this file> <report> <limits-json> -- <argv...>`` として
直接実行される。orcパッケージをimportせず標準ライブラリだけで完結させているのは、
sandbox化されたenv（PYTHONPATHなし・cwdはworktree）でも確実に動かすため。
"""

from __future__ import annotations

import json
import os
import resource
import sys

# 適用対象と、SandboxLimitsのfield名との対応。順序は報告の安定性のため固定する。
_LIMITS: tuple[tuple[str, int, str], ...] = (
    ("RLIMIT_CPU", resource.RLIMIT_CPU, "cpu_seconds"),
    ("RLIMIT_AS", resource.RLIMIT_AS, "memory_bytes"),
    ("RLIMIT_NPROC", resource.RLIMIT_NPROC, "processes"),
    ("RLIMIT_FSIZE", resource.RLIMIT_FSIZE, "file_bytes"),
    ("RLIMIT_NOFILE", resource.RLIMIT_NOFILE, "open_files"),
)

EXIT_USAGE = 97
EXIT_REPORT_FAILED = 98
REPORT_VERSION = 1


def apply_limits(requested: dict[str, int]) -> list[str]:
    """上限を順に適用し、platformが拒否したlimit名を返す。"""
    unapplied: list[str] = []
    for label, limit_name, field in _LIMITS:
        value = requested[field]
        _soft, hard = resource.getrlimit(limit_name)
        effective = value if hard == resource.RLIM_INFINITY else min(value, hard)
        try:
            resource.setrlimit(limit_name, (effective, effective))
        except (ValueError, OSError):
            unapplied.append(label)
    return unapplied


def main(argv: list[str]) -> int:
    """引数を検証し、上限を適用・報告してから対象へexecする。戻るのは失敗時だけ。"""
    if len(argv) < 5 or argv[3] != "--":
        sys.stderr.write("usage: limit_launcher <report> <limits-json> -- <argv...>\n")
        return EXIT_USAGE
    report_path, limits_json, target = argv[1], argv[2], argv[4:]
    try:
        requested = {field: int(json.loads(limits_json)[field]) for _, _, field in _LIMITS}
    except (ValueError, KeyError, TypeError) as error:
        sys.stderr.write(f"limit_launcher: invalid limits: {error}\n")
        return EXIT_USAGE
    unapplied = apply_limits(requested)
    # 報告はexecの前に書き切り、完成したものだけを原子的に公開する。
    # 途中で失敗した空ファイルや欠けたファイルを親が「未適用なし」と読まないよう、
    # 一時ファイルへ書いてfsyncしてからrenameし、completeフラグを持つ構造にする。
    payload = json.dumps({"version": REPORT_VERSION, "unapplied": unapplied, "complete": True})
    staging = f"{report_path}.tmp"
    try:
        with open(staging, "w", encoding="utf-8") as report:
            report.write(payload)
            report.flush()
            os.fsync(report.fileno())
        os.replace(staging, report_path)
    except OSError as error:
        sys.stderr.write(f"limit_launcher: cannot write report: {error}\n")
        return EXIT_REPORT_FAILED
    os.execv(target[0], target)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
