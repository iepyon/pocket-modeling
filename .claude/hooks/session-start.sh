#!/bin/bash
# SessionStart hook — スキルの Python 依存（pyyaml / jsonschema / ruamel.yaml）と Quint CLI を入れる。
# クラウドの新しいセッションには入っていないため、最初の dmlctl init が exit 3 で落ちるのを防ぐ。
# すでに入っていれば何もしない（冪等）。失敗してもセッションは止めない。
set -u
cd "${CLAUDE_PROJECT_DIR:-$(pwd)}" || exit 0

# Quint CLI（`dmlctl quint --run` のモデル検査用）。無ければ npm で入れる。
# 数十秒かかるのでバックグラウンドで走らせ、セッション開始を待たせない。
# 入らなくてもモデル検査が使えないだけなので、失敗は無視する。
if ! command -v quint >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
  (npm install -g --silent --no-fund --no-audit @informalsystems/quint >/dev/null 2>&1 &)
fi

# import せずに在否だけ見る（毎回のセッション開始を遅くしない）
if python3 -c 'import sys; from importlib.util import find_spec as f; sys.exit(0 if all(f(m) for m in ("yaml", "jsonschema", "ruamel.yaml")) else 1)' >/dev/null 2>&1; then
  exit 0
fi
REQ=".claude/skills/eventstorming-facilitator/requirements.txt"
# オフラインでもセッション開始を長く止めない
PIP_OPTS="--disable-pip-version-check --retries 1 --timeout 10"
python3 -m pip install -q $PIP_OPTS --root-user-action=ignore -r "$REQ" >/dev/null 2>&1 \
  || python3 -m pip install -q $PIP_OPTS --user -r "$REQ" >/dev/null 2>&1 \
  || echo "[session-start] 依存のインストールに失敗しました。手動で: pip install -r $REQ" >&2
exit 0
