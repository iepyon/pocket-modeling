#!/usr/bin/env python3
"""集約・集約間連携の整合チェック（構造チェック拡張）の単体テスト。

依存ゼロ（stdlib unittest）。fixture はインライン dict で各チェック関数を直接呼ぶ。
実行: python3 scripts/tests/test_structural_checks.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dml_filters.checks import (  # noqa: E402
    CHECKS,
    evt_ownership,
    policy_agg_consistency,
    policy_cycle,
    scenario_agg_consistency,
    state_dead_end,
    transition_state_integrity,
)


def agg(name="A", states=("S1", "S2"), transitions=(), events=(), **kw):
    return {
        "name": name,
        "ctx": "bc",
        "states": list(states),
        "transitions": list(transitions),
        "events": [{"name": e} for e in events],
        **kw,
    }


def tr(frm, to, via, when=None):
    t = {"from": frm, "to": to, "via": via}
    if when:
        t["when"] = when
    return t


class RegistrationTest(unittest.TestCase):
    def test_all_registered(self):
        for name in (
            "transition_state_integrity",
            "scenario_agg_consistency",
            "evt_ownership",
            "policy_agg_consistency",
            "policy_cycle",
            "state_dead_end",
        ):
            self.assertIn(name, CHECKS)


class TransitionStateIntegrityTest(unittest.TestCase):
    def test_clean_with_none_initial(self):
        m = {"aggregates": [agg(transitions=[tr("NONE", "S1", "C1"), tr("S1", "S2", "C2")])]}
        self.assertEqual(transition_state_integrity(m), [])

    def test_unknown_from_and_to(self):
        m = {"aggregates": [agg(transitions=[tr("X", "S2", "C1"), tr("S1", ["S2", "Y"], "C2")])]}
        paths = [f.path for f in transition_state_integrity(m)]
        self.assertEqual(
            paths,
            ["aggregates[0].transitions[0].from", "aggregates[0].transitions[1].to"],
        )

    def test_duplicate_without_when_is_ambiguous(self):
        m = {"aggregates": [agg(transitions=[tr("S1", "S2", "C"), tr("S1", "S1", "C")])]}
        self.assertEqual(len(transition_state_integrity(m)), 1)

    def test_branch_with_when_and_distinct_to_is_ok(self):
        m = {"aggregates": [agg(transitions=[
            tr("NONE", "S1", "C", when="空きあり"),
            tr("NONE", "S2", "C", when="満席"),
        ])]}
        self.assertEqual(transition_state_integrity(m), [])

    def test_duplicate_with_when_but_same_to_is_ambiguous(self):
        m = {"aggregates": [agg(transitions=[
            tr("S1", "S2", "C", when="a"),
            tr("S1", "S2", "C", when="b"),
        ])]}
        self.assertEqual(len(transition_state_integrity(m)), 1)

    def test_terminal_states_must_exist(self):
        m = {"aggregates": [agg(terminalStates=["S2", "ZZ"])]}
        fs = transition_state_integrity(m)
        self.assertEqual([f.path for f in fs], ["aggregates[0].terminalStates[1]"])

    def test_no_states_is_skipped(self):
        m = {"aggregates": [agg(states=(), transitions=[tr("X", "Y", "C")])]}
        self.assertEqual(transition_state_integrity(m), [])


class ScenarioAggConsistencyTest(unittest.TestCase):
    def sc(self, **kw):
        return {"name": "s", "ctx": "bc", "actor": "U", **kw}

    def test_unknown_agg_is_error(self):
        m = {"aggregates": [agg()], "scenarios": [self.sc(agg="Nope", cmd="C")]}
        fs = scenario_agg_consistency(m)
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].severity, "error")

    def test_cmd_not_in_via_is_not_checked(self):
        # creation / 属性変更の cmd は transitions に書かない規約
        m = {
            "aggregates": [agg(transitions=[tr("S1", "S2", "Other")])],
            "scenarios": [self.sc(agg="A", cmd="Create")],
        }
        self.assertEqual(scenario_agg_consistency(m), [])

    def test_evt_not_declared_is_warn(self):
        m = {
            "aggregates": [agg(events=["Known"])],
            "scenarios": [self.sc(agg="A", cmd="C", evt="Unknown")],
        }
        fs = scenario_agg_consistency(m)
        self.assertEqual([f.severity for f in fs], ["warn"])

    def test_brs_evt_checked_and_undeclared_events_skipped(self):
        m = {
            "aggregates": [agg(events=["Known"]), agg(name="B")],
            "scenarios": [
                self.sc(agg="A", cmd="C", brs=[{"evt": "Known"}, {"evt": "Bad"}]),
                self.sc(agg="B", cmd="D", evt="Anything"),  # B は events 未宣言 → 対象外
            ],
        }
        fs = scenario_agg_consistency(m)
        self.assertEqual([f.path for f in fs], ["scenarios[0].brs[1].evt"])


class EvtOwnershipTest(unittest.TestCase):
    def test_duplicate_owner(self):
        m = {"aggregates": [agg("A", events=["E"]), agg("B", events=["E", "F"])]}
        fs = evt_ownership(m)
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].slice["aggs"], ["A", "B"])

    def test_unique_is_clean(self):
        m = {"aggregates": [agg("A", events=["E"]), agg("B", events=["F"])]}
        self.assertEqual(evt_ownership(m), [])


class PolicyAggConsistencyTest(unittest.TestCase):
    def test_side_effect_policy_without_agg_is_ok(self):
        m = {"aggregates": [agg()], "policies": [{"name": "P", "ctx": "bc", "trg": "E", "cmd": "Notify"}]}
        self.assertEqual(policy_agg_consistency(m), [])

    def test_unknown_agg_error_and_evt_warn(self):
        m = {
            "aggregates": [agg(events=["Known"])],
            "policies": [
                {"name": "P1", "ctx": "bc", "trg": "E", "cmd": "C", "agg": "Nope"},
                {"name": "P2", "ctx": "bc", "trg": "E", "cmd": "C", "agg": "A", "evt": "Bad"},
            ],
        }
        fs = policy_agg_consistency(m)
        self.assertEqual([(f.path, f.severity) for f in fs],
                         [("policies[0].agg", "error"), ("policies[1].evt", "warn")])


class PolicyCycleTest(unittest.TestCase):
    def pol(self, name, trg, cmd=None, evt=None):
        p = {"name": name, "ctx": "bc", "trg": trg}
        if cmd:
            p["cmd"] = cmd
        if evt:
            p["evt"] = evt
        return p

    def test_two_policy_loop_via_scenario_cmd(self):
        m = {
            "scenarios": [
                {"name": "s1", "ctx": "bc", "actor": "U", "cmd": "CmdB", "evt": "EvtB"},
                {"name": "s2", "ctx": "bc", "actor": "U", "cmd": "CmdA", "evt": "EvtA"},
            ],
            "policies": [self.pol("P1", "EvtA", cmd="CmdB"), self.pol("P2", "EvtB", cmd="CmdA")],
        }
        fs = policy_cycle(m)
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].severity, "warn")
        self.assertEqual(fs[0].slice["events"], ["EvtA", "EvtB"])

    def test_self_loop(self):
        m = {"policies": [self.pol("P", "E", evt="E")]}
        self.assertEqual(len(policy_cycle(m)), 1)

    def test_chain_without_cycle(self):
        m = {"policies": [self.pol("P1", "A", evt="B"), self.pol("P2", "B", evt="C")]}
        self.assertEqual(policy_cycle(m), [])


class StateDeadEndTest(unittest.TestCase):
    def test_dead_end_and_terminal_declaration(self):
        base = dict(states=("A", "B", "C"),
                    transitions=[tr("A", "B", "x"), tr("B", "C", "y")])
        fs = state_dead_end({"aggregates": [agg(**base)]})
        self.assertEqual([f.slice["state"] for f in fs], ["C"])
        self.assertEqual(fs[0].severity, "warn")
        self.assertEqual(state_dead_end({"aggregates": [agg(terminalStates=["C"], **base)]}), [])

    def test_no_transitions_is_skipped(self):
        self.assertEqual(state_dead_end({"aggregates": [agg(states=("ONLY",))]}), [])


if __name__ == "__main__":
    unittest.main()
