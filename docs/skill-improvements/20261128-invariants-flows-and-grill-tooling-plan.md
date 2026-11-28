# 改善計画書: 不変条件の検査・代替の流れ・質問パネルの道具（2026-11-28）

> 元のメモ: [`20261128-invariants-flows-and-grill-tooling.md`](20261128-invariants-flows-and-grill-tooling.md)（事業計画書セッションの振り返り。改善点 20 項目）
>
> **対応状況（2026-11-28）**: ✅ 段階 1（道具の穴）と段階 2（手順の書き直し）は実施済み。段階 2 の確かめのセッション、§3 の判断 1〜4、段階 3・4 は未着手。詳細は §7

この計画書は、元のメモの改善点 #1〜#20 を「どの順で・何を変えて・何をもって完了とするか」に落としたもの。番号（#n）は元のメモ §4 の表の番号、原因（A〜E）は元のメモ §2 の分類をそのまま使う。

## 1. 目的

元のメモの結論は「チェックが緑でも、モデルが正しいとは限らない」こと。この計画では次の 3 つを満たす状態を目指す。

1. **決まりが検査できる場所に書かれる**。フェーズ 5 で書いた複数集約にまたがる決まりが `invariants[]` と `requires` に入り、フェーズ 7 で Quint にかかる（原因 B）
2. **緑の意味を取り違えない**。構造チェックで見つけられる流れの誤りは構造チェックで見つけ、見つけられないものは報告の文言で区別する（原因 A）
3. **回答の速さに手順が追いつく**。時刻・質問の列の手書きをやめ、まとめて反映する（原因 D）

進行役（AI）が書いたものの印（原因 C）は、スキーマの設計判断が要るので段階 3 で扱う。

## 2. 現状の確認（コードを読んで分かったこと）

元のメモを書いたあと、該当箇所を読んで確かめた。計画はこの事実を前提にする。

| 確かめたこと | 結果 | 関係する項目 |
|---|---|---|
| SKILL.md に `invariants[]` / `requires` / Quint の記述があるか | **無い**。フェーズ表の 5 行目は `rules` / `errs` / `transitions` / `attrs` / `params` / `queries` だけ。品質チェックの手順も構造＋意味の 2 段だけ | #1 #2 |
| playbook と検査で、集約の生成の扱いが一致しているか | **食い違っている**。`ddd-playbook.md` は「生成は `transitions[]` に書かない」（266 行目）。一方、`shared.INITIAL_STATE = "NONE"` を構造チェック（`checks.py` 1168・1500 行目）も Quint 変換も「生成前」として受け付けている | #8 #10 |
| `dml_to_quint.py` に評価器の選択があるか | **無い**。`quint run` を既定の評価器で呼ぶだけ | #4 |
| SessionStart フックが `quint` を入れるか | **入れない**（Python 依存だけ）。このコンテナには `/opt/node22/bin/quint` があるが、環境によっては無い | #4 |
| `dmlctl rename` が `decisions[].affects` を置換するか | **しない**。`refs.py` に `affects` の扱いが無い | #17 |
| `dmlctl update --merge-yaml` のリストの扱い | `_deep_merge` は dict だけ再帰し、**リストは丸ごと置き換える**。警告も出ない | #17 |
| `dmlctl init` が時刻を付けるか | **付けない**。`--session-id` と `--started-at` を呼び出し側が書く | #17 |
| `decisions[].chosen` の型 | **文字列のみ**（`dml.schema.yaml` 499 行目）。`decided_by` は `decisions[]` にだけある | #11 #12 |
| 「毎ターン commit / push」を求める Stop フック | **リポジトリの `.claude/settings.json` には無い**。クラウド環境側のフック。リポジトリから止めることはできない | #18 |
| 回答の未反映を拾う道具 | `grill_pending.py` がある。時刻の妥当性（未来・回答より前）は見ていない | #5 |
| テスト | `scripts/tests/` に `test_structural_checks.py` / `test_dml_to_quint.py` / `smoke_test_dmlctl.sh` などがある。新しい検査はここに足す | 全般 |

