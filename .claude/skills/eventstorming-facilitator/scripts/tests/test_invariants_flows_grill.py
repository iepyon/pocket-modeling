#!/usr/bin/env python3
"""改善計画 20261128-invariants-flows-and-grill-tooling 段階 1 の単体テスト。

- 構造チェック: alt_flow_takes_default_branch / rules_without_invariants
- dmlctl: rename が affects の `<種別>.<識別子>` を置換 / update --merge-yaml のリスト置換の警告 /
  add --before・--after / init の実時計 ID と時刻ずれの警告 / decision（採用案の付け替え）
- dml_to_quint: --backend=auto のやり直し
- grill_pending（未来の時刻の警告）/ grill_reflect / grill_queue

実行: python3 scripts/tests/test_invariants_flows_grill.py
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import yaml  # noqa: E402

import dml_to_quint  # noqa: E402
import grill_pending  # noqa: E402
import grill_queue  # noqa: E402
import grill_reflect  # noqa: E402
from dml_filters.checks import (  # noqa: E402
    CHECKS,
    alt_flow_takes_default_branch,
    phase_outputs_missing,
    rules_without_invariants,
)
from dml_filters.refs import apply_rename, collect_refs  # noqa: E402

DMLCTL = SCRIPTS / "dmlctl.py"


def _flow_model(alt_entry: str, *, terminal: bool = False, keyed: bool = False) -> dict:
    """承認／条件つき承認に分かれる流れ。alt は「条件つき承認」。"""
    decide = {
        "name": "会議が判断する", "ctx": "a", "actor": "Board", "cmd": "Decide",
        "brs": [
            {"cond": "承認", "evt": "Approved"},
            {"cond": "条件つき", "evt": "ConditionallyApproved", "next": "担当が条件を確かめる"},
        ],
    }
    if terminal:
        decide["brs"][1] = {"cond": "条件つき", "evt": "ConditionallyApproved", "terminal": "alt-conditional"}
    submit = {"name": "起案者が提出する", "ctx": "a", "actor": "Proposer", "cmd": "Submit", "evt": "Submitted",
              "next": {"happy": "会議が判断する", "alt-conditional": "会議が判断する"} if keyed else "会議が判断する"}
    return {
        "narratives": [
            {"id": "happy", "kind": "happy", "entry": "起案者が提出する"},
            {"id": "alt-conditional", "kind": "alt", "entry": alt_entry},
        ],
        "scenarios": [
            submit, decide,
            {"name": "担当が条件を確かめる", "ctx": "a", "actor": "Approver", "cmd": "Verify", "evt": "Verified"},
        ],
    }


class TestAltFlowCheck(unittest.TestCase):
    def test_alt_following_main_path_is_warned(self):
        f = alt_flow_takes_default_branch(_flow_model("起案者が提出する"))
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].severity, "warn")
        self.assertIn("会議が判断する", f[0].message)

    def test_entry_on_branch_target_is_ok(self):
        self.assertEqual(alt_flow_takes_default_branch(_flow_model("担当が条件を確かめる")), [])

    def test_terminal_for_alt_is_ok(self):
        self.assertEqual(alt_flow_takes_default_branch(_flow_model("起案者が提出する", terminal=True)), [])

    def test_keyed_next_is_ok(self):
        self.assertEqual(alt_flow_takes_default_branch(_flow_model("起案者が提出する", keyed=True)), [])

    def test_without_happy_entry_is_silent(self):
        m = _flow_model("起案者が提出する")
        m["narratives"][0].pop("entry")
        self.assertEqual(alt_flow_takes_default_branch(m), [])

    def test_registered(self):
        self.assertIn("alt_flow_takes_default_branch", CHECKS)
        self.assertIn("rules_without_invariants", CHECKS)


class TestRulesWithoutInvariants(unittest.TestCase):
    def _model(self, phase="5"):
        return {
            "session": {"phase": phase},
            "aggregates": [{"name": "Plan", "ctx": "a"}, {"name": "Consultation", "ctx": "a"}],
            "scenarios": [{"name": "起案者が提出する", "ctx": "a", "actor": "P", "cmd": "Submit",
                           "rules": [{"rule": "NoSubmitWhileConsultationOpen"}]}],
        }

    def test_warns(self):
        f = rules_without_invariants(self._model())
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].severity, "warn")

    def test_silent_before_phase5(self):
        self.assertEqual(rules_without_invariants(self._model(phase="4.6")), [])

    def test_silent_with_invariants_or_requires(self):
        m = self._model()
        m["invariants"] = [{"name": "X", "kind": "safety"}]
        self.assertEqual(rules_without_invariants(m), [])
        m = self._model()
        m["scenarios"][0]["requires"] = [{"agg": "Consultation", "in": ["ANSWERED"]}]
        self.assertEqual(rules_without_invariants(m), [])

    def test_silent_with_single_aggregate(self):
        m = self._model()
        m["aggregates"] = m["aggregates"][:1]
        self.assertEqual(rules_without_invariants(m), [])


class TestPhaseOutputsMissing(unittest.TestCase):
    def test_description_and_actions(self):
        m = {"session": {"phase": "7"}, "contexts": [{"name": "a", "description": "x"}, {"name": "b"}]}
        paths = [f.path for f in phase_outputs_missing(m)]
        self.assertEqual(paths, ["contexts[1].description", "actions"])
        m["session"]["phase"] = "4"
        self.assertEqual(phase_outputs_missing(m), [])


class TestRenameAffects(unittest.TestCase):
    def test_qualified_reference_is_renamed(self):
        m = {"policies": [{"name": "NotifyX"}],
             "decisions": [{"id": "D1", "affects": ["policies.NotifyX", "policies.NotifyXY", "NotifyX"]}]}
        self.assertEqual(len(collect_refs(m, "NotifyX")["occurrences"]), 3)
        replaced, _m, conflicts = apply_rename(m, "NotifyX", "NotifyY")
        self.assertEqual(conflicts, [])
        self.assertEqual(m["decisions"][0]["affects"], ["policies.NotifyY", "policies.NotifyXY", "NotifyY"])
        self.assertEqual(len(replaced), 3)


def _dmlctl(*args, cwd=None):
    return subprocess.run([sys.executable, str(DMLCTL), *args], capture_output=True, text=True, cwd=cwd)


class TestDmlctl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.file = self.dir / "t.dml.yaml"
        self.file.write_text(yaml.safe_dump({
            "actions": [{"id": "A1", "text": "a"}, {"id": "A3", "text": "c"}],
            "decisions": [{
                "id": "D1", "topic": "t", "chosen": "a",
                "options": [{"name": "a", "label": "案A", "adopted": True, "why": "早い"},
                            {"name": "b", "label": "案B", "why_not": "高い"}],
                "affects": ["session.goal"],
            }],
        }, allow_unicode=True), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _load(self):
        return yaml.safe_load(self.file.read_text(encoding="utf-8"))

    def test_merge_yaml_warns_on_list_replacement(self):
        r = _dmlctl("update", str(self.file), "--path=decisions", "--where=id=D1",
                    "--merge-yaml={options: [{name: c}]}", "--dry-run")
        self.assertIn("丸ごと置き換わります", r.stderr)
        self.assertIn("--- 前", r.stderr)
        r = _dmlctl("update", str(self.file), "--path=decisions", "--where=id=D1",
                    "--merge-yaml={topic: u}", "--no-postprocess")
        self.assertNotIn("丸ごと置き換わります", r.stderr)

    def test_add_before_and_after(self):
        self.assertEqual(_dmlctl("add", str(self.file), "--to=actions", "--item={id: A2, text: b}",
                                 "--before=id=A3", "--no-postprocess").returncode, 0)
        self.assertEqual(_dmlctl("add", str(self.file), "--to=actions", "--item={id: A0, text: z}",
                                 "--after=id=A3", "--no-postprocess").returncode, 0)
        self.assertEqual([a["id"] for a in self._load()["actions"]], ["A1", "A2", "A3", "A0"])
        r = _dmlctl("add", str(self.file), "--to=actions", "--item={id: A9}", "--before=id=nope", "--no-postprocess")
        self.assertEqual(r.returncode, 2)

    def test_decision_rechoose(self):
        r = _dmlctl("decision", str(self.file), "--id=D1", "--choose=b", "--no-postprocess")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = self._load()["decisions"][0]
        a, b = d["options"]
        self.assertEqual(d["chosen"], "b")
        self.assertTrue(b["adopted"])
        self.assertNotIn("why_not", b)
        self.assertNotIn("adopted", a)
        self.assertIn("回答の選び直しで不採用（案A→案B）", a["why_not"])
        self.assertIn("当初の採用理由: 早い", a["why_not"])
        self.assertEqual(_dmlctl("decision", str(self.file), "--id=D1", "--choose=zz",
                                 "--no-postprocess").returncode, 2)

    def test_init_uses_clock(self):
        r = _dmlctl("init", "--domain=テスト", "--no-postprocess", cwd=self.dir)
        self.assertEqual(r.returncode, 0, r.stderr)
        files = list((self.dir / "docs/eventstorming").glob("eventstorming-*.dml.yaml"))
        self.assertEqual(len(files), 1)
        session = yaml.safe_load(files[0].read_text(encoding="utf-8"))["session"]
        self.assertEqual(session["id"] + ".dml.yaml", files[0].name)
        self.assertTrue(session["started_at"].endswith("Z"))

    def test_init_warns_on_clock_skew(self):
        r = _dmlctl("init", str(self.dir / "eventstorming-20000101-0900.dml.yaml"), "--domain=x", "--no-postprocess")
        self.assertEqual(r.returncode, 0)
        self.assertIn("ずれています", r.stderr)


class TestQuintBackend(unittest.TestCase):
    def _fake(self, body: str) -> Path:
        d = Path(tempfile.mkdtemp())
        p = d / "fake.py"
        p.write_text(body, encoding="utf-8")
        return p

    def test_auto_retries_with_typescript(self):
        fake = self._fake(
            "import sys\n"
            "if '--backend=rust' in sys.argv: print('Error: failed to download evaluator', file=sys.stderr); sys.exit(1)\n"
            "print('[ok] No violation found'); sys.exit(0)\n")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = dml_to_quint.run_simulation([sys.executable, str(fake)], "auto")
        self.assertEqual(rc, 0)
        self.assertIn("typescript でやり直します", err.getvalue())

    def test_auto_keeps_violation_result(self):
        fake = self._fake("import sys\nprint('[violation] Found an issue'); sys.exit(1)\n")
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = dml_to_quint.run_simulation([sys.executable, str(fake)], "auto")
        self.assertEqual(rc, 1)
        self.assertNotIn("やり直します", err.getvalue())


def _write(root: Path, rel: str, doc):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class TestGrillTools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pending_flags_future_reflection(self):
        future = self.now + timedelta(hours=1)
        _write(self.root, "answers/q-1--u.json",
               {"qid": "q-1", "at": _iso(self.now - timedelta(minutes=1)), "choices": ["b"], "rev": 2})
        _write(self.root, "grill/progress.json", {"reflected": {"q-1": {"at": _iso(future), "summary": "s"}}})
        self.assertIn("q-1", grill_pending.future_reflections(grill_pending.load_reflected(self.root)))
        rows = grill_pending.pending(grill_pending.load_answers(self.root), grill_pending.load_reflected(self.root))
        self.assertEqual([r["_reason"] for r in rows], ["要確認（反映の記録の時刻が未来）"])

    def test_reflect_stamps_clock_and_version(self):
        _write(self.root, "answers/q-1--u.json", {"qid": "q-1", "at": _iso(self.now - timedelta(minutes=1))})
        _write(self.root, "grill/progress.json", {"data": {"reflected": {}}, "version": 7})
        batch, errors = grill_reflect.build_writes(self.root, [("q-1", "記録した")], ["D3"], now=self.now)
        self.assertEqual(errors, [])
        op = batch["writes"][0]
        self.assertEqual(op["if_version"], 7)
        rec = op["data"]["reflected"]["q-1"]
        self.assertEqual(rec["at"], _iso(self.now))
        self.assertEqual(rec["decisions"], ["D3"])

    def test_reflect_rejects_unknown_qid_and_future_answer(self):
        _write(self.root, "answers/q-1--u.json", {"qid": "q-1", "at": _iso(self.now + timedelta(hours=1))})
        batch, errors = grill_reflect.build_writes(self.root, [("q-1", "s"), ("q-9", "s")], [], now=self.now)
        self.assertIsNone(batch)
        self.assertEqual(len(errors), 2)

    def test_queue_prune_front_add(self):
        q = lambda qid: {"qid": qid, "text": "?", "options": [{"label": "x"}, {"label": "y"}]}  # noqa: E731
        _write(self.root, "grill/current.json", {"data": {"questions": [q("q-1"), q("q-2")]}, "version": 3})
        _write(self.root, "answers/q-1--u.json", {"qid": "q-1", "at": _iso(self.now)})
        _write(self.root, "grill/progress.json", {"reflected": {"q-1": {"at": _iso(self.now), "summary": "s"}}})
        write, errors, _info = grill_queue.build_write(self.root, prune=True, remove=[], front=[q("q-3")],
                                                       add=[q("q-4")], done=None)
        self.assertEqual(errors, [])
        op = write["writes"][0]
        self.assertEqual(op["if_version"], 3)
        self.assertEqual([x["qid"] for x in op["data"]["questions"]], ["q-3", "q-2", "q-4"])

    def test_queue_rejects_reused_qid_and_marked_labels(self):
        _write(self.root, "grill/current.json", {"questions": []})
        _write(self.root, "answers/q-1--u.json", {"qid": "q-1", "at": _iso(self.now)})
        bad = [{"qid": "q-1", "text": "?", "options": [{"label": "x"}, {"label": "y"}]},
               {"qid": "q-2", "text": "?", "options": [{"label": "a. はい"}, {"label": "b. いいえ"}]},
               {"qid": "q-3", "text": "?", "options": [{"label": "only"}]}]
        write, errors, _ = grill_queue.build_write(self.root, prune=False, remove=[], front=[], add=bad, done=None)
        self.assertIsNone(write)
        self.assertTrue(any("q-1" in e for e in errors))
        self.assertTrue(any("記号" in e for e in errors))
        self.assertTrue(any("2〜4" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
