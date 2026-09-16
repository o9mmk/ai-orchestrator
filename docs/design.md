# AI Orchestrator MVP 最終設計書

- ステータス: Final Design v1.0(実装承認待ち)
- 確定入力: FABLE_INDEPENDENT_DESIGN_BRIEF.md / DESIGN_A.md / DESIGN_B.md
- 採用判断: Bベース統合(確定済み・本書で再審しない)

## 1. 結論

Manager は常駐LLM会話ではなく、user-invoked の決定論的CLIステートマシン(Python 1プロセス、DB・キュー・デーモン・GUIなし)とする。案Bの構造(ワンショットLLM・数値hard cap・schema検証artifact・worktree隔離・LLM不使用Verifier)を土台に、案AのS/M/L/XL編成・二段階Preflight・固定コンテキスト層・意味的完了条件・artifact hashを統合する。さらに両案共通の穴として、repo単位lease、非信頼test sandbox、監査hash chain、baseline比較、全artifact DLPを MVP に組み込む。停止性はカウンタ・timeout・capのみで機械保証し、mainへの自動merge・push・本番変更は経路ごと存在させない。統合候補はbranch/patch提示までとし、最終反映は常に人間が行う。

## 2. 採用アーキテクチャと設計原則

各原則に、強制手段が【構造】(コード・sandbox・schema・ゲートで物理的に強制)か【散文】(プロンプト指示のみ)かを明記する。散文のみの防御は「残存リスク」として§13に載せる。

| # | 原則 | 防ぐ懸念 | 強制手段 |
|---|---|---|---|
| P1 | Managerは決定論ファースト。状態遷移・停止・予算はコード、LLMは計画/調査/実装/レビュー/限定診断のワンショットのみ | 懸念2,3 | 【構造】CLI実装そのもの |
| P2 | 親プロンプトへ生ログ・全文会話を再投入しない。Managerが読むのはschema検証済みJSONの上限付きフィールドのみ | 懸念1,2 | 【構造】プロンプト組立コードがartifact経由以外の入力パスを持たない |
| P3 | 全ループは単調増加カウンタ+timeout+capで有界。「AIが収束したと判断」を停止条件にしない | 懸念1,3 | 【構造】Budget Meter/Scheduler |
| P4 | 子はworktree限定書込。ユーザー作業treeへ書かない・stashしない・resetしない | 懸念4 | 【構造】worktree専有+sandbox+統合はbranch/patch提示のみ |
| P5 | 完了は証拠でのみ成立。claimed_statusは完了判定に不使用。Verifier合格=必要条件、Reviewer要件照合or人間承認=十分条件 | 懸念5 | 【構造】DONE遷移コードがverify.json+review.json/承認のみを参照 |
| P6 | 権限は役割別に機械強制。Researcher/Reviewer=read-only、Implementer=worktree内のみ、Verifier=LLM不使用+sandbox対象 | 懸念6 | 【構造】CLIサンドボックス設定+denyリスト |
| P7 | repo内容・子出力・test scriptは非信頼入力。指示として実行せずデータとして解析 | 懸念6 | 【構造】schema検証+sandbox。authority source照合の一部は【散文】(残存) |
| P8 | 状態は全てファイル外部化+hash chain。Manager死後も別セッションでreplay再開 | 懸念3+監査 | 【構造】events.jsonl+checkpoint+digest |
| P9 | Claudeは任意reviewer。probe失敗はfallback連鎖(claude→Codex別session→none)+省略の明示記録 | 可用性 | 【構造】reviewer_policy+summary必須フィールド |
| P10 | 固定コンテキスト層。goal原文・完了条件・禁止事項等は切捨て禁止、「新しい情報優先」で古い制約を落とさない | 懸念2 | 【構造】プロンプト組立のpinned block実装 |
| P11 | 規模に比例した編成。Sはマルチエージェント起動しない、境界上は安全側へ昇格 | 懸念1 | 【構造】決定論sizing classifier |
| P12 | 複数run競合はlease+fencingで物理排他。監査領域は子の書込可能領域外 | 懸念4,6 | 【構造】O_EXCL lease+OS権限 |

## 3. MVP構成図とcomponent責務