## 3. 先に決めること（設計判断）

段階 3 の作業はこの判断に依存する。段階 1・2 は判断を待たずに進められる。各項目に推奨案を書く。

### 判断 1. 集約の生成を `transitions[]` に書くか（#8・#10）

- **推奨: 書く（`from: NONE` を正式に認める）**。playbook の 266 行目を直す
- 理由: 構造チェックと Quint 変換はすでに `NONE` を受け付けており、実装は推奨案の側にそろっている。「計画書が起案された」という生成は業務の事実で、検査の都合ではない。書かないと Quint は「最初から存在する」と見なし、相談が 1 件も無い計画書まで「答え待ち」になる（元のメモ §2 原因 B）
- 直す範囲: playbook §8 の初期化規約（NG/OK 例ごと）、`hint --path=aggregates.transitions` の例、HTML の状態遷移図で `NONE` を「（生成）」と表示するか

### 判断 2. 検査の都合で足した遷移をどこに置くか（#8）

- **推奨: `transitions[]` の要素に任意キー `for_check: true` を足す**。Quint 変換は通常どおり読み、HTML の状態遷移図は点線で描くか凡例付きで分ける。構造チェックは「`for_check` の遷移に `note` が無い」を警告する
- 退けた案: 別のトップレベル（`verification:` など）に分ける案。集約の遷移が 2 か所に散り、`rename` や `refs` の対象も増える
- 1 インスタンス近似そのものを解く #9 が入れば、この印が付く遷移は減る

### 判断 3. 複数選択の回答を決定ログにどう書くか（#12）

- **推奨: `chosen` に配列を許す**（`oneOf: string | array`）。配列のときは、含まれる各 option に `adopted: true` を付ける。`decision_chosen_adopted` チェックを配列に対応させる
- 合わせて SKILL.md に「提示していない対案を `options[]` に作らない」と書く
- 退けた案: 「4 つすべて」を 1 つの option にまとめる案。どの項目が効いているのかが `affects` から追えなくなる

### 判断 4. 進行役が書いたものの印（#11）

- **推奨: `decided_by` と同じ値（`user` / `ai-default` / `ai-persona` に `ai-draft` を追加）を、`aggregates[]` / `policies[]` / `invariants[]` / `scenarios[].rules[]` にも任意キー `drafted_by` として持たせる**。省略時は従来どおり印なし（本人確認済みとは見なさない）
- `ai-decided` の view を広げ、決定だけでなく `drafted_by: ai-*` の要素も一覧する。本人が確認したら `drafted_by` を消すか `user` にする
- 退けた案: `note` に定型文を書く案。view で拾えず、文言の揺れで漏れる

## 4. 段階ごとの作業

前の段階のセッションで使ってみてから次に進む。各段階の終わりに元のメモの「対応状況」を更新する。

### 段階 1. 道具の穴をふさぐ（判断不要・テストで確かめられるもの）

