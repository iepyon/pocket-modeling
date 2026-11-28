#!/usr/bin/env python3
"""Artifact グリルの「未反映の回答」を一覧する。

使い方:
    1. ArtifactData で answers と grill/progress を、同じ out_dir に保存する
         ArtifactData list  collection=answers           out_dir=<dir>
         ArtifactData get   collection=grill doc_id=progress out_dir=<dir>
       → <dir>/answers/<qid>--<uid>.json と <dir>/grill/progress.json ができる
    2. python3 scripts/grill_pending.py <dir>

未反映とは次のどちらか:
    - 回答の qid が grill/progress の reflected に無い
    - 回答の at が reflected[qid].at より新しい（パネルの「選び直す」で届いた回答）

終了コード: 0 = 未反映なし（何も出さない）／ 1 = 未反映あり（一覧を出す）／ 2 = 入力の誤り

ArtifactData が保存する JSON が「ドキュメント本体」か「{data|fields: 本体, version, …}」の
どちらの形でも読めるようにしてある。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


def _unwrap(doc):
    """保存形式の差（本体そのまま／data・fields で包まれている）を吸収して本体を返す。"""
    if not isinstance(doc, dict):
        return {}
    for key in ("data", "fields", "document", "doc"):
        inner = doc.get(key)
        if isinstance(inner, dict) and ("qid" in inner or "reflected" in inner or "questions" in inner):
            return inner
    return doc


def _parse_at(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        from datetime import timezone

        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_answers(root: Path) -> list[dict]:
    folder = root / "answers"
    if not folder.is_dir():
        return []
    out = []
    for f in sorted(folder.glob("*.json")):
        try:
            doc = _unwrap(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as e:
            print(f"⚠ {f.name} を読めません: {e}", file=sys.stderr)
            continue
        if not doc.get("qid"):
            # doc_id（<qid>--<uid>）から qid を補う
            doc = {**doc, "qid": f.stem.split("--", 1)[0]}
        doc.setdefault("_doc_id", f.stem)
        out.append(doc)
    return out


def load_reflected(root: Path) -> dict:
    for cand in (root / "grill" / "progress.json", root / "progress.json"):
        if cand.is_file():
            doc = _unwrap(json.loads(cand.read_text(encoding="utf-8")))
            reflected = doc.get("reflected")
            return reflected if isinstance(reflected, dict) else {}
    return {}


def pending(answers: list[dict], reflected: dict) -> list[dict]:
    rows = []
    for a in answers:
        qid = a.get("qid")
        rec = reflected.get(qid)
        if not isinstance(rec, dict):
            rows.append({**a, "_reason": "未反映"})
            continue
        a_at, r_at = _parse_at(a.get("at")), _parse_at(rec.get("at"))
        if a_at and r_at and a_at > r_at:
            reason = "選び直し" if a.get("rev") or a.get("prev") else "反映後に更新"
            rows.append({**a, "_reason": reason})
    rows.sort(key=lambda r: (_parse_at(r.get("at")) is None, r.get("at") or ""))
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Artifact グリルの未反映の回答を一覧する")
    ap.add_argument("out_dir", help="ArtifactData の out_dir（answers/ と grill/progress.json を含む）")
    ap.add_argument("--json", action="store_true", help="JSON で出す")
    args = ap.parse_args(argv)
    root = Path(args.out_dir)
    if not root.is_dir():
        print(f"❌ ディレクトリがありません: {root}", file=sys.stderr)
        return 2
    rows = pending(load_answers(root), load_reflected(root))
    if not rows:
        return 0
    if args.json:
        print(json.dumps(
            [{"qid": r.get("qid"), "by": r.get("by"), "at": r.get("at"), "reason": r["_reason"],
              "choices": r.get("choices") or ([r["choice"]] if r.get("choice") else []),
              "text": r.get("text") or "", "hint": bool(r.get("hint")), "prev": r.get("prev")}
             for r in rows],
            ensure_ascii=False, indent=2,
        ))
        return 1
    print(f"未反映の回答 {len(rows)} 件:")
    for r in rows:
        choices = r.get("choices") or ([r["choice"]] if r.get("choice") else [])
        parts = [f"- {r.get('qid')}（{r['_reason']}・{r.get('at') or '時刻なし'}・{r.get('by') or '?'}）"]
        if r.get("hint"):
            parts.append("？（ヒント要求）")
        if choices:
            parts.append("選択: " + " / ".join(map(str, choices)))
        if r.get("text"):
            parts.append("記述: " + str(r["text"]).replace("\n", " ")[:120])
        prev = r.get("prev")
        if isinstance(prev, dict) and prev.get("choices"):
            parts.append("前の回答: " + " / ".join(map(str, prev["choices"])))
        print("  ".join(parts))
    return 1


if __name__ == "__main__":
    sys.exit(main())
