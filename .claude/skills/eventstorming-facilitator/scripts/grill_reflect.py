#!/usr/bin/env python3
"""Artifact グリルの「反映の記録」（grill/progress.reflected）の書き込みを組み立てる。

反映の記録の時刻を手で書くと、実際より先の時刻を書く誤りが起きる（未来の時刻があると、
その後の選び直しが grill_pending.py で拾えなくなる）。このスクリプトは時刻を実時計から付け、
回答より前・現在より先になる記録を作らない。

使い方:
    1. ArtifactData で answers と grill/progress を同じ out_dir に保存する（grill_pending.py と同じ）
    2. python3 scripts/grill_reflect.py <out_dir> --qid q-…-04 --summary "「…」→ …" [--decisions D7,D8]
       複数の qid を同じ summary で記録するなら --qid を繰り返す。qid ごとに summary を変えるなら
       --entry 'q-…-04=summary' を繰り返す（--decisions は --entry には付かない）
    3. 出力された JSON の writes を ArtifactData の batch にそのまま渡す
       質問の列の書き換え（grill_queue.py の出力）も同じ batch に入れるなら --queue-op <file>

出力: {"writes": [ {op: "update", collection: "grill", doc_id: "progress", data: {...}, if_version: N}, … ]}

終了コード: 0 = 出力した／ 1 = 時刻や qid の誤りで出力しなかった／ 2 = 入力の誤り
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from grill_pending import _parse_at, load_answers, load_grill_doc


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_writes(root: Path, entries: list[tuple[str, str]], decisions: list[str],
                 now: datetime | None = None, queue_op: dict | None = None,
                 allow_unanswered: bool = False) -> tuple[dict | None, list[str]]:
    """(batch 用の dict, エラー一覧) を返す。エラーがあれば dict は None。"""
    now = now or _now()
    errors: list[str] = []
    answers = load_answers(root)
    latest: dict[str, datetime] = {}
    for a in answers:
        at = _parse_at(a.get("at"))
        qid = a.get("qid")
        if qid and at and (qid not in latest or at > latest[qid]):
            latest[qid] = at
    answered = {a.get("qid") for a in answers}
    reflected: dict[str, dict] = {}
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for qid, summary in entries:
        if not summary.strip():
            errors.append(f"{qid}: summary が空です（モデルを変えなかったときも、そう書きます）")
        if qid not in answered and not allow_unanswered:
            errors.append(f"{qid}: 保存した answers にこの qid の回答がありません（qid の打ち間違い？）")
        if qid in latest and latest[qid] > now:
            errors.append(
                f"{qid}: 回答の時刻（{latest[qid]:%H:%M:%S}）が実時計（{now:%H:%M:%S}）より先です。"
                "answers を保存し直してから実行してください"
            )
        rec: dict = {"at": stamp, "summary": summary}
        if decisions:
            rec["decisions"] = decisions
        reflected[qid] = rec
    if errors:
        return None, errors
    writes = []
    if queue_op:
        writes.append(queue_op)
    _progress, version = load_grill_doc(root, "progress")
    op = {"op": "update", "collection": "grill", "doc_id": "progress", "data": {"reflected": reflected}}
    if version is not None:
        op["if_version"] = version
    writes.append(op)
    return {"writes": writes}, []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="grill/progress の反映の記録を、実時計の時刻で組み立てる")
    ap.add_argument("out_dir", help="ArtifactData の out_dir（answers/ と grill/progress.json）")
    ap.add_argument("--qid", action="append", default=[], help="反映した qid（繰り返し可。--summary と組）")
    ap.add_argument("--summary", help="--qid に共通の一言（非エンジニアが読む。DDD 用語・英語識別子を避ける）")
    ap.add_argument("--entry", action="append", default=[], help="qid=summary（qid ごとに一言を変えるとき。繰り返し可）")
    ap.add_argument("--decisions", help="この回答が元になった decisions[].id（カンマ区切り。--qid に付く）")
    ap.add_argument("--queue-op", help="grill_queue.py の出力ファイル。同じ batch の先頭に入れる")
    ap.add_argument("--allow-unanswered", action="store_true", help="回答の無い qid も記録する（チャットで答えた場合など）")
    args = ap.parse_args(argv)

    root = Path(args.out_dir)
    if not root.is_dir():
        print(f"❌ ディレクトリがありません: {root}", file=sys.stderr)
        return 2
    entries: list[tuple[str, str]] = []
    if args.qid:
        if not args.summary:
            print("❌ --qid には --summary が要ります", file=sys.stderr)
            return 2
        entries += [(q, args.summary) for q in args.qid]
    for e in args.entry:
        qid, sep, summary = e.partition("=")
        if not sep:
            print(f"❌ --entry は qid=summary の形です: {e}", file=sys.stderr)
            return 2
        entries.append((qid.strip(), summary.strip()))
    if not entries:
        print("❌ --qid か --entry が要ります", file=sys.stderr)
        return 2
    queue_op = None
    if args.queue_op:
        queue_op = json.loads(Path(args.queue_op).read_text(encoding="utf-8"))
        if isinstance(queue_op, dict) and "writes" in queue_op:
            queue_op = queue_op["writes"][0]
    decisions = [d.strip() for d in (args.decisions or "").split(",") if d.strip()]
    batch, errors = build_writes(root, entries, decisions, queue_op=queue_op,
                                 allow_unanswered=args.allow_unanswered)
    if errors:
        for e in errors:
            print(f"❌ {e}", file=sys.stderr)
        return 1
    if not any("if_version" in w for w in batch["writes"]):
        print("⚠ 保存した grill の版（version）が見つかりません。ArtifactData で get し直すと if_version が付きます", file=sys.stderr)
    print(json.dumps(batch, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