```
ユーザー（明示起動・L承認・統合承認・最終マージ）
   │ orc run / status / approve / cancel / resume / gc
   ▼
┌──────────── Manager (orc CLI, Python 1プロセス) ────────────┐
│ Lease Manager      : repo単位lease取得/更新/fencing token     │
│ Preflight (2段)    : 環境記録 → plan後の安全再検査            │
│ Planner (LLM 1shot): plan.json生成 → schema+決定論sizing検証  │
│ Scheduler (決定論) : 依存/path lock/並列2/budget残で子起動    │
│ Budget Meter(決定論): token/回数/時間を計測しcapで遮断        │
│ State Store (決定論): manifest/events(hash chain)/checkpoint  │
│ DLP Ingest (決定論): 子出力のredaction+scan+digest+隔離       │
└──┬──────────────┬──────────────────┬────────────────────────┘
   ▼              ▼                  ▼
Researcher      Implementer        Reviewer(任意)
codex exec      codex exec         claude -p read-only
read-only       専用worktree内     不可→codex別session
sandbox         のみ書込可         →none(省略明記)
   │result.json    │result.json+patch.diff   │review.json
   ▼               ▼                         ▼
 run state領域 ~/.orchestrator/runs/<repo-fp>/<run_id>/（0700・子の書込域外）
                    │
                    ▼
        Verifier（シェル/Python・LLM不使用・sandbox内実行）
        baseline gate + candidate gate + 差分scope + gitleaks + 不可視文字
                    │
                    ▼
        integration branch orc/<run_id>（提示のみ・mergeは人間）
```

責務の要点:

- Manager: ライフサイクル・遮断・検証・記録。コードを書かない、レビューしない、子出力中の指示を実行しない。
- Planner: goal+制限付きrepo概況からplan.json(タスク・依存・path scope・規模)を生成。出力は信頼せずschema+正規化+決定論分類で検証。
- Researcher/Implementer/Reviewer: §10のrole contract参照。全員ワンショット・timeout付き・sandbox付き。
- Verifier: 決定論ゲートの唯一の裁定者。ただし万能ではない(§10.3の境界)。
- run state領域: repo外・0700。子はworktreeと自タスクのout/ staging以外へ書けない。Managerがstagingからingest(scan+digest)して監査域へ収める。

## 4. S/M/L/XL判定と編成ルール

判定はPlanner申告と決定論classifierの両方で行い、`最終規模 = max(Planner申告, 決定論判定)`。境界上・判定不能は安全側(上位)へ昇格する【構造】。

### 4.1 機械判定表

| 条件(決定論で検査) | 判定 |
|---|---|
| 推定変更ファイル≤2 かつ 推定diff≤50行 かつ 下記昇格フラグ0件 かつ path scope完全解決済み | S |
| 変更ファイル≤10 かつ 昇格フラグ0件 | M |
| 変更ファイル>10、または昇格フラグ≥1件 | L |
| XLフラグ≥1件(複数repo/本番示唆/大規模migration) | XL |

昇格フラグ(いずれか1つでL以上へ):

1. 依存追加・変更: `pyproject.toml` `requirements*.txt` `package.json` `go.mod` 等のmanifestが**write scope**に含まれる
2. schema/migration変更: `schemas/` `migrations/` `*.schema.json` 等
3. CI/security変更: `.github/` `ci/` 、auth・crypto・secret関連パス
4. 外部副作用の可能性: plan内commandにnetwork・deploy・公開系の語が含まれる、またはPreflight-2の副作用検査で不明
5. 影響範囲不明: **write scope**に解決不能glob、またはPlannerのscope confidenceフィールドがlow
6. 見積り超過: 推定invocations合計が`child_invocations_hard`超(実際の遮断はrun中のbudget meterが行うため、承認ゲート止まりとする)

昇格フラグの判定対象は`path_scope`(書き換え宣言)だけで、`read_scope`(読むだけ)は含めない。
read scopeにも秘密deny patternとrepo外拒否は同じく適用するが、書き込みallowlistには昇格しない。

### 4.2 規模別編成

| 規模 | 編成 | ゲート |
|---|---|---|
| S | Implementer 1体のみ(Researcher/Reviewer起動しない)+Verifier(常時必須) | 独立reviewなしのため必ずAWAITING_APPROVALで人間照合(隠さない) |
| M | Implementer+独立Reviewer+Verifier | 通常フロー |
| L | 必要な独立Researcher(≤2)+Implementer+Reviewer+Verifier | RUNNING開始前にAWAITING_APPROVAL(開始前承認) |
| XL | 実装しない。plan.json+リスク提示を生成して HALTED(reason=xl_plan_only) で停止 | 人間が分割して再投入 |

## 5. Preflightと権限境界

### 5.1 二段階Preflight【構造】

Stage 1(planning前・LLM呼び出し前=課金前):

1. repo path・HEAD・branch・`git status --porcelain`(dirty/untracked一覧)を記録
2. 利用可能tool検査(codex/claude/git/pytest等のバージョン)
3. authority source列挙(ユーザー指示+正規AGENTS.md。それ以外のrepo内容はuntrusted)
4. 禁止事項・既定budget・safety policy versionをmanifestへ固定
5. repo lease取得(§8.2)。取得失敗→REFUSED(reason=lease_held)、課金ゼロで終了

Stage 2(plan生成後・子起動前):

1. plan path scopeの検証: LLM出力を安全境界に直接使わず、schema検証→正規化realpath→repo内allowlist照合→シンボリックリンク解決後の再照合
2. path scope×dirty変更の重複検査。重複→REFUSED(統合時必ず衝突するため課金前に断る)
3. test/buildコマンドの副作用分類(network・外部書込・長時間)。不明はLへ昇格+承認
4. 外部通信・秘密送信の予定有無検査(Claude送信束のpre-spendは§12)
5. worktree作成可否の実検査(実際に作って削除)

