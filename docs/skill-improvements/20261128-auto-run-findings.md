# 最後まで自動で進めたときに見つかった課題（2026-11-28）

> **対応状況（2026-11-28）**
> - ✅ A-1: `decisions[].decided_by`（`user` / `ai-default` / `ai-persona`）をスキーマに足し、`dmlctl view --view=ai-decided` で本人未確認の決定を一覧できる。HTML の決定カードに「AI が選択（要確認）」の印。自動実行モードの手順を SKILL.md に書いた
> - ✅ A-2: 理由の分からない `why_not` は省略してよい（playbook §11・SKILL.md）。coverage view は不採用案の `why_not` を残欠に数えない。意味チェック `decision-rationale-clarity` が「同じ文言の `why_not` が 3 件以上」を指摘する。パネルの「選ばなかった理由」欄は見送った（質問が重くなるため。必要な論点だけ次の質問で聞く）
> - ✅ A-3: 意味チェック `decision-rationale-clarity` が、決着済みの論点への「確認中」が残った `note` を指摘する。質問を閉じるときに一覧する専用コマンドは見送った
> - ✅ B: 構造チェックを 5 本足した（すべて warn）。`alt_narrative_entry` / `unreachable_scenario` / `next_bypasses_policy` / `policy_scenario_overlap` / `phase_drift`。なお「POLICY の CMD へ `next` でつなぐ」こと自体は、ビルダーがフローを描くのに使う正しい書き方だった（前の evt → POLICY → `next` のシナリオ）。問題は「前のシナリオが POLICY の起動イベントを発行していないのに直結する」ことなので、`next_bypasses_policy` はそれを見る。運用モードと不変条件の区別、POLICY とシナリオの書き分けは playbook §6・§8 に足した。サブエージェントの完了通知を待つ手順は quality-check.md に足した
> - ✅ C-1: `--value` が YAML として読めないとき、スタックトレースではなく `--value-file` を案内する
> - ✅ C-2: SKILL.md に `--dry-run && 本書き込み` の書き方を載せた。`dmlctl batch` は見送った（この書き方と C-1 でほぼ防げるため）
> - ✅ C-3: hint に項目の意味と正しい例（`meaning` / `correct_example`）を出す。`next` と `terminal` の排他エラー、`params` に文字列を渡したときのエラーを分かりやすい文言にした
> - ✅ C-4: `dmlctl derive-agg-events <file> [--agg=<Name>] [--dry-run]`
> - ✅ C-5: 集約を足すときに `lang.states` も同時登録する、と SKILL.md に書いた
> - ✅ D-1: 冒頭に 1 回だけ自動コミットの可否を聞く、権限が通らないときの報告は 1 回だけ、を SKILL.md に書いた。権限ルールの書き方は README のセットアップ 5
> - ✅ D-2: `grill/current` と `grill/progress` を `ArtifactData` の `batch` で 1 回で書く手順（例つき）
> - △ D-3: 質問を出す前に `lang` のラベルと見比べる手順だけ足した。同じ読みの別の字を機械で見つける点検は見送った（精度が出ないため）
> - ✅ D-4: `phase_drift` が、中身から見たフェーズと `session.phase` のずれを警告する

コーヒーショップのセッション（`eventstorming-20261128-1105`）で、オーナーから「ここからは自動で最後までやりきって、課題を見つけたら改善メモに」と指示があった。
選択は、「昔ながらの手作業好きかつメカ好きの顧客」の目線で AI が行った。その実行中に見つけた課題をまとめる。

実行の結果: 集約 5 つ（焙煎ロット、生豆ロット、定期便、注文、客の好み）、リードモデル 3 つ、ポリシー 6 つ、決定 39 件。構造チェック 26 観点は指摘 0 件。意味チェック（6 観点）では 15 件の指摘が出て、直せるものは直した。

各課題に、確認できた事実（○）と、提案を付ける。

## A. 決定の記録・AI が選ぶときの扱い（優先度 高）

### A-1. 「AI が本人の代わりに選ぶ」モードの正式な扱いがない

- ○ 今回、D35〜D39 は AI が選んだ。SKILL.md にあるのは、質問 1 問ごとの「おまかせ」シグナルだけで、「顧客像に沿って最後まで選ぶ」という指示を扱う手順がない。
- 即席でやったこと: 回答データ（`answers`）には偽の回答を書かない。決定の `note` に「AI が選んだ。本人の選択ではない」と書く。進捗の `summary` の先頭に【AIが選択】を付ける。パネルで確認の質問を出す。次のアクション A1 を足す。
- 提案:
  - `decisions[]` に `decided_by`（`user` / `ai-default` / `ai-persona`）を足す。`note` の自由記述に頼らない。
  - `dmlctl view --view=ai-decided` で、本人未確認の決定を一覧できるようにする。
  - パネルは、`ai-persona` の決定に「AI が選択（要確認）」の印を出す。
  - 自動実行を指示されたときの手順を SKILL.md に書く。(1) `answers` に書かない (2) `decided_by` を付ける (3) 最後に確認の質問を 1 本出す (4) 次のアクションに「本人確認」を足す。

