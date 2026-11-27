#!/usr/bin/env python3
"""dml_to_quint.py と invariant_refs の単体テスト。

依存は stdlib + pyyaml。Quint CLI は無くても動く（QUINT_BIN があれば結合テストも走る）。
実行: python3 scripts/tests/test_dml_to_quint.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dml_filters.checks import invariant_refs  # noqa: E402
from dml_to_quint import MODULE, generate  # noqa: E402


def model(**kw):
    base = {
        "narratives": [{"id": "happy", "kind": "happy", "entry": "注文する", "prose": "x"}],
        "aggregates": [
            {"name": "Order", "ctx": "o", "states": ["PLACED", "CANCELLED"],
             "transitions": [{"from": "NONE", "to": "PLACED", "via": "PlaceOrder"},
                             {"from": "PLACED", "to": "CANCELLED", "via": "CancelOrder"}]},
            {"name": "Stock", "ctx": "s", "states": ["FREE", "RESERVED"],
             "transitions": [{"from": "NONE", "to": "FREE", "via": "OpenStock"},
                             {"from": "FREE", "to": "RESERVED", "via": "Reserve"}]},
        ],
        "scenarios": [
            {"name": "注文する", "ctx": "o", "actor": "U", "agg": "Order", "cmd": "PlaceOrder", "evt": "OrderPlaced"},
            {"name": "在庫を登録する", "ctx": "s", "actor": "U", "agg": "Stock", "cmd": "OpenStock", "evt": "StockOpened"},
            {"name": "在庫を引き当てる", "ctx": "s", "actor": "System", "agg": "Stock", "cmd": "Reserve", "evt": "Reserved"},
        ],
        "policies": [
            {"name": "ReserveOnPlaced", "ctx": "s", "trg": "OrderPlaced", "cmd": "Reserve", "agg": "Stock"},
        ],
    }
    base.update(kw)
    return base


class GenerateTest(unittest.TestCase):
    def test_structure_and_initial_states(self):
        q = generate(model())
        self.assertIn(f"module {MODULE} {{", q)
        self.assertIn("var st_Order: str", q)
        self.assertIn("st_Order' = \"NONE\"", q)  # NONE から出る遷移があれば初期は NONE
        self.assertIn("action step = any {", q)

    def test_policy_fires_after_trigger_and_sends_message(self):
        q = generate(model())
        self.assertIn('emitted.contains("OrderPlaced")', q)
        self.assertIn('inbox.union(Set("Stock.Reserve"))', q)

    def test_policy_driven_scenario_has_no_user_issue_action(self):
        q = generate(model())
        self.assertIn("issue_0_PlaceOrder", q)       # entry は利用者が発行できる
        self.assertNotRegex(q, r"action issue_\d+_Reserve")  # POLICY 駆動の cmd は利用者から発行しない

    def test_reject_when_state_mismatch(self):
        q = generate(model())
        self.assertIn("action reject_Stock_Reserve", q)
        self.assertIn('st_Stock != "FREE"', q)

    def test_duplicates_keep_message_in_inbox(self):
        self.assertIn("inbox.exclude(", generate(model()))
        self.assertNotIn("inbox.exclude(", generate(model(), duplicates=True))

    def test_safety_and_liveness_properties(self):
        invs = [
            {"name": "NoCancelledReserved", "kind": "safety",
             "never": [{"agg": "Order", "in": ["CANCELLED"]}, {"agg": "Stock", "in": ["RESERVED"]}]},
            {"name": "CancelFrees", "kind": "liveness",
             "when": {"agg": "Order", "in": ["CANCELLED"]}, "eventually": {"agg": "Stock", "in": ["FREE"]}},
        ]
        q = generate(model(invariants=invs))
        self.assertIn("val inv_NoCancelledReserved = not(and {", q)
        self.assertIn("temporal live_CancelFrees = always(", q)
        self.assertIn("val allInvariants = and { inv_NoCancelledReserved }", q)
        self.assertIn("val quiet_CancelFrees = not(sysEnabled) implies", q)
        self.assertIn("val allQuiescent = and { quiet_CancelFrees }", q)
        self.assertIn("action stepLive = any {", q)

    def test_unknown_agg_in_invariant_exits(self):
        invs = [{"name": "X", "kind": "safety", "never": [{"agg": "Ghost", "in": ["A"]}]}]
        with self.assertRaises(SystemExit):
            generate(model(invariants=invs))

    def test_brs_events_pair_with_transition_groups(self):
        m = model()
        m["aggregates"][0]["transitions"].append({"from": "PLACED", "to": "PLACED", "via": "Confirm"})
        m["scenarios"].append({"name": "確認する", "ctx": "o", "actor": "U", "agg": "Order", "cmd": "Confirm",
                               "brs": [{"evt": "Confirmed"}, {"evt": "NotConfirmed"}]})
        q = generate(m)
        self.assertIn('Set("Confirmed")', q)
        self.assertIn("deliver_Order_Confirm_noop_0", q)  # 余る brs は状態不変で EVT のみ


class InvariantRefsTest(unittest.TestCase):
    def test_clean_and_findings(self):
        good = [{"name": "I", "kind": "safety", "never": [{"agg": "Order", "in": ["PLACED"]}]}]
        self.assertEqual(invariant_refs(model(invariants=good)), [])
        bad = [
            {"name": "I1", "kind": "safety", "never": [{"agg": "Ghost", "in": ["A"]}]},
            {"name": "I2", "kind": "liveness", "when": {"agg": "Order", "in": ["NOPE"]},
             "eventually": {"agg": "Stock", "in": ["FREE"]}},
        ]
        paths = [f.path for f in invariant_refs(model(invariants=bad))]
        self.assertEqual(paths, ["invariants[0].never[0].agg", "invariants[1].when.in"])


@unittest.skipUnless(os.environ.get("QUINT_BIN"), "QUINT_BIN 未設定のため Quint 結合テストは省略")
class QuintIntegrationTest(unittest.TestCase):
    def run_quint(self, invs):
        q = generate(model(invariants=invs))
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "m.qnt"
            f.write_text(q, encoding="utf-8")
            tc = subprocess.run([os.environ["QUINT_BIN"], "typecheck", str(f)], capture_output=True, text=True)
            self.assertEqual(tc.returncode, 0, tc.stdout + tc.stderr)
            r = subprocess.run(
                [os.environ["QUINT_BIN"], "run", str(f), f"--main={MODULE}", "--invariant=allInvariants",
                 "--max-steps=15", "--max-samples=300"], capture_output=True, text=True)
            return r.returncode

    def test_true_invariant_holds_and_false_one_is_violated(self):
        # PLACED になる前に RESERVED にはなれない（Reserve は OrderPlaced が起点）→ 成り立つ
        ok = [{"name": "A", "kind": "safety",
               "never": [{"agg": "Order", "in": ["NONE"]}, {"agg": "Stock", "in": ["RESERVED"]}]}]
        # 注文確定と在庫引当の間に窓がある → 破れる
        ng = [{"name": "B", "kind": "safety",
               "never": [{"agg": "Order", "in": ["PLACED"]}, {"agg": "Stock", "in": ["FREE"]}]}]
        self.assertEqual(self.run_quint(ok), 0)
        self.assertNotEqual(self.run_quint(ng), 0)


if __name__ == "__main__":
    unittest.main()
