# ai-orchestrator (`orc`)

AIエージェントにコードを書かせるための、短命なローカル orchestrator。
専用の git worktree・固定 budget・JSON schema 検証・DLP・決定的な gate で囲い、
Codex / Claude を子プロセスとして起動して plan → 実装 → review → verify を編成する。

設計の出発点は「何をさせるか」ではなく「何をさせないか」だった。
エージェントに書かせること自体は難しくない。難しいのは、暴走したときの被害を
設計時に上限として決めておくことである。

## やらないこと（設計上の境界）

意図的に実装していない。後から足せる機能ではなく、安全性の前提そのものだから。

- daemon 化しない — run は短命で、終わったら状態を残して死ぬ
- `main` へ自動 merge しない — 成果物は worktree に置いたままにする
- push / deploy しない — 外部に出す操作は人間の手に残す
- 作業対象 repo の外に書き込まない — 状態ディレクトリも repo の外側に分離する

## 安全境界のかけ方

| 仕組み | 防いでいること | 実装 |
| --- | --- | --- |
| worktree 隔離 | 作業中の repo を壊す | `worktree.py` |
| sandbox-exec + rlimit | untrusted なテスト/ビルドが network・worktree 外へ出る／対応する資源上限の適用と超過検知 | `sandbox.py`, `limit_launcher.py` |
| 出力上限 | 大量出力で制限対象外の親プロセスを圧迫する | `sandbox.py` の `_pump_output` |
| 実行中の違反の記録 | 出力超過・ディスク超過で停止した事実が監査記録から消える | `baseline.py`, `verify.json` の `gates[].violation` |
| lease | 同一 repo への多重実行 | `lease.py` |
| budget（soft / hard の二段） | 子プロセス起動の無限ループ | `budget.py` |
| path lock / scope 分離 | 書き換え範囲の逸脱 | `path_locks.py`, `scope.py` |
| DLP gate | 秘密情報が子プロセスの出力に載る | `dlp_matcher.py`, `stream_redaction.py` |
| gitleaks / GlassWorm gate | 資格情報・不可視 Unicode の混入 | `gitleaks_adapter.py`, `glassworm_gate.py` |
| JSON schema 検証 | 子プロセスの出力を信用して壊れる | `schemas.py`, `plan_schema.py`, `result_schema.py` |
| process ledger + cancel | 停止したはずの子プロセスの残留 | `process_ledger.py`, `cancel.py` |
| quarantine | 壊れた状態のまま再開する | `quarantine.py` |
| resume / checkpoint | 中断した run の再実行による二重適用 | `resume.py`, `checkpoint.py` |
| 上限の不成立検知 | 資源上限が効かないまま PASS すること | `baseline.py` の `limits_enforced` |

budget は soft cap で警告、hard cap で実際に起動を遮断する二段構成にした。
見積りが hard cap を超える規模は L へ昇格し、`approve` を挟まないと進まない。

resource 上限はプラットフォームによって構造的に適用できないものがある。macOS の `RLIMIT_AS`
がそれで、`setrlimit` が失敗するためメモリ上限は強制されない。この事実は
`PLATFORM_UNENFORCEABLE_LIMITS` として明示的に宣言し、それ以外の上限が未適用だった場合と、
適用状況そのものを回収できなかった場合は、gate が `INCONCLUSIVE` を返して人間の判断へ送る。
適用状況は `verify.json` の `gates[].limits` に必ず記録するので、監査記録から
「上限なしで得た結果」を後から判別できる。

この報告は worktree の外に置く。sandbox profile は worktree 配下への書き込みを
untrusted process へ許可しているため、報告を worktree 内に置くと、封じ込めが効いているかの
記録を被検査プロセス自身が書き換えられてしまう。

上限の適用そのものは `preexec_fn` ではなく、exec 後に独立プロセスとして動く
`limit_launcher.py` が行う。fork 後・exec 前に Python コードを走らせる方式は、
スレッドを持つ親からはデッドロックし得るためである。ランチャーは `python -I -S` で起動し
（cwd・`PYTHONPATH`・site-packages の影響を受けない）、`sandbox-exec` の前段で上限を適用し、
報告を一時ファイルに書いて `fsync` してから rename で公開し、対象コマンドへ exec する。
途中で壊れた報告は `complete` フラグを持たないので「未適用なし」とは読まれず、
ランチャー自体が失敗した実行は「確認不能」として `INCONCLUSIVE` に落ちる。

保証の範囲も書いておく。stdout/stderr は親側で上限（既定 8 MiB ずつ）を設け、超過した時点で
process group ごと停止する。ディスクは `RLIMIT_FSIZE` がファイル単位の上限で多数ファイルによる
枯渇を防げないため、別スレッドが一定間隔（既定 5 秒）で worktree の増分を走査し、超過で停止する。
子が stdout/stderr を閉じた後も、終了するまで期限とディスク監視は続く。これは quota のような
即時の強制ではなく、走査時間と間隔分の遅れを持つ検知である。総メモリ・総ディスクの厳密な
被害上限が必要なら、資源制限を強制できる VM 等の実行環境が要る。実行中の違反で停止した場合も
停止までの部分結果を保持し、`verify.json` の `gates[].violation` に理由を記録して
判定は `INCONCLUSIVE` にする。

## 使い方

`ORC_STATE_DIR` は対象 repo の外側に置く。`--codex` は shell command ではなく実行ファイルの path を渡す。

```bash
export ORC_STATE_DIR=/absolute/outside-repo/orc-state

uv run --isolated --locked orc run \
  --repo /path/to/repo \
  --goal "限定した目的" \
  --acceptance "受入条件" \
  --forbidden .env \
  --forbidden .git/config \
  --cap child_invocations_soft=8 \
  --cap child_invocations_hard=12 \
  --model-window-tokens 200000 \
  --codex /absolute/path/to/codex
```

固定 plan を使う場合は `--plan-file plan.json` を指定する。この経路では Planner を呼ばないので
`--model-window-tokens` は不要。

plan の `path_scope` には書き換えるファイルだけを列挙し、読むだけのファイルは `read_scope` へ置く。
`read_scope` は patch 許可 scope にも規模判定にも入らない（秘密 deny pattern と repo 外拒否は同じく適用される）。

```bash
uv run --isolated --locked orc status  --repo /path/to/repo <run-id>
uv run --isolated --locked orc approve --repo /path/to/repo <run-id>
uv run --isolated --locked orc cancel  --repo /path/to/repo <run-id>
```

別 terminal からの live cancel は `CANCEL_PENDING` を返す。owner が intent を検出し、
ledger 済みの process group を TERM → grace → KILL で停止してから `CANCELLED` へ遷移する。

## 設計の記録

要件と設計判断を残してある。とくに `docs/design-review/` は、同じ要件から2つの設計案を
独立に起こさせ、judge に比較評価させたうえで最終設計を決めた記録である。

- [docs/requirements.md](docs/requirements.md) — 要件と制約
- [docs/design.md](docs/design.md) — 最終設計
- [docs/acceptance-matrix.md](docs/acceptance-matrix.md) — 受入条件と検証の対応表
- [docs/walkthrough.md](docs/walkthrough.md) — 実行の流れ
- [docs/design-review/](docs/design-review/) — 2案の独立設計と judge による比較評価

## 開発

```bash
uv run pytest        # 321 tests
uv run ruff check .
uv run mypy          # strict
```

## 作者

岡田 賢揮 — [ポートフォリオ](https://o9mmk.github.io/) ／ [GitHub](https://github.com/o9mmk)

## ライセンス

MIT