### A-2. `why` の多くが「AI が補った仮置き」で、`why_not` が定型文になる

- ○ 意味チェックの指摘 15 でも、同じことが挙がった。回答者が選ばなかった案の `why_not` は、ほぼ「回答者は選ばなかった（理由は未確認）」になる（30 件以上）。
- ○ 意味チェックは、`why_not` の文言が案ごとに似すぎている（「マスターの手が止まる」が 7 件の決定に出る）ことも指摘した。AI が補う理由は、型にはまりやすい。
- 提案:
  - 選ばれなかった案の `why_not` は、省略してよい（`unchosen: true` など）ようにする。定型文を書かせない。
  - パネルに、任意の「選ばなかった理由」欄を足す。あれば、それを `why_not` に使う。
  - 意味チェック（`decision-rationale-clarity`）に、「同じ文言の `why_not` が 3 件以上」の指摘を足す。

### A-3. 決着したあとも、古い `note` が残る

- ○ 意味チェックの指摘 14: D17・D25・D27 の `note` に「Q○で確認中」が残っていた。後の決定（D30、D27、D28）で決着済みだった。
- 提案: 質問（`questions[]`）を閉じるとき、その質問を `note` で参照している決定を `dmlctl refs` のように一覧し、更新を促す。

## B. 構造チェックが見逃し、意味チェックで初めて出たもの（優先度 高）

○ 構造チェック 26 観点が全部 clean のとき、意味チェックの指摘は 15 件あった。構造チェックに足せるものがある。

| 見逃し | 事実 | 提案する構造チェック |
|---|---|---|
| 代替の narrative に `entry` がない | `alt-bean-shortage` と `alt-system-down` が `entry` なし。flow が展開されないまま、`narrative_entry_consistency` は通っていた | `kind: alt` で `entry` が無いものを警告にする |
| どの narrative からも辿れないシナリオ | 定期便・量り売り・仕入れ不一致のシナリオが孤立していた | `unreachable_scenario`: どの `entry` からも `next` / `brs` / `pol` で辿れないシナリオを警告にする |
| POLICY 起動のはずが、`next` で直結 | 入荷の `next` が焙煎に直結し、「在庫が減ったら焙煎」（D9）と食い違っていた | 同じ CMD が `policies[].cmd` にあるのに、別のシナリオの `next` から直接呼ばれているものを警告にする |
| 同じ CMD を、ポリシーとシナリオの両方で宣言する | `GuideAlternativePayment`、`NotifyCustomerOnOutage` を、ポリシーとシナリオの両方に書いた | 警告にする（どちらかに寄せる指針を playbook に） |
| 全数試飲のような「運用モード」を持つ場所 | `RoastBatch` の `constraints` に運用モードを書いていた | playbook に「集約の不変条件と運用モードを分ける」を足す |

- ○ サブエージェントは、`run_in_background: false` を指定しても、非同期で起動した。完了の通知を待つ書き方が必要。`references/quality-check.md` の Agent 起動の手順に、「通知を待ってから、次に進む」を足す。

## C. dmlctl・スキーマ（優先度 中）

### C-1. 長い散文を `--value` に渡すと、Python のスタックトレースが出る

- ○ `--value='**境界の理由**: …'`（先頭が `**`）は、YAML のアンカー記号と誤読され、`yaml.scanner.ScannerError` のスタックトレースで落ちる。終了コードは 1。
- SKILL.md には「長文は `--value-file`」とあるが、`contexts[].description` の例（「`**強調**` は HTML 化で解釈される」）と結び付いていない。
- 提案: dmlctl が YAML のエラーを捕まえ、「`--value-file` を使ってください（生テキストとして扱います）」と案内する。SKILL.md の `description` の項に `--value-file` を明記する。

### C-2. dry-run の失敗が、本書き込みを止めない

- ○ dry-run は、スキーマ違反のとき終了コード 1 で終わる（確認済み）。しかし、SKILL.md の「`hint` → `--dry-run` を通してから本書き込み」には、**失敗したら止める**書き方の例がない。
- ○ 今回、集約の追加で dry-run が違反（`events[].params` に文字列を渡した）を出したのに、そのまま本書き込みまで進め、DML が一時的に schema 違反になった。後続のコマンドは `--no-postprocess` で連なっていたため、すぐには気づけなかった。
- 提案:
  - SKILL.md に、止める書き方の例を載せる: `d add … --dry-run && d add …`。
  - `dmlctl batch <file> <script>` のような、「すべて成功したときだけ書き込む」実行を足す。

### C-3. `hint` の説明が足りない

