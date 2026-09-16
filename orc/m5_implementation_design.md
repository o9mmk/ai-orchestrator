# orc M5 実装設計

## 変更概要

設計書§16のM5だけを実装する。Plannerのplan生成、Manager入力の固定コンテキスト層、
budget meterを分離し、M4のCodex capability probe・worktree境界・timeout・PGID台帳を
共通のone-shot実行核として再利用する。

## 影響範囲

- 直接変更: Planner、context builder、usage計測、budget meter/guard、process ledger、
  ChildRunner、state store/checkpoint schema
- 間接影響: Preflight Stage 2、run状態遷移、M1〜M4のintegrity replayと全回帰テスト
- 外部影響: なし。実LLM・network・本番stateは使わず、全テストはfake executableと
  `ORC_STATE_DIR`配下で行う

## 責務境界

1. `structured_process.py`: Codex one-shotのspawn、PGID登録、timeout、TERM/KILL証跡。
   plan/resultの解釈は行わない。
2. `planner.py` / `planner_staging.py`: 最大2回、PLAN_SCHEMA再検証、決定論sizing、
   valid planだけの保存。不正本文はdigestと理由へ縮約する。
3. `manager_context.py`: pinned/variableの型分離、canonical serialize、token上限、
   model window 20%制約、古いvariableの決定論drop。
4. `usage.py`: 構造検証済みusageだけをmeasuredとし、それ以外をbytes/count proxyへ縮退。
5. `budget.py`: manifest capsから単調counterを更新し、spawn前判定、events/checkpoint永続化、
   soft/hard停止を行う。hard停止はProcessLedgerの全RUNNING PGID終了APIを使う。

## エッジケース

- invalid JSON/schema本文、stdout/stderr、JSONL本文をManager-facing値・event・例外へ出さない
- Planner 2回失敗後はFAILED(plan_failed)へ遷移し、3回目をspawnしない
- pinned単独超過は切り捨てずREFUSED(goal_too_large)、budget消費ゼロ
- model window不明時は推測せずspawn前に停止
- proxyを含む集計はmeasuredへ昇格させない
- soft到達後は新規spawnなし、hard到達時は台帳の全RUNNING PGIDを安全照合して終了
- lease/fencing不一致では既存の例外を伝播し、budget/state更新で上書きしない

## 検証方法

- Red: M5 pytestを追加し、未実装import/期待動作で失敗を確認
- Green: M5対象pytest、続いてM1〜M4を含む全pytest
- Refactor: `ruff check .`、新規M5ソースへのmypy、`git diff --check`
- 安全確認: silent-fail、raw本文再投入、merge/push/reset/stash経路、親tree status/bytes
- コミット前: staged範囲、gitleaks、GlassWorm、staged diff check、自己メタレビュー

## エンドツーエンド確認

入力は manifest/pinned goal とfake Planner出力から始まり、one-shot起動、plan再検証、
決定論sizing、context構築、budget更新、events/checkpoint保存までをfixture repoで通す。
soft/hard系はspawn前遮断とPGID残存ゼロまで検証し、`claimed_status`は完了判定に接続しない。