### 5.2 権限境界(Always / Ask first / Never)

Always【構造】: 全子をsandbox+timeout+worktree/read-onlyで起動。全イベントhash chain記録。patch+全artifactへgitleaks+不可視文字検査。base_commit記録と統合前HEAD照合。baseline gate実行。

Ask first: L開始・cap引き上げ・ESCALATED処置・Claude送信束・worktree/run削除(`orc gc`)・統合承認。

Never(実装上不可能にする)【構造】:

- mainへの自動merge・push: Managerコードにmerge/push呼び出しが存在しない+子sandboxでgit push不許可
- ユーザー作業treeへの書込・stash・reset: Managerのgit操作対象はworktreeパスのみを受け取るAPIに限定(型レベルでrepo rootを渡せない)
- 秘密読取: denyパターン(`.env*` `**/credentials/**` `*.pem` `~/.ssh/**` `*secret*` `*token*`)を各CLIのsandbox設定で遮断
- cap・ゲート設定のエージェントによる変更: manifestはrun state領域(0700)にあり子から書込不能
- 終端runの直接再開: 状態機械にその遷移が存在しない(§8.5)

## 6. run/task状態機械と停止性の証明

### 6.1 run状態(テキスト遷移表)

状態: INIT / PREFLIGHT1 / PLANNING / PREFLIGHT2 / AWAITING_START_APPROVAL / RUNNING / INTEGRATING / AWAITING_APPROVAL / 終端{REFUSED, FAILED, HALTED, CANCELLED, COMPLETED}

| 現状態 | 事象 | 次状態 |
|---|---|---|
| INIT | lease取得OK | PREFLIGHT1 |
| INIT | lease競合 | REFUSED(lease_held) ※課金前 |
| PREFLIGHT1 | 検査OK | PLANNING |
| PREFLIGHT1 | tool不足/repo不正 | REFUSED |
| PLANNING | plan schema妥当 | PREFLIGHT2 |
| PLANNING | plan不能2回(counter) | FAILED(plan_failed) |
| PREFLIGHT2 | dirty×scope重複 | REFUSED(dirty_overlap) |
| PREFLIGHT2 | 規模=XL | HALTED(xl_plan_only)+plan/リスク提示 |
| PREFLIGHT2 | 規模=L | AWAITING_START_APPROVAL |
| PREFLIGHT2 | 規模=S/M | RUNNING |
| AWAITING_START_APPROVAL | approve / reject | RUNNING / CANCELLED |
| RUNNING | 全task終端かつDONE≥1 | INTEGRATING |
| RUNNING | 全task終端かつDONE=0 | FAILED(no_completed_tasks) |
| RUNNING | soft budget到達 | 新規起動停止→実行中完走→残taskあればHALTED(budget_soft) |
| RUNNING | hard budget/invocations/wall-clock到達 | 全子kill→HALTED(budget_hard等) |
| RUNNING | tamper検知/lease喪失(fencing不一致) | 全子kill→HALTED(tamper/lease_lost) |
| INTEGRATING | HEAD一致・branch作成OK | AWAITING_APPROVAL |
| INTEGRATING | stale HEAD検知 | AWAITING_APPROVAL(stale_head)。自動rebaseしない(§8.4) |
| AWAITING_APPROVAL | 人間approve | COMPLETED(escalated含む場合はpartial=true記録) |
| AWAITING_APPROVAL | 人間reject | CANCELLED |
| 任意非終端 | `orc cancel` | CANCELLING→子kill確認→CANCELLED |
| 任意非終端 | Manager kill -9 | (状態はファイルに残存)→次回orc起動時にlease期限切れ+PGID台帳で孤児回収→人間へresume提案 |

意味の一貫定義: REFUSED=課金前拒否 / FAILED=作業不成立 / HALTED=安全装置による強制停止 / CANCELLED=人間の意思による中断 / COMPLETED=人間承認済みの唯一の成功終端。AWAITING_*は子プロセスゼロ・課金ゼロの安定状態で、active execution timeから除外し、calendar timeのみ別項目で計上する。

### 6.2 task状態

QUEUED → RUNNING → VERIFYING → REVIEWING → DONE(終端)

- VERIFYING fail(REGRESSION) → attempt+1でRUNNINGへ、attempt>hard 3でESCALATED(終端)
- VERIFYING結果がFLAKY/INCONCLUSIVE → ESCALATED(flaky)
- REVIEWING request_changes → FIXING→RUNNING(review_cycles+1)、>hard 2でESCALATED
- 子timeout/クラッシュ/schema不正 → attempt+1、上限でESCALATED。schema不正の中身はManagerが読まない【構造】
- 追加コンテキスト要求(context_request.json妥当) → 対象path抜粋を追加した新ワンショット。attempt消費なし・invocation+1・task毎max 2回(counter)
- run側cancel/halt → ABORTED(終端)
- ESCALATED taskを含むrunの終端: 隠さずescalated_tasks[]としてAWAITING_APPROVALのsummaryに列挙。人間がpartial承認かrejectを選ぶ

