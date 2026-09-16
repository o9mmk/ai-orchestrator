# M9 implementation design

## Scope

M1-M8の安全部品を、常駐プロセスを持たない`orc` CLIへ配線する。公開操作は
`run / status / approve / cancel / resume / gc`の6個だけとし、fixtureでは実LLMを
使わず同じsubprocess境界へfake Codexを差し込む。

## Runtime flow

1. `run`: Stage 1 → Plannerまたはschema済み`--plan-file` → Stage 2 → 直列DAG実行
2. Implementerは専用worktreeだけを変更し、Managerが実diffを生成・DLP検査する
3. Verifierはbase/candidateで同一gateをsandbox実行し、scopeとchanged filesを照合する
4. Sはverify-onlyを明示、M/Lは独立reviewerを試し、有限counterでDONE/ESCALATEDを決める
5. DONE patchだけを`orc/<run_id>`へ適用し、main tree不変のままAWAITING_APPROVALで止める
6. `approve`は開始承認または最終承認だけを行い、merge/pushは呼ばない

## CLI session and fencing

- mutating commandはrun snapshotを改変前に全検証し、新leaseの単調増加fencing tokenへ
  明示的にrotateする。安定状態へ到達したらleaseを解放する。
- `status`はleaseを取得せずread-only snapshotだけを読む。
- `resume`は旧runを変更せず新run generationを作り、policy/gates/base/digest一致時だけ
  schema済みplanと未完了taskを継続する。

## Destructive boundary

- `gc`は終端run、完全一致する`--confirm-run-id`、明示対象pathを必須にする。
- integration branchが未承認なら追加の`--force-unmerged`が必要。
- repo root、state root全体、glob、未解決環境変数を削除対象にしない。
- branchは削除しない。run/worktreeの削除だけを実行し、監査artifactの消失を表示する。

## Acceptance

- AT-1〜16の既存テストを保持する。
- AT-17〜21をCLI/fixtureを含むM9テストで補完する。
- 全suite、ruff、strict mypy、gitleaks、GlassWorm、CLI package smokeを最終gateにする。
