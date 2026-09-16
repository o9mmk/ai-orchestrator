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
| lease | 同一 repo への多重実行 | `lease.py` |
| budget（soft / hard の二段） | 子プロセス起動の無限ループ | `budget.py` |
| path lock / scope 分離 | 書き換え範囲の逸脱 | `path_locks.py`, `scope.py` |
| DLP gate | 秘密情報が子プロセスの出力に載る | `dlp_matcher.py`, `stream_redaction.py` |
| gitleaks / GlassWorm gate | 資格情報・不可視 Unicode の混入 | `gitleaks_adapter.py`, `glassworm_gate.py` |
| JSON schema 検証 | 子プロセスの出力を信用して壊れる | `schemas.py`, `plan_schema.py`, `result_schema.py` |
| process ledger + cancel | 停止したはずの子プロセスの残留 | `process_ledger.py`, `cancel.py` |
| quarantine | 壊れた状態のまま再開する | `quarantine.py` |
| resume / checkpoint | 中断した run の再実行による二重適用 | `resume.py`, `checkpoint.py` |

budget は soft cap で警告、hard cap で実際に起動を遮断する二段構成にした。
見積りが hard cap を超える規模は L へ昇格し、`approve` を挟まないと進まない。

resource 上限はプラットフォームによって適用できないものがある（macOS の `RLIMIT_AS` など）。
適用できなかった上限は握り潰さず `SandboxResult.unsupported_limits` に載せて返す。
「上限を課したつもり」で untrusted process を走らせないための扱いである。

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
uv run pytest        # テスト 37 ファイル
uv run ruff check .
uv run mypy          # strict
```

## ライセンス

MIT