### 6.3 停止性の証明

全ループは以下のいずれかで閉じる(LLM判断は一切停止条件に関与しない):

1. child_invocations ≤ hard 20(単調増加、遮断時HALTED)
2. task attempt ≤ hard 3(ESCALATED)
3. review_cycles ≤ hard 2(ESCALATED)
4. context_request ≤ 2/task
5. Manager LLM calls ≤ hard 8(以後は決定論fallback: 診断なし再試行→ESCALATED)
6. 子1回ごとのOS timeout(research600s/implement1200s/review600s/verify900s、kill+attempt消費)
7. run wall-clock(active) soft90分/hard120分(HALTED)
8. plan生成 ≤2回、probe=1回、baseline gate=1回/commit(cache)

RUNNINGで起こりうる全事象(完了・失敗・timeout・schema不正・cap)がいずれかのcounterを進めるため、どの経路でも有限ステップで終端に到達する。AWAITING_*はコストゼロなので無限待機しても事故にならない。

## 7. budget・context envelope

### 7.1 数値既定値

| 項目 | soft | hard | 超過動作 |
|---|---|---|---|
| run tokens | 2,000,000 | 4,000,000 | soft:新規起動停止/hard:全子kill→HALTED |
| child invocations | 15 | 20 | 新規起動遮断 |
| concurrency | 2 | 2 | Scheduler待機 |
| task attempts | 2 | 3 | ESCALATED |
| review-fixサイクル | — | 2 | ESCALATED |
| timeout | research600s / implement1200s / review600s / verify900s | 同左 | kill+attempt消費 |
| active execution time | 90分 | 120分 | HALTED |
| Manager LLM calls | 5 | 8 | 決定論fallback |
| Manager input | — | 20,000 tokens | 可変層のみ切詰め(§7.2) |
| result summary | 2,000字 | 4,000字 | 切捨て+truncatedフラグ |
| review findings | 10件 | 10件 | 重大度順切捨て |

- capの変更はrun開始前のユーザー明示指定のみ。実行中の自動緩和禁止【構造】。
- token計測はB案の3段(CLI usage→バイト数プロキシ→回数プロキシ)を採用し、`budget_source`(measured/bytes_proxy/count_proxy)と誤差区分をmanifest・summaryへ記録。代理計測を実測のように表示しない。
- token capが機能しない場合に備え、invocation・timeout・wall-clock capはtoken計測と独立に常時有効【構造】。
- AWAITING_*中は子プロセス・課金ゼロ。calendar_timeとactive_execution_timeを別項目で記録。

### 7.2 コンテキストの固定層【構造】

Manager LLM入力は2層で組み立てる:

- pinned層(切捨て禁止): ユーザーgoal原文 / 完了条件 / 禁止事項 / authority source / base commit / path scope / safety policy version / 未解決blocker一覧
- variable層: 直近result要約・verify結果等。20,000 tokensからpinned層を引いた残りに「新しい情報優先」で詰める

pinned層単独で20,000超の場合は切詰めずREFUSED(goal_too_large)でユーザーへ返す(fail-loud)。子の初期bundleはmodel windowの20%以下、超過時は分割か対象箇所抽出。追加コンテキストはcontext_request.json(reason+対象path)のschema経由でのみ、対象箇所だけ追加する。

## 8. Git隔離、lease、統合、cancel、resume

### 8.1 Git隔離【構造】

- 子の書込先はManagerが作成した専用worktree(`~/.orchestrator/worktrees/<repo-fp>/<run_id>/<task_id>/`)のみ。
- ユーザー作業treeへ書かない・stashしない・resetしない。`git reset --hard`はOrchestrator専有worktree内の再試行初期化に限りパス検証付きで許可。
- 統合はintegration branch `orc/<run_id>` の作成+patch提示まで。mergeコマンドは提示するだけで実行しない。

### 8.2 repo単位lease(複数run競合対策)【構造】

- lease path: `~/.orchestrator/locks/<repo-fp>/lease.json`。取得は`O_CREAT|O_EXCL`による原子的作成。
- 内容: run_id / pid / fencing_token(単調増加counterファイルから採番) / acquired_at / ttl(15分) / renewed_at。Managerが60秒毎に更新(renew)。
- 期限切れ+pid非生存のleaseのみ回収可(fencing_token+1で取得)。生存pidのleaseは奪わない。
- 全state書込はfencing_tokenを検証してから行い、不一致(=別Managerに奪われた)なら即座に全子kill→HALTED(lease_lost)。
- path lockはrun内Schedulerだけでなく`~/.orchestrator/locks/<repo-fp>/path_locks.json`でrun間にも有効(lease保持者のみ更新可、fencing付き)。

