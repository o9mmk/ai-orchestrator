# ブラインド比較審査結果（Claude審査員・2026-07-16）

- 審査プロンプト: judge/JUDGE_PROMPT.md
- 対象: design-a.md ／ design-b.md
- 基準正本: design-brief.md

---

## 1. 総評

案A: 要件レベルの網羅性が高く、実行モード編成・重複作業検出・コマンド副作用分類・承認過多対策など運用知見に基づく防壁が豊富。ただしManager自体がLLM会話（前提7「スキル+ファイル+小さな機械ガード」）であるため、cap執行・状態遷移・完了判定の最終執行がLLMの遵守に残り、散文依存が構造的に解消されていない。計測源も未決（§18-4）。

案B: Managerを決定論CLI（Python 1プロセス）としLLMをワンショット呼び出しに限定することで、6懸念の防御を「カウンタ・sandbox・schema検証・OS timeout」というコード層に置いた。停止性の機械的証明（§4.3）、計測3段fallback、events replay再構築まで実装可能な粒度で閉じている。弱点はVerifier自体の隔離未規定と、S規模タスクの縮退経路の欠落。

## 2. 懸念別比較表

| 懸念 | 案A | 案B |
|---|---|---|
| 1. クレジット暴走 | ○ 混在型。70/90/100%停止(CB-002)・重複検出(CB-005)は具体的だが、執行主体がManager LLM+補助スクリプトで計測源は未決(§18-4) | ◎ 構造。決定論Budget Meter+計測3段fallback(§6.2)+回数/時間の独立cap多重。穴=子内部のfan-out消費は計測外 |
| 2. 親コンテキスト肥大 | △ Manager自体がLLM会話。CB-007の「15ターン/60%」トリガーの遵守はManager依存=散文。子生出力隔離(FR-032/034)は良い | ◎ 構造。「Managerは会話を持たない」(P1, §6.5)、LLM入力20k cap。穴=plan用リポジトリ概況の生成手順が薄い |
| 3. 非収束 | ○ 数値cap(§10.3)は完備・無進捗15分は独自の強み。だが遷移判断の執行がLLM | ◎ 構造。単調カウンタ+OS timeout+Manager呼び出し自体のcap8(§4.3, §6.1)。穴=長timeout内の無進捗を早期検知しない |
| 4. 並列編集破壊 | ○ 重複時Preflight停止(FR-012)・隔離不能時停止(FR-013)・worktree保持(FR-046)。隔離の実装機構は未規定 | ◎ 構造。ユーザーtreeへ書き込む経路自体が存在しない(P4: patch+手動マージ)、path lock、AT-11でバイト一致検証。穴=symlink回避 |
| 5. 偽完了 | ○ 証拠schema+決定論Verifier(FR-055〜057)だが、最終完了判定は「Managerが証拠に基づき判断」=LLM裁量が残る | ◎ 構造。DONE=verify.json全passのみで成立、claimed_statusは非権威と明記(§7.2)、changed_files実在照合。穴=Verifier自体の隔離なし |
| 6. 権限事故 | ○ authority source/untrusted区別(FR-016/036)と副作用分類(FR-017)は独自の強み。だが強制機構(sandbox設定)が未規定=散文寄り | ◎ 構造。sandbox・denyパターンをCLI設定で機械強制(§5末尾)、network無効・秘密env非継承。穴=Verifierが実行するテストコマンドの副作用分類がない(Aにはある) |

## 3. 停止性・budget・Git安全・resumeの深掘り

数値既定値の突き合わせ:

| 項目 | A | B |
|---|---|---|
| 再試行 | 既定1 / hard 2 (§10.3) | 既定2 / hard 3 (§6.1) |
| review周回 | 2で`BLOCKED` (FR-054) | 2で`ESCALATED` (§4.3) |
| 子timeout | 期限30分+無進捗15分 | 役割別600〜1,200秒のOS timeout (§6.1) |
| run全体 | 60分 (§10.3) | 90分soft / 120分hard (§6.1) |
| 同時並列 | 既定2 / hard 3 | 2固定 |
| トークン | 推定値+70/90/100%運用 (CB-002) | soft 2M / hard 4M+5run較正手順 (§6.2) |

片方にしかない防壁:

- Bのみ: 子起動のグローバル回数cap 20（Aはタスク数自体に上限がなく、総起動数の天井がない）／Manager LLM呼び出し自体のcap 8と決定論fallback（§6.1）／`AWAITING_APPROVAL`を「子なし・課金なしの安定状態」と定義（§4.1）／計測不能時のバイト・回数プロキシ換算式と`budget_source`記録（§6.2）／Claude可用性の2段プローブ（§6.4）／差分スコープ照合ゲート（§5 Verifier）／worktrees.jsonのPGID台帳による孤児回収（§7.1, AT-10）／checkpoint破損時のevents replay再構築（§7.2）。
- Aのみ: 無進捗15分のheartbeat検知（§10.3）／ポーリング禁止（FR-042）／同一調査・同一全文再読込の重複検出（CB-005）／Manager context 60/75%の切替トリガー（CB-007）／外部レビュー前のpre-spend gate（FR-015, CB-004）／テスト・ビルドコマンドの副作用事前分類（FR-017）／worktreeを人間受理まで保持（FR-046）／状態ファイル耐障害の受入テスト（AC-15）／承認過多対策（R-20, FR-074）。

