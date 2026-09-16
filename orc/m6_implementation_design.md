# orc M6 実装設計

## 変更概要

`FINAL_DESIGN.md` §16のM6だけを実装する。子stdout/stderrはPIPEから並行して読み、
disk書込前に固定形式でredactする。patch/result/transcript/findings/summary等は、
path境界検証、local high-confidence matcher、gitleaks scanの順で評価し、CLEANだけを
通常artifactへatomic publishする。検出・scan不能・出力上限到達は成功へ縮退させず、
Manager管理下のquarantineと安全なmetadataへ閉じ込める。

## 影響範囲

- 直接変更: `structured_process.py`、`codex_adapter.py`、`attempt_staging.py`、
  `planner_staging.py`、`child_runner.py`、`planner_attempt.py`、
  `manager_context_models.py`、`result_artifacts.py`
- 新規責務: DLP models/matcher、stream capture、gitleaks adapter、artifact ingest、
  quarantine、M6 acceptance helpers/tests
- 間接影響: M4のtimeout・PGID台帳、M5のPlanner・budget・Manager context、
  checkpoint artifact digest
- 外部影響: なし。実LLM・network・親worktree・`~/.orchestrator/`実pathは使わない

## 責務境界

1. `dlp_matcher.py`: version付きの高確度secret/PII patternと固定replacement。
2. `stream_redaction.py`: incremental UTF-8 decode、overlap、bounded output、
   stdout/stderr同時drain。raw byte列は返さない。
3. `gitleaks_adapter.py`: 実CLI capability/version probe、argv起動、timeout、
   bounded stdout/stderr、raw reportの一時0600保存と安全なcountへの縮約。
4. `artifact_ingest.py`: sourceのpath/type/size/link検証、matcher+scanner、
   CLEAN publishまたはquarantine。公開・event・checkpointを同一fencing境界で確定する。
5. `quarantine.py`: opaque idだけをpathへ使い、payload/manifestを0600でatomic保存。
6. `structured_process.py`: 既存のspawn/PGID/timeout/ledgerを維持しつつ、
   stdout/stderrのPIPE captureへ接続し、leader正常終了後も残存PGIDを回収する。
7. `child_runner.py` / `planner_attempt.py`: stream/resultをingestし、1件でも非CLEANなら
   validated result・claimed_statusをManagerへ渡さない。DLPはbudget invocationに加算しない。
8. `staging_files.py`: child書込可能pathをdirectory fd/inodeへ固定し、Managerの
   read/write/cleanupがsymlink差替え先へ到達しないようにする。
9. `manager_context_models.py`: store登録済みopaque DLP clearanceのない
   result/verify等をvariable化できない。Builderとplan/result/verify保存境界でも再照合し、
   parse/finalize後のcanonical JSONを再scanする。

## 公開モデルとinvariant

- statusは `CLEAN / REDACTED / QUARANTINED / SCAN_FAILED` のみ。
- Manager-facing値はartifact kind/id、status、category count、scanner name/version/code、
  quarantine有無、固定reason、CLEAN全体digestだけ。
- secret/PII値、match周辺、line、raw scanner report、raw stdout/stderr、
  secret由来path、match単体digestは公開しない。
- scanner failure、quarantine failure、output limitはCLEANにならない。
- normal publish前に全scanが完了し、失敗時は通常artifactを残さない。
- quarantineはrun固有0600 keyによるHMACだけをcheckpointへ載せ、match値単体の
  unkeyed digestを公開せずに改変を検出する。

## エッジケース

- secretがread chunk境界をまたぐ、UTF-8文字がbyte境界をまたぐ
- footer欠落・上限超過のprivate key block、matcher安全値未満のstream overlap
- stdout/stderr双方がpipe容量を超えて同時出力する
- timeout後のTERM/KILL中にもpipeを最後まで安全にflushする
- output上限後もpipeをdrainしてdeadlockせず、上限到達を構造化する
- absolute/`..`/escape/symlink/hardlink/FIFO/directory/oversize source
- gitleaks起動不能、timeout、unsupported version/capability、unknown exit、
  malformed/oversize report・stdout/stderr
- quarantine payload成功後のmanifest失敗、通常publish途中失敗
- staging中間directory差替え、正常終了leaderが残すdescendant、fencing中のcheckpoint失敗
- checkpoint失敗時のartifact rollbackとappend-only補償event、capture失敗時のledger確定
- JSON Unicode escapeでraw scanを通過したresult/planのcanonical再scan
- detected stdout/resultがあるのにchildの`claimed_status=done`が返る

## データフロー

`child process` → `PIPE同時drain` → `streaming matcher/redaction` →
`0600 staging` → `path safety + local scan + gitleaks` →
`CLEAN: atomic normal publish` / `otherwise: 0600 quarantine` →
`safe DLP event + checkpoint` → `CLEAN clearanceだけManager-facing model`

## TDDと検証

- Red: matcher/stream/scanner/path/quarantine/safe metadata/AT-15/AT-21テストを先行追加
- Green: M6対象pytest、次に全pytest
- Refactor: `ruff check .`、変更ソースmypy、`git diff --check`
- 実機: gitleaks 8.30.1の`dir [path]`とreport flagsを使い、repo外のclean/dummy fixtureで確認
- E2E: fake childの入力からredaction→scan→quarantine/publish→event/checkpoint、
  PGID timeout、budget非加算、親tree不変、Manager-facing grep 0まで確認