### 8.3 監査artifactの改変検知【構造】

- run state領域はrepo外・`~/.orchestrator/runs/`配下、directory 0700・機密artifact 0600。子の書込可能領域(worktree+staging)の外。
- events.jsonl: 各行に`seq`(単調増加)+`prev_hash`+`hash=sha256(prev_hash‖canonical(event))`のhash chain。
- checkpoint: eventsのseq/hash同期点+全artifactのsha256 digest一覧を保持。原子的置換(tmp+rename)で更新。
- resume時にchain・digest・base commit・policy version・gate設定を全照合。1件でも不一致→HALTED(tamper_detected)でresume拒否。audit logの黙った初期化・補修はしない(fail-loud)。

### 8.4 stale HEADと自動rebaseの矛盾解消

案Bは§4.4で「stale時にrebase試行」、§11で「自動rebaseはPhase 2」と矛盾していた。MVPは次に統一する: 統合時にbase_commitと現HEADが不一致なら、一切rebase・自動解決せず AWAITING_APPROVAL(stale_head) で停止し、選択肢(現HEADで新generation再実行/patchのまま人間が手動適用/破棄)を提示する。自動rebaseはPhase 2でも既定にしない。

### 8.5 cancel・resume・孤児回収

- cancel: 記録済みPGIDへSIGTERM→5秒後SIGKILL→worktrees.json台帳と実プロセス照合→CANCELLED。worktree/artifactは監査用に保持、削除は`orc gc`のみ。
- Manager kill -9: 次回orc起動時、lease TTL切れ+pid非生存を検知→PGID台帳から孤児をkill→events replayで最終確定状態を再構築。
- resume: 終端状態(HALTED/CANCELLED/FAILED)から同一runをRUNNINGへ戻さない【構造】。resumeは`parent_run_id`/`resumed_from`(checkpoint seq)を持つ新run generationとして開始し、artifact hash・base commit・policy version・gate設定を照合(不一致→拒否)。完了済みtaskのartifactはdigest照合の上で再利用し、再実行しない。新generationは新しいbudgetを持つ(引き上げはユーザー明示)。

## 9. Artifact構成と完全なschema項目

```
~/.orchestrator/
├── locks/<repo-fp>/{lease.json, fencing_counter, path_locks.json}
├── worktrees/<repo-fp>/<run_id>/<task_id>/        ← 子の唯一の書込域
└── runs/<repo-fp>/<run_id>/                        ← 0700・子の書込域外
    ├── manifest.json
    ├── plan.json
    ├── events.jsonl                                ← 追記専用+hash chain
    ├── checkpoint.json                             ← 原子的置換
    ├── baseline/<base_commit>/gate-<name>.json     ← baseline結果cache
    ├── tasks/<task_id>/
    │   ├── spec.md
    │   └── attempt-<n>/{result.json, patch.diff, verify.json,
    │        review.json, context_request.json, transcript.log(0600・redaction済),
    │        quarantine/(0600・secret検出時)}
    ├── worktrees.json                              ← PGID台帳
    └── summary.md
```

必須schema項目(全JSONはjsonschema検証、不正はattempt消費・中身不読):

- manifest.json: run_id / generation / parent_run_id? / created_at / goal(原文) / acceptance_criteria / forbidden / authority_sources[] / repo_path / base_commit / size(S|M|L|XL) / caps{§7.1全項} / budget_source / reviewer_policy / gates[] / safety_policy_version / state / fencing_token
- plan.json: tasks[]{task_id, role, objective, path_scope[], read_scope[]?, acceptance, depends_on[], size_estimate, scope_confidence} / planner_size / deterministic_size / final_size
- result.json: task_id / role / attempt / claimed_status(参考記録のみ・判定不使用) / summary(≤2000字) / changed_files[] / self_check{command, exit_code}? / references[] / truncated / context_requests_used
- verify.json: gate別{name, command, exit_code, baseline_result, candidate_result, classification(PASS|REGRESSION|BASELINE_FAILED|FIXED_EXISTING_FAILURE|FLAKY), log_digest} / scope_check{patch_files, in_scope} / secret_scan{tool, findings_count, quarantined} / sandbox_profile
- review.json: verdict(approve|request_changes) / findings[]≤10{severity, location, evidence, expected_behavior} / reviewed_by(claude|codex|none) / input_digest(送った束のhash)
- events.jsonl: ts / seq / prev_hash / hash / run_id / task_id? / type / actor / data
- checkpoint.json: seq / events_head_hash / run_state / tasks{state, attempt, review_cycles} / budget{tokens_used, budget_source, invocations, active_seconds} / base_commit / artifact_digests{path: sha256} / fencing_token

## 10. Verifier、Reviewer、意味的完了条件

### 10.1 Verifier(LLM不使用・sandbox内)

