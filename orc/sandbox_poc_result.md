# sandbox-exec / CLI 実機確認結果

- 実施日: 2026-07-16 JST
- OS: macOS 26.5.2 (Build 25F84, x86_64)
- Python: 3.12.13
- sandbox-exec: `/usr/bin/sandbox-exec`（version flagなし、`-p profile-string`対応）
- 再実行: `cd ai-orchestrator && python3.12 -m orc.sandbox_poc`

## PoC判定

合格。profile `sandbox-exec:v1-deny-network-outside-write` で次を実機確認した。

| probe | 結果 | 根拠 |
|---|---|---|
| worktree内file write | PASS | child exit 0、内容一致 |
| worktree外file write | BLOCKED | child非0、対象file未作成、sandbox deny観測 |
| loopback TCP接続 | BLOCKED | child非0、sandbox deny観測 |
| AF_UNIX socket作成 | BLOCKED | child非0、socket未作成、sandbox deny観測 |
| env sanitize | PASS | allowlist外のdummy secret envがchildに非継承（pytest実証） |
| symlink/hardlink escape | PASS | 実行前scanで明示例外（pytest実証） |

profileはdefault denyを起点に、process/file-read/sysctl/self signal、worktree内file-writeだけを許可し、`network*`を拒否する。CPU/NPROC/FSIZE/NOFILE/RLIMIT_AS、timeout、前後disk増分も別層で制限する。

## CLI flag再確認（M4以降用、起動は未実施）

| CLI | version | 実機で確認したflag |
|---|---|---|
| Codex | `codex-cli 0.144.1` | `codex exec --output-schema <FILE>` / `--sandbox <MODE>` / `--json` |
| Claude Code | `2.1.210` | `-p, --print` / `--json-schema <schema>` / `--permission-mode` / `--allowedTools` / `--tools` |

help表示だけを確認し、子agent・LLM callは実行していない。M4着手時はversion driftを再確認する。

## 残存リスク

- `sandbox-exec`はdeprecatedで将来のmacOSで消える可能性がある。利用不能・PoC不合格時は機能をfail-loudに無効化し、合成fallbackを再評価する。
- macOSの共有cacheによりPython起動直後のVSZが約34 GiBあるため、RLIMIT_ASは64 GiB。物理memoryの厳密な2 GiB capではない。
- file-readはruntime互換性のため広く許可し、`~/.ssh`・`~/.aws`・gcloud設定とworktree内の既知秘密patternを個別denyしている。任意名の秘密file検知とartifact DLP/quarantineは設計書の後続M6範囲。
- diskはRLIMIT_FSIZEと実行前後の総量差で検知する方式で、filesystem quotaのような実行中の総量hard capではない。
