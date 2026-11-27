# agg-purpose-quality

AGG の `purpose` が **単一責任で 30 字以上の業務言語** で書かれているかを LLM で評価する観点。

> 根拠となるルール（AGG フィールドの役割・purpose の書き方・単一責任・所有 BC 1 つ）の正典は
> [`../ddd-playbook.md`](../ddd-playbook.md) §8 集約。

## 抽出条件

```bash
python3 scripts/dmlctl.py view <session>.dml.yaml --view=agg-detail [--name <AggName>]
```

`--name` 省略時は全 AGG を返す。1 件ずつ評価する。

## LLM へのプロンプト

```
あなたは DDD の戦術的設計レビュアーです。以下の AGG の purpose / background / constraints を
読み、単一責任の明確化として十分な記述か評価してください。

評価観点:
1. **単一責任の言語化** になっているか（複数の責務が混在していないか）
2. **業務語彙** で書かれているか（実装詳細・データモデル詳細ではないか）
3. **background が WHY** を答えているか（「なぜ今この AGG を切り出すか」が伝わるか）
4. **constraints が業務 / 法令 / プラットフォームの制約** であり、技術スタック制約に偏っていないか

入力 (YAML):
{{agg_detail_yaml}}

出力フォーマット:
[
  {
    "agg": "<AggName>",
    "verdict": "ok" | "needs-revision" | "critical",
    "issues": [
      { "field": "purpose"|"background"|"constraints", "issue": "<指摘>", "suggestion": "<改善案>" }
    ]
  }
]
```

## 期待される所見

- ✅ 合格: `purpose: 旧機種の引き受けから売買成立まで状態を所有し、各遷移の根拠を法令準拠で明示する`
- ❌ 不合格例:
  - `purpose: 下取を扱う`（短すぎる・単一責任が見えない）
  - `purpose: テーブル trade_in を CRUD する`（実装詳細）
  - `background:` 空 — WHY が不明
  - `constraints: PostgreSQL を使う`（業務制約ではない）

## 連携する構造チェック

- `agg_purpose_minlength` — purpose の 30 字以上は構造チェックに降格済み（LLM では見ない）。先に走らせて
  欠落・字数不足を解消しておき、LLM は単一責任・業務語彙・WHY の質的評価（観点 1〜4）に集中する