| # | 作業 | 変更するファイル | 完了の条件 |
|---|---|---|---|
| 4 | `dml_to_quint.py` と `dmlctl quint` に `--backend={auto,rust,typescript}` を足す。`auto`（既定）は既定の評価器で走らせ、評価器を取得できずに失敗したら typescript でやり直し、その旨を 1 行出す | `dml_to_quint.py` / `dmlctl.py` | 評価器を取れない環境で `dmlctl quint <f> --run` が成功する。`test_dml_to_quint.py` に引数の受け渡しのテスト |
| 4 | SessionStart フックで、`quint` が無ければ `npm i -g @informalsystems/quint` を試す（失敗してもセッションは止めない。Python 依存と同じ作り） | `.claude/hooks/session-start.sh` | `quint` の無い環境で初回のセッション開始後に `quint --version` が通る。ある環境では何もしない |
| 3 | 構造チェック `alt_flow_takes_default_branch` を足す（warn）。`kind: alt` で `entry` を持つ流れを `flow_walk.py` でたどり、通ったどの分岐でもその流れ用の `next`（dict のキー）や `terminal` が無く、happy と同じ経路をたどっていれば警告 | `dml_filters/checks.py` | 元のセッションで起きた形（「条件つき承認」の流れが無条件の承認を通る）のフィクスチャで警告が出る。正しく分岐する `examples/sample.dml.yaml` では出ない |
| 3 | 構造チェック `rules_without_invariants` を足す（warn）。集約が 2 つ以上・`rules` が 1 件以上・`invariants[]` と `requires` が 0 件のとき警告。フェーズ 5 より前は黙る | `dml_filters/checks.py` | フィクスチャで警告が出る。`invariants[]` を 1 件足すと消える |
| 5 | `grill_pending.py` に、`reflected[qid].at` が現在時刻より先、または回答の `at` より前なら警告する機能を足す | `grill_pending.py` | 未来の時刻を含む progress で警告が出るテスト |
| 5 | `grill_reflect.py` を足す。`ArtifactData` で保存した out_dir と `--qid` / `--summary` / `--decisions` を受け取り、`at` を実時計で付けた `batch` の書き込み JSON を出力する。回答の `at` より前や未来になる場合は出力しない | 新規 `scripts/grill_reflect.py` | 出力をそのまま `ArtifactData batch` に渡せる。時刻の誤りで exit 1 になるテスト |
| 5 | `grill_queue.py` を足す。保存した `grill/current` に対して「1 問足す（先頭／末尾）」「qid で外す」「全員が答えて反映済みの質問を外す」を行い、`if_version` 付きの `set` の JSON を出す | 新規 `scripts/grill_queue.py` | 質問の列を手で書き直さずに補充できる。テストあり |
| 17 | `update --merge-yaml` の値にリストが含まれるとき、「このリストは丸ごと置き換わる」と警告し、`--dry-run` で置き換わる前と後を出す | `dmlctl.py` | 元の事故（決定の options が消えた D10）と同じ操作で警告が出る |
| 17 | `add --before=<key=value>` / `--after=<key=value>` で挿入位置を指定できるようにする | `dmlctl.py` | 暫定の流れの途中にシナリオを差し込めることを smoke test で確かめる |
| 17 | `init` で `--session-id` を省くと、実時計（UTC）から `eventstorming-YYYYMMDD-HHMM` を付け、`started_at` も入れる。ファイル名と ID が食い違うときは警告 | `dmlctl.py` | 引数なしの init で現在時刻の ID が付く |
| 17 | `rename` が `decisions[].affects` の `<種別>.<識別子>` 形式（`policies.<名前>` など）も置換する | `dml_filters/refs.py` | 元の取りこぼしと同じ形のテスト |
| 17 | `decision` サブコマンドを足す。`dmlctl decision <f> --id=D14 --choose=<option名>` で `chosen` と `adopted` を付け替え、前の採用案の `why` を `why_not`（「選び直しで不採用（前→後）」）に移す | `dmlctl.py` | 選び直しの手順（SKILL.md の 5 手順）の 2 が 1 コマンドになる |

規模の目安: 1 セッション。テストを先に書いて回す。

### 段階 2. 手順を書き直す（SKILL.md・playbook・quality-check）

段階 1 の道具を前提に書く。文書だけの変更だが、セッションの進み方が一番変わる段階。