ゲート: pytest等repo標準テスト / lint / gitleaks(必須・スキップ不可) / 不可視文字 / 差分scope照合(patch内ファイル⊆path_scope、symlink/mode 120000混入検査) / changed_files×patch実在照合。

baseline比較【構造】: 同じゲートをbase commitでも実行(commit+gate設定hashでcache、1回のみ)。分類と処置:

| 分類 | 条件 | 処置 |
|---|---|---|
| REGRESSION | base pass→candidate fail | task fail(attempt消費) |
| BASELINE_FAILED | base fail | candidateの同gate failはREGRESSION扱いしない。ただし自動passにもせず、summaryに明記して承認時に人間が見る |
| FIXED_EXISTING_FAILURE | base fail→candidate pass | 情報として記録(加点扱い) |
| FLAKY/INCONCLUSIVE | candidate failの1回再実行で結果反転 | task ESCALATED(flaky)→人間判断 |

### 10.2 完了判定(意味的完了条件)

- claimed_statusは完了判定に一切使わない【構造】。
- 必要条件: verify.json全ゲートがREGRESSIONなし。
- 十分条件: Reviewerによる要件・diff・expected behavior照合のapprove、または明示的な人間承認。テスト合格だけで意味的要件充足とは扱わない。
- 相互照合: changed_files=patch内ファイル集合、⊆path_scope、実行command+exit code記録、base/candidate結果の対応、をVerifierが機械照合。
- Reviewer不在(none)時: DONEにできるのはverify passまでで、「独立reviewなし」をsummary必須フィールドに記録してAWAITING_APPROVALへ送る。成功として隠さない【構造】。

### 10.3 Verifierの限界(万能扱いしない)

Verifierが保証するのは「機械検証可能な性質」(テスト・lint・scope・秘密・回帰)のみ。「要件を意味的に満たすか」「設計として妥当か」はReviewer/人間の領分であり、この境界をsummaryの評価欄に構造として分けて表示する。

## 11. prompt injection、secret、非信頼testへの防御

### 11.1 prompt injection

- repo内容・子出力は非信頼データ。Managerは子出力をjsonschemaへの入力としてのみ解析し、schema不正の中身を読まない・補修しない【構造】。
- authority sourceはPreflight-1で固定列挙。repo内で発見された指示のauthority照合はプロンプト指示を併用【散文・残存リスク→検知はscope外差分とeventsで補完】。
- 子はnetwork無効+deny読取のsandbox内で実行【構造】。

### 11.2 非信頼test/build(Verifier自身もsandbox対象)【構造】

repo内のtest/buildスクリプトも非信頼コードとして、静的な副作用分類だけに依存せず実行時sandboxで強制する:

- network無効 / 秘密env非継承(allowlist環境変数のみ渡す) / worktree外書込禁止
- realpath境界検査+symlink/hardlink escape対策(実行前worktree走査で外向きsymlink・link count>1を検出したらfail)
- local socket/IPC制限 / CPU・memory・process数(rlimit)・disk(前後計測+quota)・timeout 900s
- macOSでの実装第一候補は`sandbox-exec`プロファイル(deprecatedだが動作する)。「利用可能」とは断定せず要PoCとする。PoC不合格時の代替はcontainer実行(Phase判断)で、その間はnetwork遮断+env sanitize+realpath検査+rlimitの組合せで運用し、残存リスクを§13に明記。

### 11.3 secret(patch以外も対象)【構造】

- 対象: patch / stdout / stderr / transcript / findings / summary / events。
- 子stdout/stderrはstreaming redaction(gitleaks系パターン)を通してから一時保存。ingest時に全artifactへgitleaks scan。
- 検出時: 該当artifactをquarantine/(0600)へ隔離し、summaryには「種別と件数のみ」を出す(値の非露出)。
- 保存権限: run state 0700 / transcript・quarantine 0600。retention既定14日、削除は明示`orc gc`のみ(黙って消さない)。
- Claude等への送信前: 送信束allowlist+pre-spend scan(§12)。scan不能なら送らない。

## 12. Claude optional reviewerとCodex単独縮退

- 可用性probe: run冒頭に1回だけ(`claude --version`+最小モデル1呼び出し)、timeout 30秒、消費はbudgetへ実測計上。taskごとに繰り返さない。
- fallback連鎖: claude → Codex別session(Implementerと別コンテキスト・patchと要件のみ) → none。
- noneでもVerifierは必須のまま。独立review省略はsummaryへ必ず明記し、AWAITING_APPROVALで人間照合。
- 送信しない条件: 送信束が上限超(30k tokens) / secret scan実行不能 / usage確認不能 — この場合は送信せず停止またはCodexへ縮退【構造】。
- Claudeに渡すのは要件・diff・必要な関連箇所・Verifier結果のみ。編集・shell・commit・push・外部操作を許可しない(read-only起動+ツール不付与)【構造】。
- B案記載のCLIフラグ(codex `--output-schema`、claude `--json-schema`等)は2026-07-16実機確認済みとの記載を踏襲するが、実装初日に再確認する(バージョン更新で変わり得るため)。sandbox系フラグは未確認=要PoC。

