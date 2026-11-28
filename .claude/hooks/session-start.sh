#!/bin/bash
# SessionStart hook — スキルの Python 依存（pyyaml / jsonschema / ruamel.yaml）を入れる。
# クラウドの新しいセッションには入っていないため、最初の dmlctl init が exit 3 で落ちるのを防ぐ。
# すでに入っていれば何もしない（冪等）。失敗してもセッションは止めない。
set -u
cd "${CLAUDE_PROJECT_DIR:-$(pwd)}" || exit 0
if python3 -c 'import yaml, jsonschema, ruamel.yaml' >/dev/null 2>&1; then
  exit 0
fi
REQ=".claude/skills/eventstorming-facilitator/requirements.txt"
python3 -m pip install -q --disable-pip-version-check --root-user-action=ignore -r "$REQ" >/dev/null 2>&1 \
  || python3 -m pip install -q --disable-pip-version-check --user -r "$REQ" >/dev/null 2>&1 \
  || echo "[session-start] 依存のインストールに失敗しました。手動で: pip install -r $REQ" >&2
exit 0
