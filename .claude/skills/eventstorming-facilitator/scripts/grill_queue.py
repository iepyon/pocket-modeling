#!/usr/bin/env python3
"""Artifact グリルの質問の列（grill/current）を書き換える書き込みを組み立てる。

質問の列を毎回丸ごと手で書き直すと、qid の重複・選択肢の並べ替え（記号のずれ）・
回答済みの質問の取り残しが起きる。このスクリプトは保存した grill/current に操作を当て、
ArtifactData の batch にそのまま渡せる set 操作を出す。

使い方:
    1. ArtifactData で grill/current（と、--prune を使うなら answers と grill/progress）を
       同じ out_dir に保存する
    2. python3 scripts/grill_queue.py <out_dir> [--prune] [--remove QID]… \
           [--front '<質問 JSON>']… [--add '<質問 JSON>']… [--done '<締めの一言>']
       質問 JSON は {"qid", "phase", "text", "options": [{"label", "desc"}], …}。
       長いときは @<file> でファイルから読む（--add @q.json。中身は 1 問か質問の配列）
    3. 出力の writes を ArtifactData の batch に渡す。反映の記録と 1 回で書くなら、
       出力をファイルに保存して grill_reflect.py --queue-op <file> に渡す

操作の順: --prune → --remove → --front（先頭へ。書いた順に並ぶ）→ --add（末尾へ）
--prune: 反映済み（grill/progress.reflected にある）で回答もある質問を列から外す

検査（違反があれば出力しない）: qid の重複・すでに回答や反映の記録がある qid の再利用
（同じ qid は回答済み扱いで表示されない）・options が 2〜4 個でない・label の先頭に
「a.」のような記号を書いている（画面が自動で振る）

終了コード: 0 = 出力した／ 1 = 検査に通らず出力しなかった／ 2 = 入力の誤り
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from grill_pending import load_answers, load_grill_doc, load_reflected

_LABEL_MARK = re.compile(r"^\s*[a-zA-Z][.)．）]\s")


def _load_questions(spec: str) -> list[dict]:
    text = Path(spec[1:]).read_text(encoding="utf-8") if spec.startswith("@") else spec
    val = json.loads(text)
    return val if isinstance(val, list) else [val]


def check_question(q: dict, used: set[str]) -> list[str]:
    errs = []
    qid = q.get("qid")
    if not qid:
        return [f"qid がありません: {q.get('text', '')[:30]}"]
    if qid in used:
        errs.append(f"{qid}: すでに使われた qid です（出し直すときも新しい qid にする）")
    if not q.get("text"):
        errs.append(f"{qid}: text がありません")
    opts = q.get("options") or []
    if not 2 <= len(opts) <= 4:
        errs.append(f"{qid}: options は 2〜4 個にします（{len(opts)} 個）")
    for o in opts:
        label = o.get("label") if isinstance(o, dict) else None
        if not label:
            errs.append(f"{qid}: label の無い選択肢があります")
        elif _LABEL_MARK.match(label):
            errs.append(f"{qid}: label の先頭に記号を書かない（画面が a. b. を振る）: {label}")
    return errs


def build_write(root: Path, *, prune: bool, remove: list[str], front: list[dict], add: list[dict],
                done: str | None) -> tuple[dict | None, list[str], list[str]]:
    """(set 操作, エラー一覧, 情報) を返す。"""
    current, version = load_grill_doc(root, "current")
    questions = [q for q in current.get("questions") or [] if isinstance(q, dict)]
    info: list[str] = []
    answers = load_answers(root)
    answered = {a.get("qid") for a in answers}
    reflected = load_reflected(root)
    if prune:
        keep = [q for q in questions if not (q.get("qid") in reflected and q.get("qid") in answered)]
        gone = [q.get("qid") for q in questions if q not in keep]
        if gone:
            info.append("反映済みで外した: " + ", ".join(gone))
        questions = keep
    errors: list[str] = []
    for qid in remove:
        if not any(q.get("qid") == qid for q in questions):
            errors.append(f"{qid}: 列にありません（外せません）")
        questions = [q for q in questions if q.get("qid") != qid]
    used = {q.get("qid") for q in questions} | answered | set(reflected)
    for q in front + add:
        errors += check_question(q, used)
        used.add(q.get("qid"))
    if errors:
        return None, errors, info
    questions = list(front) + questions + list(add)
    data: dict = {"questions": questions}
    if done is not None:
        if questions:
            return None, ["--done は質問の列が空のときだけ使えます"], info
        data.update({"done": True, "note": done})
    op = {"op": "set", "collection": "grill", "doc_id": "current", "data": data}
    if version is not None:
        op["if_version"] = version
    info.append(f"列: {len(questions)} 問（先頭: {questions[0].get('qid') if questions else 'なし'}）")
    return {"writes": [op]}, [], info


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="grill/current（質問の列）の書き換えを組み立てる")
    ap.add_argument("out_dir")
    ap.add_argument("--prune", action="store_true", help="反映済みで回答もある質問を外す")
    ap.add_argument("--remove", action="append", default=[], help="qid を外す（繰り返し可）")
    ap.add_argument("--front", action="append", default=[], help="先頭に足す質問 JSON（または @file）")
    ap.add_argument("--add", action="append", default=[], help="末尾に足す質問 JSON（または @file）")
    ap.add_argument("--done", help="質問が尽きたときの締めの一言（列が空のときだけ）")
    args = ap.parse_args(argv)
    root = Path(args.out_dir)
    if not root.is_dir():
        print(f"❌ ディレクトリがありません: {root}", file=sys.stderr)
        return 2
    try:
        front = [q for s in args.front for q in _load_questions(s)]
        add = [q for s in args.add for q in _load_questions(s)]
    except (OSError, json.JSONDecodeError) as e:
        print(f"❌ 質問 JSON を読めません: {e}", file=sys.stderr)
        return 2
    write, errors, info = build_write(root, prune=args.prune, remove=args.remove, front=front, add=add,
                                      done=args.done)
    for line in info:
        print(f"ℹ {line}", file=sys.stderr)
    if errors:
        for e in errors:
            print(f"❌ {e}", file=sys.stderr)
        return 1
    if "if_version" not in write["writes"][0]:
        print("⚠ 保存した grill/current の版（version）が見つかりません。ArtifactData で get し直すと if_version が付きます", file=sys.stderr)
    print(json.dumps(write, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