| # | 作業 | 変更するファイル |
|---|---|---|
| 1 | フェーズ表の 5 行目に `invariants[]` と `requires` を足し、「フェーズ別の書き出し規律」に**決まりの振り分け**を入れる。複数集約の状態の関係 → `invariants[]` と `requires`／1 集約の中 → `transitions[]`／属性・回数・権限 → `rules`（検査されないと明記）。`rules` に他集約の状態が出てきたら `requires` を対で書く | SKILL.md / `ddd-playbook.md`（新しい §として不変条件の書き分けを置く） |
| 7 | 「Event Flow」に代替の流れの書き方を例つきで足す。`entry` は分岐の先のシナリオに置く／分岐からの続きは `brs[].next`／`terminal` はその分岐で流れが終わるときだけ。誤った例（題名の分岐を通らない alt）も並べる | SKILL.md |
| 10 | liveness の使いどころを書く。自動処理が最後まで運ぶ性質にだけ書く／人の操作を待つ性質には書かない（必ず違反になる）／保証したいなら期限と自動処理を足す。`requires` の `in` に `NONE` を書く例。判断 1 が決まったら生成の書き方もここに | SKILL.md / playbook / `hints.py` |
| 2 | フェーズ 7 の標準フローを「構造チェック → Quint（`invariants[]` があれば）→ 意味チェック」の 3 段にする。報告の定型文を決める。例:「構造チェック: エラー 0。Quint: ランダム検査 6000 通りで違反なし（保証ではない。回数の上限・属性の決まりは対象外）」。**「エラー 0 件」を進み具合の報告に単独で使わない**と書く | SKILL.md / `quality-check.md` |
| 15 | 意味チェックをフェーズの途中に分ける。用語（bc-vocabulary-consistency）はフェーズ 3 の後、流れのつながり（causal-chain-completeness）はフェーズ 4 の後、集約の目的（agg-purpose-quality）は 4.6 の後。フェーズ 7 で全観点をもう一度 | SKILL.md / `quality-check.md` |
| 6 | 「回答が届いたとき」をまとめて反映する手順に書き直す。未反映を全部拾う（`grill_pending.py`）→ 質問を補充（`grill_queue.py`）→ まとめて DML を更新 → 1 回だけ公開 → `grill_reflect.py` の出力で progress と current を `batch` で書く。**時刻は手で書かない** | SKILL.md |
| 16 | Artifact のセッションでのフェーズ完了の出力を決める。チャットに出すのは「フェーズ N 完了」と要約・ホットスポット・未確認事項の差分。流れの表と DML 抜粋は出さない（ページにある）。代わりに `session.status` と反映エリアの一言を必ず更新する。`contexts[].description` と `actions[]` の書き漏れはフェーズ 4.5 以降で `phase_drift` に警告を足す | SKILL.md / `chat-output-format.md` / `checks.py` |
| 14 | 選択肢の文言の規律を足す。1 つの選択肢に結果と手段を混ぜない（「却下として終わる（自動的に却下扱い）」のような二義を作らない）。選択式が 5 問続いたら、自由記述を促す質問を 1 問挟む | SKILL.md |
| 13 | 採用理由の扱いを決める。本人が理由を言っていないなら `why` は「回答者の選択（理由は未聴取）」にとどめ、選択肢の補足文を言い換えて理由に見せない。後から見直す決定（集約の境界・領域の位置づけ・不変条件）だけ、次の質問で理由を一言聞く | SKILL.md / `checks/decision-rationale-clarity.md` |
| 19 | ヒントの要望が別々の論点で 3 回続いたら、次の質問から選択肢に「おまかせ」を足す | SKILL.md |
| 18 | コミットの可否を聞く場所を、チャットから**パネルの最初の質問**に移す。Stop フックへの対応（1 回だけ報告して繰り返さない）はすでに書いてあるので残す。Stop フック自体は環境側の設定でリポジトリからは変えられない、と注記する | SKILL.md |

完了の条件: 段階 2 の後に短いセッション（フェーズ 1〜7、質問 20 問程度）を 1 本通し、(a) 不変条件が 1 件以上 `invariants[]` に入る、(b) Quint がフェーズ 7 で回る、(c) 反映の記録の時刻がすべて実時計どおり、(d) フェーズ完了の報告が新しい定型に沿う、を確かめる。