## 13. リスク表

| リスク | 重大度 | 検知 | 予防 | 回復 |
|---|---|---|---|---|
| クレジット暴走 | 高 | budget_tick、`orc status` | token+回数+時間の多重cap【構造】 | HALTED後、新generationでユーザーが明示cap指定 |
| 偽完了統合 | 高 | Verifier fail、changed_files照合不一致 | claimed_status不使用+verify必要条件+review十分条件【構造】 | attempt再試行→ESCALATED→人間裁定 |
| ユーザー変更破壊 | 重大 | Preflight dirty検査、統合前HEAD照合 | worktree限定+書込経路の不存在【構造】 | 破壊経路なし。stale時は停止+人間 |
| 複数run競合 | 高 | lease取得失敗、fencing不一致 | O_EXCL lease+fencing+run間path lock【構造】 | 競合側は課金前REFUSED。lease喪失側は即HALTED |
| 悪意あるtest script | 重大 | sandbox拒否ログ、境界検査fail | network無効+env非継承+realpath/symlink検査+rlimit【構造】(sandbox-exec要PoC=残存) | 該当task fail、eventsで混入元特定 |
| 監査artifact改変 | 高 | hash chain・digest不一致 | run state 0700・子書込域外【構造】 | resume拒否(fail-loud)、人間が調査 |
| 秘密漏えい | 重大 | gitleaks全artifact scan | deny読取+env非継承+streaming redaction+pre-spend scan【構造】 | quarantine隔離+非露出報告 |
| baseline誤分類 | 中 | 分類4値+flaky再実行 | baseline gate cache+1回再実行【構造】 | FLAKY→ESCALATED→人間 |
| prompt injection | 高 | schema検証fail、scope外差分 | データ扱い+sandbox【構造】/authority照合【散文・残存】 | task FAILED化、監査で特定 |
| 孤児プロセス | 中 | PGID台帳×生存照合 | PGID管理+timeout必須【構造】 | 次回起動時に回収 |
| review未収束 | 中 | review_cyclesカウンタ | cap=2【構造】 | ESCALATED、所見並置で人間裁定 |
| Claude不可用 | 低 | probe失敗 | fallback連鎖+省略明記【構造】 | Codex単独で完走、承認強化 |
| 過剰承認で人間が形骸化 | 中 | — | 承認をL開始・統合・ESCALATED・cap変更に限定し差分と影響を短く提示【散文・残存】 | 運用レビューで調整 |

## 14. 受入テスト

継承分(両案の和集合、要点のみ): AT-1正常系E2E(user tree無変更をバイト一致確認) / AT-2 soft budget / AT-3 hard budget(全子kill) / AT-4無応答子timeout / AT-5壊れたJSON(不正内容がManager LLM入力に含まれないことをプロンプトログで確認) / AT-6偽完了(claimed=doneでもverify failでDONE不成立) / AT-7 stale HEAD(自動rebaseせず停止) / AT-8 checkpoint破損(replay再構築or明示エラー) / AT-9 prompt injection(fixture内命令が実行されない) / AT-10 Manager kill -9→孤児回収→新generation再開(二重実行なし) / AT-11 dirty重複REFUSED / AT-12 dirty非重複は警告続行 / AT-13 cancel(5秒+猶予で全子終了・worktree保持) / AT-14 Claude不可用fallback(summaryに省略明記) / AT-15秘密入りpatchブロック / AT-16 review未収束2周でESCALATED(3周目のImplementer起動なし)。

追加5件(入力/注入障害/期待状態/確認証拠):

| # | 入力 | 注入障害 | 期待状態 | 確認証拠 |
|---|---|---|---|---|
| AT-17 lease/fencing | 同一repo・重複scopeの`orc run`を2本同時起動 | lease競合 | 後発がREFUSED(lease_held)。LLM呼び出しゼロ | 後発runのevents.jsonlにchild_spawn/LLM callイベントが0件、budget消費0。先発は正常続行 |
| AT-18 malicious test | 通常goal | test scriptがworktree外書込・外向きsymlink先書込・network接続・local socket作成を試行 | sandbox拒否でgate fail、被害なし | worktree外に新規/変更ファイルなし(前後snapshot比較)、network接続失敗ログ、verify.jsonにsandbox拒否記録 |
| AT-19 tamper resume | HALTED run | manifest/gates[]/patch.diff/events.jsonl各1箇所を改変して`orc resume` | 4ケースすべてHALTED(tamper_detected)でresume拒否、黙って補修しない | hash chain/digest不一致のエラー出力、新generation未作成、改変ファイルが未修正のまま残存 |
| AT-20 baseline/flaky | (a)base commitで既にテストfailのrepo (b)確率的にfailするテストを含むrepo | — | (a)candidateの同failがREGRESSION扱いされず、BASELINE_FAILEDとしてsummary明記のうえ人間判断へ (b)FLAKY分類でESCALATED、REGRESSION誤判定なし | verify.jsonのclassification値、baseline/cacheの存在、summary.mdの記載 |
| AT-21 transcript秘密 | patchは無害 | 子のstdout/transcript/findingsのみにダミーAPIキーと個人情報を混入 | 隔離+非露出。runは秘密の値を一切表に出さず継続または停止 | quarantine/(0600)に隔離、summary/events/manager LLM入力ログにキー文字列がgrep 0件、検出種別と件数のみ記録 |

