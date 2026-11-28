#!/usr/bin/env python3
"""2026-11-28 の改善メモ由来の変更の単体テスト。

- 構造チェック: alt_narrative_entry / unreachable_scenario / next_bypasses_policy /
  policy_scenario_overlap / phase_drift / language_coverage（actor）
- view: ai-decided
- hints: 意味と正しい例（meaning / correct_example）
- validate: next と terminal の排他・params に文字列を渡したときの文言
- dmlctl: YAML として読めない --value の案内・未宣言 ctx の警告・derive-agg-events
- grill_pending.py: 未反映・選び直しの判定

依存ゼロ（stdlib unittest + プロジェクト既存の依存）。
実行: python3 scripts/tests/test_session_findings.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import yaml  # noqa: E402

from dml_filters.checks import (  # noqa: E402
    CHECKS,
    alt_narrative_entry,
    language_coverage,
    next_bypasses_policy,
    phase_drift,
    policy_scenario_overlap,
    unreachable_scenario,
)
from dml_filters.hints import resolve_hint  # noqa: E402
from dml_filters.views import VIEWS  # noqa: E402
from validate_dml import validate_dml_text  # noqa: E402
import grill_pending  # noqa: E402

DMLCTL = SCRIPTS / "dmlctl.py"
SCHEMA = yaml.safe_load((SCRIPTS.parent / "references" / "dml.schema.yaml").read_text(encoding="utf-8"))


def sc(name, cmd=None, evt=None, nxt=None, brs=None, ctx="bc", actor="System", **kw):
    d = {"name": name, "ctx": ctx, "actor": actor}
    if cmd:
        d["cmd"] = cmd
    if evt:
        d["evt"] = evt
    if nxt is not None:
        d["next"] = nxt
    if brs is not None:
        d["brs"] = brs
    d.update(kw)
    return d


class AltNarrativeEntryTest(unittest.TestCase):
    def test_ignored_before_flow_chaining(self):
        model = {"narratives": [{"id": "happy", "kind": "happy"}, {"id": "alt-a", "kind": "alt"}]}
        self.assertEqual(alt_narrative_entry(model), [])

    def test_alt_without_entry_is_warned(self):
        model = {"narratives": [
            {"id": "happy", "kind": "happy", "entry": "a"},
            {"id": "alt-a", "kind": "alt"},
            {"id": "alt-b", "kind": "alt", "entry": "b"},
        ]}
        f = alt_narrative_entry(model)
        self.assertEqual([x.slice["id"] for x in f], ["alt-a"])
        self.assertEqual(f[0].severity, "warn")


class UnreachableScenarioTest(unittest.TestCase):
    def test_next_and_policy_paths_are_followed(self):
        model = {
            "narratives": [{"id": "happy", "kind": "happy", "entry": "注文する"}],
            "scenarios": [
                sc("注文する", "PlaceOrder", "OrderPlaced", nxt="焙煎する"),
                sc("焙煎する", "Roast", "Roasted",
                   brs=None),
                sc("在庫を補充する", "Restock", "Restocked"),   # policy で到達
                sc("孤立した操作", "Lonely", "LonelyDone"),     # どこからも来ない
            ],
            "policies": [{"name": "RestockOnRoasted", "ctx": "bc", "trg": "Roasted", "cmd": "Restock"}],
        }
        f = unreachable_scenario(model)
        self.assertEqual([x.slice["scenario"] for x in f], ["孤立した操作"])
        self.assertTrue(all(x.severity == "warn" for x in f))

    def test_branch_next(self):
        model = {
            "narratives": [{"id": "happy", "kind": "happy", "entry": "a"}],
            "scenarios": [
                sc("a", "A", brs=[{"evt": "X", "next": "b"}, {"evt": "Y", "terminal": "happy"}]),
                sc("b", "B", "BDone"),
            ],
        }
        self.assertEqual(unreachable_scenario(model), [])

    def test_skipped_without_entries(self):
        self.assertEqual(unreachable_scenario({"scenarios": [sc("a", "A", "E")]}), [])


class NextBypassesPolicyTest(unittest.TestCase):
    POL = {"name": "RoastOnLowStock", "ctx": "bc", "trg": "StockLow", "cmd": "Roast"}

    def test_next_after_trigger_is_ok(self):
        model = {
            "scenarios": [sc("在庫減を検知する", "DetectLow", "StockLow", nxt="焙煎する"),
                          sc("焙煎する", "Roast", "Roasted")],
            "policies": [self.POL],
        }
        self.assertEqual(next_bypasses_policy(model), [])

    def test_direct_next_without_trigger_is_warned(self):
        model = {
            "scenarios": [sc("入荷する", "Receive", "Received", nxt="焙煎する"),
                          sc("焙煎する", "Roast", "Roasted")],
            "policies": [self.POL],
        }
        f = next_bypasses_policy(model)
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].slice["cmd"], "Roast")
        self.assertEqual(f[0].severity, "warn")

    def test_trigger_through_policy_chain(self):
        model = {
            "scenarios": [sc("入荷する", "Receive", "Received", nxt="焙煎する"),
                          sc("焙煎する", "Roast", "Roasted")],
            "policies": [
                {"name": "CheckStock", "ctx": "bc", "trg": "Received", "evt": "StockLow"},
                self.POL,
            ],
        }
        self.assertEqual(next_bypasses_policy(model), [])

    def test_branch_next_uses_branch_evt(self):
        model = {
            "scenarios": [
                sc("支払う", "Pay", brs=[{"evt": "Paid", "next": "焙煎する"}, {"evt": "StockLow", "next": "焙煎する"}]),
                sc("焙煎する", "Roast", "Roasted"),
            ],
            "policies": [self.POL],
        }
        # Paid の分岐は trg を発行しない → 1 件（同じ先は 1 回だけ報告）
        self.assertEqual(len(next_bypasses_policy(model)), 1)


class PolicyScenarioOverlapTest(unittest.TestCase):
    def test_consistent_is_clean(self):
        model = {
            "scenarios": [sc("焙煎する", "Roast", "Roasted", agg="Batch")],
            "policies": [{"name": "P", "ctx": "bc", "trg": "Low", "cmd": "Roast", "evt": "Roasted", "agg": "Batch"}],
        }
        self.assertEqual(policy_scenario_overlap(model), [])

    def test_evt_and_agg_mismatch(self):
        model = {
            "scenarios": [sc("焙煎する", "Roast", "Roasted", agg="Batch")],
            "policies": [{"name": "P", "ctx": "bc", "trg": "Low", "cmd": "Roast", "evt": "Other", "agg": "Lot"}],
        }
        paths = sorted(x.path for x in policy_scenario_overlap(model))
        self.assertEqual(paths, ["policies[0].agg", "policies[0].evt"])

    def test_side_effect_policy_duplicated_by_scenario(self):
        model = {
            "scenarios": [sc("客に知らせる", "NotifyCustomer", "CustomerNotified")],
            "policies": [{"name": "NotifyOnOutage", "ctx": "bc", "trg": "Outage", "evt": "CustomerNotified"}],
        }
        f = policy_scenario_overlap(model)
        self.assertEqual(len(f), 1)
        self.assertIn("副作用専用", f[0].message)


class PhaseDriftTest(unittest.TestCase):
    def test_aggregates_with_phase_1(self):
        model = {"session": {"phase": "1"}, "aggregates": [{"name": "A", "ctx": "bc"}]}
        f = phase_drift(model)
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].slice["inferred_at_least"], "4.5")

    def test_up_to_date(self):
        model = {"session": {"phase": "5"}, "aggregates": [{"name": "A", "ctx": "bc", "attrs": [{"name": "x"}]}]}
        self.assertEqual(phase_drift(model), [])

    def test_empty_model(self):
        self.assertEqual(phase_drift({"session": {"phase": "1"}}), [])

    def test_missing_phase(self):
        model = {"scenarios": [sc("a", "A", "E")]}
        self.assertEqual(phase_drift(model)[0].slice["phase"], None)


class LanguageCoverageActorTest(unittest.TestCase):
    def test_actor_is_warned_once(self):
        model = {
            "contexts": [{"name": "bc", "lang": {"cmds": {"A": "a"}, "evts": {"E": "e"}}}],
            "scenarios": [sc("x", "A", "E", actor="Barista"), sc("y", "A", "E", actor="Barista")],
        }
        f = language_coverage(model)
        self.assertEqual([(x.slice["kind"], x.severity) for x in f], [("ACTOR", "warn")])

    def test_registered_actor(self):
        model = {
            "contexts": [{"name": "bc", "lang": {"cmds": {"A": "a"}, "evts": {"E": "e"}, "actors": {"Barista": "店員"}}}],
            "scenarios": [sc("x", "A", "E", actor="Barista")],
        }
        self.assertEqual(language_coverage(model), [])


class UndeclaredCtxTest(unittest.TestCase):
    def test_reports_each_ctx_once(self):
        model = {
            "contexts": [{"name": "billing"}],
            "scenarios": [sc("a", "A", "E", ctx="ordering"), sc("b", "B", "F", ctx="ordering"),
                          sc("c", "C", "G", ctx="billing")],
            "policies": [{"name": "P", "ctx": "ops", "trg": "E"}],
        }
        f = CHECKS["undeclared_ctx"](model)
        self.assertEqual(sorted(x.slice["ctx"] for x in f), ["ops", "ordering"])
        self.assertTrue(all(x.severity == "warn" for x in f))


class LanguageCoverageFlowIdsTest(unittest.TestCase):
    def test_branch_evt_and_qry_are_warned(self):
        model = {
            "contexts": [{"name": "bc", "lang": {"cmds": {"A": "a"}, "actors": {"System": "システム"}}}],
            "scenarios": [sc("x", "A", brs=[{"evt": "Ok"}, {"evt": "Ng"}], qry=["Stock"])],
        }
        got = sorted((x.slice["identifier"], x.severity) for x in language_coverage(model))
        self.assertEqual(got, [("Ng", "warn"), ("Ok", "warn"), ("Stock", "warn")])


class RegistryTest(unittest.TestCase):
    def test_new_checks_registered(self):
        for name in ("undeclared_ctx", "alt_narrative_entry", "unreachable_scenario", "next_bypasses_policy",
                     "policy_scenario_overlap", "phase_drift"):
            self.assertIn(name, CHECKS)
        self.assertIn("ai-decided", VIEWS)


class AiDecidedViewTest(unittest.TestCase):
    def test_lists_only_ai_decisions(self):
        model = {"decisions": [
            {"id": "D1", "topic": "t", "chosen": "a", "options": [{"name": "a", "label": "エー", "adopted": True}]},
            {"id": "D2", "topic": "t", "chosen": "b", "decided_by": "ai-persona",
             "options": [{"name": "b", "label": "ビー", "adopted": True}]},
            {"id": "D3", "topic": "t", "chosen": "c", "decided_by": "user", "options": [{"name": "c"}]},
        ]}
        out = VIEWS["ai-decided"](model)
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["ai_decided"][0]["id"], "D2")
        self.assertEqual(out["ai_decided"][0]["chosen"], "ビー (b)")

    def test_schema_accepts_decided_by(self):
        dml = yaml.safe_dump({"decisions": [{"id": "D1", "topic": "t", "chosen": "a", "decided_by": "ai-default",
                                             "options": [{"name": "a", "adopted": True}]}]}, allow_unicode=True)
        self.assertEqual(validate_dml_text(dml, SCHEMA), [])
        bad = dml.replace("ai-default", "robot")
        self.assertTrue(validate_dml_text(bad, SCHEMA))


class HintNotesTest(unittest.TestCase):
    def test_via_meaning(self):
        h = resolve_hint(SCHEMA, "aggregates[name=Order].transitions[].via")
        self.assertIn("コマンド名", h["meaning"])
        self.assertIn("ConfirmOrder", h["correct_example"])

    def test_params_example(self):
        h = resolve_hint(SCHEMA, "aggregates[].events[].params")
        self.assertIn("{name: orderId", h["correct_example"])

    def test_no_note_for_plain_path(self):
        h = resolve_hint(SCHEMA, "session.phase")
        self.assertNotIn("meaning", h)


class ValidateMessageTest(unittest.TestCase):
    def test_next_terminal_exclusive_message(self):
        dml = yaml.safe_dump({"scenarios": [{"name": "a", "ctx": "bc", "actor": "A", "cmd": "X",
                                             "brs": [{"evt": "E", "next": "b", "terminal": "happy"}]}]},
                             allow_unicode=True)
        errs = validate_dml_text(dml, SCHEMA)
        self.assertTrue(any("`next` と `terminal` は同時に指定できません" in e for e in errs), errs)

    def test_params_string_hint(self):
        dml = yaml.safe_dump({"aggregates": [{"name": "A", "ctx": "bc",
                                              "events": [{"name": "E", "params": ["orderId"]}]}]})
        errs = validate_dml_text(dml, SCHEMA)
        self.assertTrue(any("{name: orderId}" in e for e in errs), errs)


def _dmlctl(*args):
    return subprocess.run([sys.executable, str(DMLCTL), *args], capture_output=True, text=True)


class DmlctlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = str(Path(self.tmp.name) / "t.dml.yaml")
        r = _dmlctl("init", self.file, "--session-id=t", "--domain=テスト", "--no-postprocess")
        self.assertEqual(r.returncode, 0, r.stderr)

    def tearDown(self):
        self.tmp.cleanup()

    def test_unparsable_value_guides_to_value_file(self):
        r = _dmlctl("set", self.file, "--path=session.goal", "--value=**境界の理由**: x", "--no-postprocess")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--value-file", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_undeclared_ctx_warning_after_write(self):
        r = _dmlctl("add", self.file, "--to=scenarios",
                    "--item={name: 注文する, ctx: ordering, actor: Customer, cmd: PlaceOrder, evt: OrderPlaced}")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ctx 'ordering' は contexts[] に宣言されていません", r.stderr)
        self.assertIn("日本語ラベル未登録", r.stderr)
        r = _dmlctl("add", self.file, "--to=contexts",
                    "--item={name: ordering, label_ja: 注文, lang: {cmds: {PlaceOrder: 注文する}, "
                    "evts: {OrderPlaced: 注文された}, actors: {Customer: 客}}}")
        self.assertNotIn("宣言されていません", r.stderr)
        self.assertNotIn("日本語ラベル未登録", r.stderr)

    def test_where_parse_error_does_not_suggest_value_file(self):
        _dmlctl("add", self.file, "--to=scenarios", "--item={name: a, ctx: bc, actor: A, cmd: X}", "--no-postprocess")
        r = _dmlctl("remove", self.file, "--path=scenarios", "--where=name=**x", "--no-postprocess")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("YAML として読めません", r.stderr)
        self.assertNotIn("--value-file", r.stderr)

    def test_derive_agg_events(self):
        _dmlctl("add", self.file, "--to=scenarios",
                "--item={name: a, ctx: bc, actor: A, cmd: X, agg: Order, brs: [{evt: E1}, {evt: E2}]}",
                "--no-postprocess")
        _dmlctl("add", self.file, "--to=aggregates", "--item={name: Order, ctx: bc, events: [{name: E1}]}",
                "--no-postprocess")
        r = _dmlctl("derive-agg-events", self.file, "--agg=Order", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        model = yaml.safe_load(Path(self.file).read_text(encoding="utf-8"))
        self.assertEqual([e["name"] for e in model["aggregates"][0]["events"]], ["E1"])  # dry-run は書かない
        r = _dmlctl("derive-agg-events", self.file, "--agg=Order", "--no-postprocess")
        self.assertEqual(r.returncode, 0, r.stderr)
        model = yaml.safe_load(Path(self.file).read_text(encoding="utf-8"))
        self.assertEqual([e["name"] for e in model["aggregates"][0]["events"]], ["E1", "E2"])
        r = _dmlctl("derive-agg-events", self.file, "--agg=Nope", "--no-postprocess")
        self.assertEqual(r.returncode, 2)


class GrillPendingTest(unittest.TestCase):
    def _write(self, root: Path, answers: dict, progress: dict | None, wrap=False):
        (root / "answers").mkdir(parents=True)
        for doc_id, doc in answers.items():
            body = {"data": doc, "version": 1} if wrap else doc
            (root / "answers" / f"{doc_id}.json").write_text(json.dumps(body), encoding="utf-8")
        if progress is not None:
            (root / "grill").mkdir()
            (root / "grill" / "progress.json").write_text(json.dumps(progress), encoding="utf-8")

    def test_pending_and_reselect(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._write(root, {
                "q-1--u1": {"qid": "q-1", "by": "u1", "at": "2026-11-28T10:00:00Z", "choices": ["a"]},
                "q-2--u1": {"qid": "q-2", "by": "u1", "at": "2026-11-28T10:05:00Z", "choices": ["b"]},
                "q-3--u1": {"qid": "q-3", "by": "u1", "at": "2026-11-28T11:00:00Z", "choices": ["c"],
                            "rev": 2, "prev": {"choices": ["d"], "at": "2026-11-28T09:00:00Z"}},
            }, {"reflected": {
                "q-1": {"at": "2026-11-28T10:01:00Z", "summary": "ok"},
                "q-3": {"at": "2026-11-28T09:30:00Z", "summary": "ok"},
            }})
            rows = grill_pending.pending(grill_pending.load_answers(root), grill_pending.load_reflected(root))
            self.assertEqual([(r["qid"], r["_reason"]) for r in rows], [("q-2", "未反映"), ("q-3", "選び直し")])
            self.assertEqual(grill_pending.main([str(root)]), 1)

    def test_all_reflected_exit_zero_wrapped_docs(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._write(root, {"q-1--u1": {"qid": "q-1", "at": "2026-11-28T10:00:00Z"}},
                        {"data": {"reflected": {"q-1": {"at": "2026-11-28T10:00:00Z"}}}}, wrap=True)
            self.assertEqual(grill_pending.main([str(root)]), 0)

    def test_no_progress_doc(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._write(root, {"q-9--u1": {"at": "2026-11-28T10:00:00Z"}}, None)
            rows = grill_pending.pending(grill_pending.load_answers(root), grill_pending.load_reflected(root))
            self.assertEqual(rows[0]["qid"], "q-9")  # doc_id から補う


class BuilderMarksTest(unittest.TestCase):
    """ビルダー: 訳未登録の印・上部の件数・AI が選んだ決定の印・空のモデルの案内・レーンの日本語名。"""

    def _build(self, model) -> str:
        import eventstorming_build as eb
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "docs" / "eventstorming" / "t.dml.yaml"
            src.parent.mkdir(parents=True)
            src.write_text(yaml.safe_dump(model, allow_unicode=True), encoding="utf-8")
            out = eb.build(src, Path(d) / "out")
            return out.read_text(encoding="utf-8")

    def test_empty_model_shows_guide(self):
        html = self._build({"session": {"id": "t", "domain": "d"}})
        self.assertIn("質問に答えると、ここにモデルが育ちます", html)
        self.assertNotIn('class="label-gaps"', html)

    def test_untranslated_marks_and_lane_label(self):
        html = self._build({
            "session": {"id": "t", "domain": "d"},
            "contexts": [{"name": "ordering", "label_ja": "注文", "lang": {"cmds": {"PlaceOrder": "注文する"}}}],
            "narratives": [{"id": "happy", "kind": "happy", "entry": "客が注文する", "prose": "p"}],
            "scenarios": [{"name": "客が注文する", "ctx": "ordering", "actor": "Customer",
                           "cmd": "PlaceOrder", "evt": "OrderPlaced"}],
        })
        self.assertNotIn("質問に答えると、ここにモデルが育ちます", html)
        self.assertIn("日本語ラベル未登録: 2 件", html)
        self.assertIn('class="untr"', html)
        self.assertIn('注文<span class="lane-id">ordering</span>', html)
        # 登録済みのコマンドには印が付かない
        self.assertIn("注文する</div>", html)

    def test_ai_decision_badge(self):
        html = self._build({
            "session": {"id": "t", "domain": "d"},
            "decisions": [
                {"id": "D1", "topic": "接客", "chosen": "a", "decided_by": "ai-persona",
                 "options": [{"name": "a", "adopted": True}]},
                {"id": "D2", "topic": "支払い", "chosen": "b", "decided_by": "ai-default",
                 "options": [{"name": "b", "adopted": True}]},
                {"id": "D3", "topic": "焙煎", "chosen": "c", "options": [{"name": "c", "adopted": True}]},
            ],
        })
        self.assertIn("AI が選択（要確認）", html)
        self.assertIn("AI が選択（おまかせ）", html)
        self.assertEqual(html.count('<span class="ai-badge'), 2)


class GrillPanelBrowserTest(unittest.TestCase):
    """質問パネルの選び直し（Playwright。node と playwright が無ければ skip）。"""

    def test_reselect_flow(self):
        import shutil
        node = shutil.which("node")
        if not node:
            self.skipTest("node がありません")
        probe = subprocess.run([node, "-e", "require('playwright')"], capture_output=True)
        if probe.returncode != 0:
            self.skipTest("playwright がありません")
        import eventstorming_build as eb
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "docs" / "eventstorming" / "t.dml.yaml"
            src.parent.mkdir(parents=True)
            src.write_text(yaml.safe_dump({"session": {"id": "t", "domain": "d"}}), encoding="utf-8")
            page = eb.build(src, Path(d) / "out", artifact=True)
            r = subprocess.run([node, str(Path(__file__).with_name("grill_panel_check.js")), str(page)],
                               capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["errs"], [])
        self.assertIn("あなた・モデルに反映済み", out["log"][0])
        self.assertIn("元になった決定: D3", out["log"][0])
        self.assertEqual(out["formQ"], "接客はどうする？")
        self.assertIn("前の回答：キャラが接客", out["note"])
        w = out["written"][0]["d"]
        self.assertEqual((w["choices"], w["rev"], w["prev"]["choices"]), (["人が接客"], 2, ["キャラが接客"]))
        self.assertIn("キャラが接客 → 人が接客", out["strip"])
        self.assertEqual(out["qAfter"], "二問目？")
        self.assertIn("反映が始まっていません", out["model"])
        self.assertIn("10 分前に届いた", out["model"])


if __name__ == "__main__":
    unittest.main()