規模の目安: 文書の書き直しに 1 セッション、確かめのセッションにもう 1 本。

### 段階 3. スキーマを変える（判断 1〜4 が決まってから）

| # | 作業 | 変更するファイル | 前提 |
|---|---|---|---|
| 8 | 判断 1 に沿って playbook を直す。判断 2 に沿って `transitions[].for_check` をスキーマに足し、ビルダーで描き分け、`for_check` に `note` が無ければ警告 | `dml.schema.yaml` / playbook / `eventstorming_build.py` / `checks.py` / `RENDER_SPEC.md` | 判断 1・2 |
| 12 | 判断 3 に沿って `chosen` に配列を許す。`decision_chosen_adopted`・HTML の意思決定ログ・`grill/progress.decisions` の表示を配列に対応させる | `dml.schema.yaml` / `checks.py` / `eventstorming_build.py` | 判断 3 |
| 11 | 判断 4 に沿って `drafted_by` を足し、`ai-decided` view を広げる。HTML でも印を出す（例: 「AI 下書き」の小さなバッジ） | `dml.schema.yaml` / `views.py` / `eventstorming_build.py` / SKILL.md | 判断 4 |
| 9 | Quint 変換で回数の上限と複数インスタンスを扱う。候補は (a) `invariants[]` に `kind: bound`（`agg` と `via` と上限回数）を足してカウンタに変換、(b) `aggregates[].instances: 2` で集約ごとにインスタンス数を指定。どちらも状態数が増えるので、元のセッションの DML で検査時間を測ってから採否を決める | `dml_to_quint.py` / `dml.schema.yaml` | 判断 2（`for_check` の遷移が要らなくなるか） |

規模の目安: #8・#11・#12 で 1 セッション。#9 は試作と計測を含めて別の 1 セッション。

### 段階 4. コストの試し（急がない）

| # | 作業 |
|---|---|
| 20 | 意味チェック 6 観点のうち、スライスを読んで定型の観点で判定するもの（用語・決定の理由）を軽いモデルで回し、元のセッションの DML で指摘の数と中身を今のモデルと比べる。差が小さければ `quality-check.md` に観点ごとの推奨モデルを書く |

## 5. この計画でやらないこと

- **元のメモ §5（事業計画書モデルに残っている本人未確認のもの）の解消**。これはモデルの作業で、スキルの改善ではない。あのセッションを再開するときに扱う。段階 3 の #11 が入れば、未確認のものを view で一覧できるようになる
- **Stop フックの変更**。環境側の設定で、このリポジトリの外にある
- **ディメンショナル・モデリング側**。Artifact のグリル自体が未対応なので、段階 1 の grill スクリプトも EventStorming 側だけに置く
- **Apalache（`--verify`）の高速化**。深い状態はランダム検査で見る、という今の方針を変えない

## 6. 項目の対応表

元のメモの 20 項目が、どの段階に入ったかの一覧。

| 段階 | 項目 |
|---|---|
| 1. 道具の穴 | #3 #4 #5 #17 |
| 2. 手順の書き直し | #1 #2 #6 #7 #10 #13 #14 #15 #16 #18 #19 |
| 3. スキーマ | #8 #9 #11 #12 |
| 4. コストの試し | #20 |

## 7. 実施記録（2026-11-28）

### 段階 1（実施済み）

| # | 入ったもの |
|---|---|
| 3 | 構造チェック `alt_flow_takes_default_branch` / `rules_without_invariants`（どちらも warn） |
| 4 | `dml_to_quint.py` / `dmlctl quint` の `--backend={auto,rust,typescript}`（auto は rust で結果が出なければ typescript でやり直す）。SessionStart フックが `quint` を npm でバックグラウンド導入 |
| 5 | `grill_reflect.py`（反映の記録を実時計で組み立て）／`grill_queue.py`（質問の列の追加・削除・掃除と検査）／`grill_pending.py` の未来の時刻の警告と「要確認」 |
| 17 | `rename` / `refs` が `affects` の「種別.識別子」を扱う／`update --merge-yaml` のリスト置換の警告と `--dry-run` の前後表示／`add --before` `--after`／`init` の実時計の ID と時刻ずれの警告／`decision` サブコマンド |

