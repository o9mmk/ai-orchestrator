# orc M7 実装設計

## 変更概要

`FINAL_DESIGN.md` §10、§12、§16のM7だけを実装する。Reviewerへ渡す束を
allowlistで組み立て、30k token相当上限とDLP pre-spend scanを通過した場合だけ
Claudeをtoolsなし・safe mode・schema出力のone-shotで起動する。Claudeがprobe、
実行、schema、DLPのいずれかで失敗した場合はCodexの独立read-only sessionへ縮退し、
それも不成立なら`none`を構造化して人間承認へ送る。完了判定はverifyとreviewだけを
参照し、`claimed_status`は入力にも判定にも使わない。

## 影響範囲

- 直接変更: `structured_process.py`、`store_artifacts.py`、`state_machine.py`、
  `artifact_ingest.py`
- 新規責務: review schema/models、bundle、Claude/Codex reviewer adapter、review runner、
  fallback service、completion evaluator、M7 acceptance helpers/tests
- 間接影響: M4のPGID/timeout、M5のbudget、M6のstream redaction/DLP、checkpoint digest
- 外部影響: 実装・テスト中はなし。fake executableだけを使い、実Claude/Codexモデルを
  呼ばない

## 責務境界

1. `review_schema.py`: §9のreview.jsonを10件上限、追加field禁止で検証する。
2. `review_bundle.py`: objective、acceptance、patch、関連箇所、verifyだけを固定順で束ね、
   input digestとUTF-8 bytes/4の保守的token proxyを算出する。
3. `review_adapters.py`: 実CLI helpで確認できたflagだけを使う。Claudeは`--safe-mode`、
   `--tools ""`、`--strict-mcp-config`、`--no-session-persistence`、`--json-schema`を必須化し、
   version+最小schema callをrun当たり1回だけprobeする。Codexは既存adapterをread-onlyで使う。
4. `review_runner.py`: M4/M6のprocess runnerとstream ingestを再利用し、schema不正本文、
   生stdout/stderr、secret値をManagerへ渡さない。
5. `reviewer.py`: pre-spend不成立時はClaudeへ送らず、claude→codex→noneを有限に1回ずつ試す。
   省略理由は固定reason codeとeventだけに記録する。
6. `completion.py`: verifyにREGRESSION/FLAKY/INCONCLUSIVE、scope外、secret検出があれば
   完了不可。approveならDONE、request_changesは最大2周でESCALATED、noneは
   AWAITING_APPROVALとする。

## invariant

- Reviewerはread-onlyで、tool、MCP、session persistence、commit、push、外部操作を持たない。
- Claudeへ送る前にbundle全体のDLP scanがCLEANでなければspawnしない。
- 上限超過、usage推定不能、scanner不能、CLI flag不明はfail-loudにClaudeを無効化する。
- fallbackはclaude/codex各1回だけ。noneを成功reviewとして扱わない。
- review.jsonの`reviewed_by`と実際に使ったadapterが一致しなければ拒否する。
- `input_digest`は実送信bundleのdigestと一致しなければ拒否する。
- 2回目のrequest_changesでESCALATEDとし、3回目のImplementerを起動しない。
- `claimed_status=done`でもverify不合格ならDONEにならない。

## エッジケース

- Claude CLI自体は存在するが必須flag欠落、version probe timeout、最小call失敗
- Claude出力wrapper欠損、壊れたJSON、schema不正、reviewed_by/input_digest偽装
- bundleが30k超、patch/findings/verify内だけにsecret、scanner起動不能
- Claude失敗後のCodex成功、両方失敗、Codex schema不正、stdout/stderr DLP検出
- verify gateがBASELINE_FAILED、FLAKY、scope外、secret quarantine
- approveなのにverify fail、request_changes 1周目/2周目、reviewer none

## データフロー

`objective/acceptance/diff/context/verify` → allowlist bundle → token proxy + DLP →
`Claude probe once` → Claude review / Codex read-only fallback / none →
stream DLP → review schema + identity/digest照合 → checkpoint → completion evaluator →
DONE / FIXING / ESCALATED / AWAITING_APPROVAL

## TDDと検証

- Red: schema、bundle、adapter probe/argv、fallback、completion、AT-6/14/16を先行追加
- Green: M7対象pytest、次に全pytest
- Refactor: `ruff check`、変更ソースmypy、`git diff --check`
- E2E: fake Claude/Codex executableでprobe→fallback→review保存→完了判定を通し、
  親worktree不変、生出力非露出、budget invocationの有限性を確認する
