# orc M8 実装設計

## 変更概要

`FINAL_DESIGN.md` §8、§16のM8だけを実装する。cancelはfenced PGID台帳にある
全RUNNING groupをTERM→猶予→KILLで停止確認してからCANCELLEDへ遷移し、worktreeと
artifactを保持する。resumeは旧runを直接遷移させず、旧leaseとは独立したread-only
snapshot検証後に新しいlease/fencing token/run_id/generationを作る。integrationは
base commitと現在HEADが一致する場合だけ専用worktreeで`orc/<run_id>`候補branchを作り、
mainへのmerge/push/rebaseは実行せず提示だけ行う。

## 影響範囲

- 直接変更: `process_ledger.py`、`store.py`、`schemas.py`、`worktree.py`
- 新規責務: cancel service、read-only run snapshot、resume service、integration service、
  M8 acceptance helpers/tests
- 間接影響: events/checkpoint hash chain、lease/fencing、worktree隔離、task state、M4孤児回収
- 外部影響: ローカルgit ref `orc/<run_id>`の作成だけ。user branch/worktree、main、remote、
  networkには触れない

## 責務境界

1. `process_ledger.py`: budget停止とcancel停止のstatus/eventを分離し、どちらもPID/PGID
   identityを再確認して全RUNNING recordを終端化する。
2. `cancel.py`: 非終端runをCANCELLINGへ遷移し、全子停止証拠とtask ABORTEDをcheckpointへ
   固定した後だけCANCELLEDへ進める。削除はしない。
3. `store.py`: task snapshotを遷移間で保持し、task更新をevent+checkpointへ同一fenceで保存する。
4. `run_snapshot.py`: leaseなしread-onlyでmanifest/events/checkpoint/artifact digestを検証し、
   改変時は旧runを一切修復しない。
5. `resume.py`: source stateがHALTED/CANCELLED/FAILED、HEAD/policy/gates/capsが一致する場合だけ
   新runを作る。DONE taskはdigest参照付きで再利用し、他taskはQUEUEDへ戻す。
6. `worktree.py`: `orc/<run_id>`専用branch worktreeだけを追加でき、既存branch/targetを
   overwriteしない。
7. `integration.py`: integrityとHEADを再確認し、DONE taskの検証済みpatchだけを順に
   `git apply --index`して候補commitを作る。ユーザーtree status/bytes不変を前後比較する。

## invariant

- cancel完了時にRUNNING ledger recordと生存PGIDが0でなければCANCELLEDにしない。
- cancelはworktree/artifact/branchを削除しない。
- HALTED/CANCELLED/FAILEDの旧manifest stateは変更しない。
- tamper、base HEAD、policy、gates不一致では新run directory/leaseを作らない。
- cap引上げは明示flagなしに許可しない。既定は親capsの完全コピー。
- new generationは新run_id、新lease、新fencing token、新budget 0から開始する。
- DONE再利用は旧checkpoint digestで覆われたartifact参照だけ。再実行しない。
- stale HEADではbranch、worktree、commitを作らずAWAITING_APPROVAL(stale_head)へ送る。
- integration branch名は`orc/<run_id>`固定。既存refをforce更新しない。
- `merge`、`push`、`rebase`、user treeへの`apply`を呼ぶコードを持たない。

## エッジケース

- PID消滅済み、PID/PGID不一致、TERM無視、ledger破損、停止途中のfencing喪失
- cancel対象がAWAITING_*、既に終端、task snapshotなし/複数状態
- checkpoint遅延/破損、manifest/gates/patch/events改変、旧run key改変
- stale HEAD、policy/gates mismatch、cap暗黙引上げ、new run_id衝突、lease競合
- DONE taskのartifact欠損/digest mismatch、部分完了task、resumeを同じrun_idで要求
- integration branch既存、patch順衝突、空patch、scope外/symlink patch、commit失敗
- user treeに無関係dirty変更がある状態でintegrationしても前後bytes/statusが不変

## データフロー

- cancel: `run` → CANCELLING → ledger identity確認 → TERM/KILL → task ABORTED →
  checkpoint → CHILDREN_STOPPED → CANCELLED
- resume: `old run dir(read-only)` → chain/digest/schema → current HEAD/policy/gates/caps →
  new lease/fence/run → DONE refs再利用/残task QUEUED → INIT/PREFLIGHT1相当の新generation
- integration: `INTEGRATING store` → integrity/current HEAD → dedicated branch worktree →
  verified DONE patches → candidate commit → branch/commit/merge command提示 → AWAITING_APPROVAL

## TDDと検証

- Red: AT-7/13、tamper no-new-generation、DONE再利用、cap/policy/gate mismatch、
  user tree不変、既存branch拒否を先行追加
- Green: M8対象pytest、次に全pytest
- Refactor: `ruff check`、変更ソースmypy、`git diff --check`
- E2E: local fixture git repo+実process groupでcancel、別run generation、integration branchを通し、
  user tree hash/status不変、remote操作0、旧run bytes不変を確認する
