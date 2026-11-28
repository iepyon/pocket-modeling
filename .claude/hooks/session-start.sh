#!/bin/bash
# SessionStart hook — スキルの Python 依存（pyyaml / jsonschema / ruamel.yaml）を入れる。
# クラウドの新しいセッションには入っていないため、最初の dmlctl init が exit 3 で落ちるのを防ぐ。
# すでに入っていれば何もしない（冪等）。失敗してもセッションは止めない。
set -u
cd "${CLAUDE_PROJECT_DIR:-$(pwd)}" || exit 0
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