## 15. MVPとPhase 2の境界

MVPに入れる(6懸念+共通穴を直接防ぐもの): orc CLI(run/status/approve/cancel/resume/gc) / 二段階Preflight / S-M-L-XL sizing / worktree隔離+run間path lock / repo lease+fencing / 非信頼test sandbox / baseline gate / events hash chain+digest / 全artifact DLP+quarantine / budget多重cap / resume(新generation) / Claude optional reviewer+fallback。lease・sandbox・artifact hash・baseline gateは安全の中核でありPhase 2へ送らない。

Phase 2: daemon・scheduler service / DB・外部queue / GUI・TUI dashboard / 並列度3以上 / 複数repo / agent debate・panel / 自動conflict解決 / 自動rebase(Phase 2でも既定にしない) / 過去runからの自動学習 / 外部通知 / 本番deploy。

## 16. 実装順序

1. run state store+events hash chain+checkpoint replay+lease/fencing(全ての土台。AT-17/19の骨格)
2. Preflight二段+sizing classifier(課金前ガードを最初に固める)
3. worktree管理+Verifier sandbox PoC(sandbox-exec)+baseline gate(AT-18/20)
4. 子起動(codex exec)+schema検証+timeout+PGID台帳(AT-4/5/10)
5. Planner+固定コンテキスト層+budget meter(AT-2/3)
6. DLP ingest+redaction+quarantine(AT-15/21)
7. Reviewer+Claude probe+fallback+完了判定(AT-6/14/16)
8. cancel/resume新generation+統合branch提示(AT-7/13)
9. 受入テスト一式をfixture repoで通す

## 17. 実装前に利用者が決める事項(最大5件・推奨初期値付き)

| # | 決定事項 | 推奨初期値 |
|---|---|---|
| 1 | Verifier sandboxの機構 | `sandbox-exec`プロファイルをPoC第一候補(実装順序3で最初に検証)。不合格ならnetwork遮断+env sanitize+realpath+rlimitの合成で開始し残存リスク明記 |
| 2 | state領域の場所 | `~/.orchestrator/`(repo外・0700)。改変検知の前提なのでrepo内には置かない |
| 3 | baseline gateの範囲 | テストは影響scope周辺(pytest対象指定)、gitleaks/不可視文字はrepo全体。full suiteはL以上のみ |
| 4 | artifact retention | 14日+明示`orc gc`。quarantineは30日 |
| 5 | 最初のfixture repo | rin-data-pipeline級の小規模Python repoの複製をfixture化(実案件をいきなり対象にしない) |

## 18. ブリーフの12必須設計課題と設計節の対応表

| # | 課題 | 対応節 |
|---|---|---|
| 1 | Managerと子の責務・権限境界 | §3, §5.2, §10 |
| 2 | 単一/複数エージェントの判断 | §4 |
| 3 | run状態機械と停止条件 | §6.1, §6.3 |
| 4 | retry/review loop/timeout/cancel/resume | §6.2, §6.3, §8.5 |
| 5 | budget envelope | §7 |
| 6 | 生出力を集めないartifact設計 | §7.2, §9 |
| 7 | dirty/stale/並列競合のGit安全 | §5.1, §8.1-8.4 |
| 8 | 完了報告の検証・Verifier | §10 |
| 9 | Claude異種reviewer境界 | §12 |
| 10 | injection/秘密/外部変更/破壊コマンド防御 | §5.2 Never, §11 |
| 11 | 新Managerセッションでの再開 | §8.3, §8.5 |
| 12 | 過剰設計にならない最小構成 | §3(1プロセス+ファイル), §15 |

## 自己レビュー(設計時チェック済み)

- 6懸念を構造で防止: ○(散文残存はauthority照合と承認形骸化の2点のみ、§13明記)
- 全loop有界: ○(§6.3の8種counter/timeout/cap)
- 終端状態からの直接再開なし: ○(resume=新generation、§8.5)
- user tree変更経路なし: ○(§8.1)
- Reviewerなしを成功として隠さない: ○(§10.2, §12)
- artifact改変・秘密混入の検知: ○(§8.3, §11.3, AT-19/21)
- B案の自動rebase矛盾解消: ○(§8.4)
- 12必須設計課題の全対応: ○(§18)