- ○ `transitions[].via`: hint は「`via` 必須」とだけ言う。**コマンド名（`scenarios[].cmd`）を書く**ことは書かれておらず、イベント名を入れて `dangling_cmd` エラーになった。
- ○ `events[].params`: hint は `array<attribute{name, type, …}>` と書くが、文字列の配列（`[roastBatchId]`）を渡しやすい。エラーは「'roastBatchId' is not of type 'object'」。スキーマが文字列の省略形を受けるか、hint の例に正しい形を載せるとよい。
- ○ `brs[]` の `next` と `terminal` は排他だが、エラーが「`{'required': ['next', 'terminal']}` で valid になってはいけない」という分かりにくい文言になる。
- 提案: hint に、各項目の意味と、正しい例を 1 つずつ足す。排他のエラーは「`next` と `terminal` は同時に指定できません」に直す。

### C-4. 集約の `events` を、手で全部数える必要がある

- ○ `scenario_agg_consistency` が、シナリオの `evt` が集約の `events` に無い、と 5 件指摘した（定期便 4、注文 1）。手作業で足した。
- 提案: `dmlctl derive-agg-events <file> --agg=<name>` で、その集約に紐づくシナリオの `evt` から、`events[]` の不足分を足す（`--dry-run` あり）。

### C-5. 状態名が BC をまたいで重なる

- ○ `CANCELLED` が注文と定期便で重なり、`cross_bc_state_name_collision` が警告した。`lang.states` で解決した。
- 提案: 集約を足すときの手順に、「`lang.states` も同時に登録」を足す（`lang.aggs` / `lang.pols` と同じ扱い）。

## D. 運用・環境（優先度 中）

### D-1. 環境のフックと、スキルの git 方針が衝突する

- ○ 環境の Stop フックは、毎ターン終わりに「未追跡ファイルをコミットして push せよ」と求めた。スキルは「ユーザーが明示的に頼んだとき以外、git 操作をしない」と定めている。
- ○ コミットと push は、権限チェック（自動モード）で拒否された。私は同じ操作を繰り返さず、毎ターン「指示があれば実行する」と報告した。20 回以上、同じ報告を繰り返すことになった。
- 影響: セッションの DML と改善メモは、未追跡のまま。コンテナが破棄されると失われる。
- 提案:
  - SKILL.md に「環境のフックが git 操作を求め、権限が通らないときの扱い」を書く: 1 回だけ報告し、以降は短く触れる。
  - セッションの冒頭で、「自動でコミットしてよいか」をオーナーに 1 回だけ聞く手順を足す。
  - プロジェクトの設定で、`docs/eventstorming/` と `docs/skill-improvements/` への git 操作を許可するルールの作り方を、README に載せる。

### D-2. 質問の列と進捗のバージョン管理で、書き込みが失敗した

- ○ `grill/current` と `grill/progress` は別々のドキュメントで、書き込みのたびに `if_version` が要る。取り違えて 1 回、`version_mismatch` で失敗した。
- 1 回の回答ラウンドで、`ArtifactData` を 3〜4 回呼ぶ（質問の列、進捗、回答の確認、再公開）。
- 提案: `ArtifactData` の `batch` で、列と進捗を 1 回で書く手順を SKILL.md に書く。または、`scripts/grill_sync.py` で、バージョンを読んで 2 つを書く。

### D-3. AI が書いた質問文の誤字

- ○ 「淹れる」を「添れる」と、別の字で書いた（2 回）。修正のつもりで「淩」と、別の字をまた入れた。
- 提案: 質問を公開する前に、DML の `lang` のラベルと照合し、同じ読みで別の字になっているものを警告する簡単な点検を足す。

### D-4. セッションの進行状況（`session.phase`）が更新されない

- ○ `session.phase` は、オーナーが指摘するまで `'1'` のままで、`dmlctl advance` は手動だった。実際はフェーズ 4 の終わりまで進んでいた。
- 提案: `dmlctl check --all` に、「内容から見たフェーズ」と「`session.phase`」がずれていたら警告する観点を足す（例: 集約があるのにフェーズが 4.6 未満）。

## E. この自動実行そのものの注意点

- 決定 D35〜D39 は、AI が選んだ。前提を左右するので、本人確認を最優先にする（次のアクション A1、パネルの確認の質問 q-46）。
- 数字（放棄とみなす時間）は、AI が決めない方針にして、未確認事項 Q11 に残した。
- このメモの「確認済み」は、このセッションで実際に再現・実行して確かめたこと。再現できなかったものは書いていない。

## 関連

- 同日の別メモ: `20261128-artifact-first-publish.md`、`20261128-japanese-labels-missing.md`、`20261128-grill-panel-reselect-answers.md`。
- 対象のセッション: `docs/eventstorming/eventstorming-20261128-1105.dml.yaml`。