テスト: `scripts/tests/test_invariants_flows_grill.py`（24 件）。既存のテストはすべて通過。

### 段階 2（実施済み・確かめのセッションは未実施）

| # | 入ったもの |
|---|---|
| 1 | SKILL.md のフェーズ表 5 行目と「フェーズ別の書き出し規律」に決まりの振り分け。playbook §3 に「決まりの振り分け（どこに書くか）」 |
| 2 | フェーズ表 7 行目と品質チェックの手順に Quint を必須として追加。`quality-check.md` に「報告の文言」の定型 |
| 6 | 「回答が届いたとき」を、まとめて反映 → 1 回公開 → `grill_queue.py` と `grill_reflect.py` の出力を 1 つの batch に、の手順に書き直し。選び直しは `dmlctl decision` |
| 7 | SKILL.md「Event Flow」と playbook §12 に代替の流れの書き方（分岐での選ばれ方・NG/OK の例） |
| 10 | liveness の使いどころ、`requires` の `in: [NONE]` の例（Quint で効く条件と判断 1 待ちの注記つき） |
| 13 | 採用理由を選択肢の言い換えで書かない規律。意味チェック `decision-rationale-clarity` に観点 9 を追加 |
| 14 | 選択肢の文言の規律、選択式が 5 問続いたら自由記述の質問を挟む |
| 15 | `quality-check.md` に「いつ、どの観点を回すか」の表 |
| 16 | Artifact のセッションでのフェーズ完了の出力（`advance --status`・反映エリア・チャットの要約）。構造チェック `phase_outputs_missing`（領域の説明の空・フェーズ 7 で actions 0 件） |
| 18 | コミットの可否の質問はパネルに出す。Stop フックは環境側の設定と注記 |
| 19 | 別々の論点でヒントの要望が 3 回続いたら「おまかせ」を選択肢に足す |
| 12（暫定） | 複数選択の記録の指針（提示していない対案を作らない。組を 1 つの option にする）。`chosen` の配列化は判断 3 待ち |

### 実施中に分かったこと

- **新しい構造チェック `alt_flow_takes_default_branch` が、既存のモデル 2 件で当たった**。どちらも本物の誤り。
  - `eventstorming-20260905-1311`（ミートアップ）の `alt-waitlist`: entry が申込のシナリオで、既定の分岐（申込確定）を通る。題名の「キャンセル待ち」が図に出ない
  - `eventstorming-20261128-0900`（事業計画書）の `alt-returned`: entry がメインの道の上で、「差し戻し」の分岐が図に出ない
  - モデルの直し方は業務の判断を含むので、今回は直していない
- **差し戻し（ループ）の流れを DML で書けない**。分岐の先がメインの流れの途中に戻る場合、「この流れではこの分岐を選んで先へ続く」を書く手段が無い（`pick_active_branch` は `terminal` か既定の分岐しか選ばない）。playbook §12 に限界として書いた。**判断 5 として追加**: `brs[].flows: [<narrative id>]`（この流れではこの分岐を選ぶ）のようなキーを足すか
- `eventstorming-20260529-1254` は以前から `check --all` が error を返す（今回の変更とは無関係）

### 残り（判断が要るもの・本人が要るもの）

- §3 の判断 1〜4 と、上の判断 5
- 段階 2 の確かめのセッション（本人の回答が要る）
- 段階 3（#8 #9 #11 #12）— 判断待ち
- 段階 4（#20）— 意味チェックの指摘の良し悪しを比べる評価が要る