resumeはBが一段深い: Aは照合と`BLOCKED`移行（FR-075, AC-14）を要件として定めるが、Bは「eventsが正・checkpointはキャッシュ」という復元原理と、HALTED再開時のcap明示引き上げ（§4.4）まで機構を規定している。

## 4. 両案共通の穴（最重要）

1. Verifier/テスト実行そのものの隔離がない。悪意あるpatchが`conftest.py`やMakefileを書き換えれば、Verifierの権限で任意コードが走る。AのFR-017は「既存コマンドの事前分類」でありpatch後の変質を見ず、BはVerifierを「シェルスクリプト」とするだけでsandbox・network遮断を規定していない。
2. 子エージェント内部のfan-outが予算の外。Codex子が自身のネイティブ・サブエージェント機能でさらに委譲すると、両案のカウンタ（起動回数・attempt）は1回分しか数えない。子側のサブエージェント無効化を両案とも明記していない。
3. 同一リポジトリへの多重run防止（run-levelロック）がない。Aのsingle-writer（FR-085）もBのpath lockもrun内の整合性のみで、2つのrunやユーザーの並行worktree操作が競合する経路は塞がれていない。
4. symlink・submoduleによる境界回避。worktree内に外部を指すsymlinkを作れば、書き込み隔離・denyパターン（`.env`等）・差分スコープ照合のいずれもパス文字列ベースでは素通りしうる。両案とも言及なし。
5. 承認の根拠となる要約と実体の機械照合がない。ユーザー承認はManager生成のsummary/最終報告を見て行われるが、summaryが実diff・verify結果と矛盾していないかを検証するゲートが両案にない（AのAC-10は追跡可能性のみ、Bのsummary.mdは照合対象外）。

## 5. 判定

Bベース統合。

理由: ブリーフの品質条件「『AIがうまく判断する』に依存しない」に対し、Bは停止・完了・権限・budgetの全執行をコード層（カウンタ、sandbox設定、schema検証、OS timeout、決定論Verifier）へ移しており、6懸念すべてで防御方式が「構造」に分類できる。Aは個々の防壁の着眼は優れているが、Manager=LLM会話という土台の選択により、最重要のcap執行と完了判定が最終的にLLMの遵守（散文）に残る。これは懸念2と5に対する防御の質的な差であり、行数や体裁の差ではない。実装可能性もBは「CLI 1本+runディレクトリ+シェルゲート」で自己完結し、CLIフラグの実機確認まで済んでいる。

## 6. 統合提案（AからBへ移植すべき要素トップ5）

1. S規模のBYPASSと実行モード編成（A: PR-01, FR-005/021, §10.1）→ B §3のPlanner/Schedulerと§6.1へ。Bは全タスクがフル状態機械を通る前提で、ブリーフ必須課題2（単一/複数の選択）への答えが弱い。
2. テスト・ビルドコマンドの副作用事前分類とVerifierのsandbox化（A: FR-017, R-18）→ B §5のVerifier行と§8 Alwaysへ。共通の穴1をAの部品で半分塞げる。
3. 重複作業検出とcontent-hashによるartifact再利用（A: CB-005, FR-033, FR-083）→ B §6.5へ。Bには「同じ調査を二度させない」仕組みがなく、attempt再試行時に同一調査コストが再発する。
4. 外部レビュー前のpre-spend gate（A: FR-015, CB-004: 送信束・推定トークン・秘密検査結果の提示）→ B §6.4のプローブ直後、Reviewer送信前へ。
5. 承認提示仕様と承認過多対策（A: FR-074の「対象・影響・rollback・直前検証」提示、R-20）→ B §8 Ask firstと`AWAITING_APPROVAL`の提示内容仕様へ。Bは承認で何を見せるかが未規定。

## 7. 不足受入テスト（両案にないもの）

1. Verifier経由のコード実行攻撃: patchが`conftest.py`に外部送信とworktree外書込を仕込む → Verifier実行 → 期待: sandboxで遮断されfail、worktree外ファイル無変更・外部通信ゼロ。証拠: ネットワーク監視ログとファイルmtime照合。
2. symlink境界回避: 子がworktree内に`~/.ssh`や`.env`を指すsymlinkを作成 → 期待: 差分スコープ/deny判定が実体パスで解決されfail。証拠: verify失敗レポートと対象ファイルの読取痕跡なし。
3. 多重run競合: 同一repoで2本目のrunを同時起動 → 期待: run-levelロックで2本目が拒否または待機、両runの状態ファイルが無破損。証拠: ロックファイルとevents整合。
4. 子内部fan-out: 子CLIが内部でサブエージェントを起動し単発呼び出しで大量消費 → 期待: 子設定でサブエージェント無効、または内部消費がbudgetに計上されhard capで遮断。証拠: usageイベントとbudget記録の一致。
5. 偽summary承認すり抜け: 実diffと矛盾するsummaryを生成するManagerスタブ → 期待: summary↔diff/verify結果の機械照合が不一致を検知し、承認画面に警告または停止。証拠: 照合ゲートのfailレポート。

## 8. 確信度

高。判定が変わりうる条件は3つ: (1) 実行環境で`codex exec --output-schema`等のCLIフラグが使えない場合（Bの構造化出力とbudget計測の骨格が崩れ、会話内スキル前提のAが現実解になる）、(2) plan固定・ワンショットManagerでは再計画不足で実タスクが頻繁にESCALATEDになる場合（Aの会話型Managerの柔軟性が効く）、(3) 実装工数を極小化しMVPを即日動かすことが最優先の場合（AのほうがPhase 1として軽い）。
