# orc MVP walkthrough

## 完成状態

M1〜M9を接続し、`orc run / status / approve / cancel / resume / gc`をローカルpackageの
console scriptとして利用できる。Managerは単一の短命Python processで、daemon・DB・
外部queueを持たない。

正常系fixtureでは、専用worktree内の変更からManagerが実patchを生成し、DLP、
baseline/candidate sandbox gate、scope照合、reviewer fallbackを通した後、
`orc/<run_id>`だけを作って`AWAITING_APPROVAL`で停止した。ユーザーtreeは開始前後で
バイト一致し、merge/pushは実行していない。

## 利用例

```bash
uv run orc run \
  --repo /path/to/repo \
  --goal "目的" \
  --acceptance "受入条件" \
  --model-window-tokens 200000

uv run orc status --repo /path/to/repo <run-id>
uv run orc approve --repo /path/to/repo <run-id>
```

schema済みの固定planを使う場合は`--plan-file plan.json`を指定し、Planner呼び出しを
省略できる。Claude reviewは既定無効で、`--allow-claude`の明示時だけ候補になる。
`--forbidden`と`--cap KEY=JSON_INTEGER`は繰り返し指定する。Planner利用時は
`child_invocations_hard >= 2`が必要で、`ORC_STATE_DIR`は対象repo外に置く。

`gc`は監査artifactとworktreeを不可逆に削除するため、終端runと完全一致する
`--confirm-run-id`が必須である。未承認branchがある場合はさらに`--force-unmerged`が
必要で、integration branch自体は削除しない。

## 検証結果

- `uv run --isolated --locked pytest ai-orchestrator/tests/ -x --tb=short`: 295 passed
- `uv run --isolated --locked ruff check ai-orchestrator/orc ai-orchestrator/tests`: passed
- `uv run --isolated --locked mypy ai-orchestrator/orc`: strict mode passed
- `uv lock --check`: passed
- CLI package smoke: `uv run orc --help` passed
- 実Codex `0.144.1`: Planner + implementerを最大2 invocationで完走し`AWAITING_APPROVAL`、
  その後に通常cancel/gcで一時runを回収
- AT-1〜21の対応nodeは`ACCEPTANCE_MATRIX.md`に固定
- staged commit前gate: gitleaks / GlassWorm / pre-commitを実行する

## 安全境界

- main自動merge、push、rebase、stash経路なし
- childは専用worktree、Verifierはsandbox、全出力はDLP経由
- completionはverify/review証拠のみで、`claimed_status`を不使用
- cancelはlock領域のcooperative intentをlive ownerがpollし、PGID終了確認後にCANCELLED。
  dead ownerだけを回収し、alive/PermissionError/PID再利用ownerのleaseは強奪しない
- canonical schemaは`uniqueItems`等を保持し、Codex transportだけをallowlist変換。返却値は
  canonical schemaで再検証する
- duplicate run-idはlease前拒否、active leaseは30秒間隔renew、resumeは必ず新generation
- stale HEAD、tamper、policy/gate不一致は自動修復せず拒否
