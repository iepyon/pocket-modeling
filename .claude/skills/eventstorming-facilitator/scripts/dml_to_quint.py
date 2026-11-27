#!/usr/bin/env python3
"""DML → Quint（モデル検査用の状態遷移系）変換。スパイク版。

集約・集約間連携（POLICY 連鎖）を非同期メッセージの状態遷移系に落とし、
`invariants[]`（safety / liveness）を Quint の性質として書き出す。

使い方:
    python3 scripts/dml_to_quint.py <file.dml.yaml> [--out <file.qnt>] [--duplicates]
    python3 scripts/dml_to_quint.py <file.dml.yaml> --run [--max-steps N] [--max-samples N]

--run   は Quint CLI（`quint`、または環境変数 QUINT_BIN）で `quint run`（ランダムシミュレーション）を実行する。
        safety は `step` 上の allInvariants、liveness は `stepLive` 上の allQuiescent（下記）を検査する。
--verify は `quint verify`（Apalache による有界モデル検査）で同じ 2 つを網羅的に検査する。
        初回は Apalache を自動取得する。ステップ数が大きいと時間がかかる（60 アクション規模で
        9 ステップ約 2 分）ため、深い状態へ到達する検査は --run を使う。

## 変換モデル（過大近似・各集約 1 インスタンス）

- 状態: 集約ごとに変数 `st_<Agg>`。初期値は `NONE` から出る遷移があれば `NONE`、なければ states[0]
- 履歴: `emitted`（発生済み EVT）, `inbox`（配送待ちの `Agg.Cmd` メッセージ）, `fired`（発火済み POLICY）
- 配送: メッセージが現在状態から遷移できれば遷移（`to` が複数なら非決定）して EVT を発行、
  できなければ拒否（状態不変・EVT なし）。遷移を持たない CMD（属性変更など）は状態不変で EVT を発行
- POLICY: トリガー EVT が発生済みなら、cmd のメッセージを inbox に追加（非同期・遅延は任意）
- 利用者操作: scenario の cmd は、narrative の entry、`next` / `brs[].next` の先行 EVT 発生後、
  またはどの POLICY も発行しない自発的な操作（時刻駆動・外部起因）のとき発行できる
- EVT と遷移先の対応: scenario に brs があるとき、cmd の遷移を `to` の出現順でグループ化し、
  k 番目の brs の EVT を k 番目のグループに対応づける。余る brs は「状態不変で EVT だけ発生」
  （例: 最少催行人数に届かず確定しない）。対応づけられない場合は全 brs の EVT を許す過大近似
- liveness の公平性: Apalache は fairness 演算子に未対応のため、liveness 用に別の遷移 `stepLive` を出す。
  `stepLive` は「配送・POLICY 発火などシステム側の遷移が可能なら必ずそれを先に行い、
  なければ利用者操作、どちらもなければ停止（stutter）」という優先スケジューラ。
  仮定: 「メッセージは次の利用者操作より前に配送される」。つまり競合（配送中に別の操作が割り込む）は
  liveness 検査では消えるので、競合は safety 側（`step`）で検査する。停止状態を stutter にしているのは、
  行き詰まり（デッドロック）で性質が満たされないケースを違反として検出するため
- liveness の検査（quiescence 検査）: Apalache の temporal 検査は 100 アクション規模だと現実的な時間で
  終わらないため、`stepLive` の上では「システム側の遷移が尽きた（静止）状態で when が成り立つなら
  eventually も成り立つ」を safety として検査する（`quiet_<Name>` / `allQuiescent`）。
  静止状態に達したのに結果が出ていないなら本当の行き詰まりなので、反例になる。
  LTL の liveness（`live_<Name>`）も `temporal` として出力するが、現状は検査に使わない
- requires: scenarios / policies の `requires`（他集約の状態への実行前提）は配送の条件になる。
  満たされないときのメッセージは拒否される（状態不変・EVT なし）。自然文の `errs` / `rules` は読めない
- 制約: 1 インスタンス抽象のため、「申込 N 件のうち 1 件」のような多重度は表せない
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

INITIAL = "NONE"
MODULE = "DmlModel"


def ident(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", s)


def qstr(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def qset(items) -> str:
    items = sorted(set(items))
    return "Set(" + ", ".join(qstr(i) for i in items) + ")" if items else "Set()"


def _to_list(v: Any) -> list[str]:
    if isinstance(v, list):
        return [x for x in v if x]
    return [v] if v else []


class Model:
    """DML dict から変換に必要な情報を取り出す。"""

    def __init__(self, dml: dict, duplicates: bool = False):
        self.dml = dml
        self.duplicates = duplicates
        self.aggs: dict[str, dict] = {
            a["name"]: a for a in (dml.get("aggregates") or []) if isinstance(a, dict) and a.get("name")
        }
        self.scenarios = [s for s in (dml.get("scenarios") or []) if isinstance(s, dict)]
        self.policies = [p for p in (dml.get("policies") or []) if isinstance(p, dict)]
        self.invariants = [i for i in (dml.get("invariants") or []) if isinstance(i, dict)]
        self.stateful = {n: a for n, a in self.aggs.items() if a.get("states")}

    @staticmethod
    def msg(agg: str | None, cmd: str) -> str:
        return f"{agg or '_'}.{cmd}"

    def initial(self, agg: dict) -> str:
        if any(isinstance(t, dict) and t.get("from") == INITIAL for t in agg.get("transitions") or []):
            return INITIAL
        return (agg.get("states") or [INITIAL])[0]

    def transitions(self, agg_name: str | None, cmd: str) -> list[tuple[str, list[str]]]:
        agg = self.aggs.get(agg_name or "")
        if not agg:
            return []
        out = []
        for t in agg.get("transitions") or []:
            if isinstance(t, dict) and t.get("via") == cmd:
                out.append((t.get("from") or "", _to_list(t.get("to"))))
        return out

    def scenario_evts(self, sc: dict) -> list[str]:
        evs = [sc["evt"]] if sc.get("evt") else []
        for br in sc.get("brs") or []:
            if isinstance(br, dict) and br.get("evt"):
                evs.append(br["evt"])
        return evs

    def policy_msgs(self) -> set[str]:
        return {self.msg(p.get("agg"), p["cmd"]) for p in self.policies if p.get("cmd")}

    def evt_plan(self, agg_name: str | None, cmd: str) -> tuple[list[str], list[str]]:
        """(single_evts, brs_evts)。single は成功時に常に発行、brs は分岐ごとの結果 EVT（出現順）。"""
        single: list[str] = []
        brs: list[str] = []
        for sc in self.scenarios:
            if sc.get("cmd") == cmd and (sc.get("agg") or None) == (agg_name or None):
                if sc.get("evt"):
                    single.append(sc["evt"])
                for br in sc.get("brs") or []:
                    if isinstance(br, dict) and br.get("evt") and br["evt"] not in brs:
                        brs.append(br["evt"])
        for p in self.policies:
            if p.get("cmd") == cmd and (p.get("agg") or None) == (agg_name or None) and p.get("evt"):
                single.append(p["evt"])
        return single, brs

    def requires(self, agg_name: str | None, cmd: str) -> list[dict]:
        """(agg, cmd) の scenarios / policies に書かれた requires（重複は除く）。"""
        out: list[dict] = []
        for src in list(self.scenarios) + list(self.policies):
            if src.get("cmd") == cmd and (src.get("agg") or None) == (agg_name or None):
                for c in src.get("requires") or []:
                    if isinstance(c, dict) and c not in out:
                        out.append(c)
        return out

    def all_msgs(self) -> list[tuple[str | None, str]]:
        seen: list[tuple[str | None, str]] = []
        for src in list(self.scenarios) + list(self.policies):
            if src.get("cmd"):
                key = (src.get("agg") or None, src["cmd"])
                if key not in seen:
                    seen.append(key)
        return seen

    def issue_conditions(self) -> dict[int, str | None]:
        """scenario index → 発行可能条件の Quint 式（None=発行不可、"true"=常時）。"""
        entries = {n.get("entry") for n in (self.dml.get("narratives") or []) if isinstance(n, dict)}
        by_name = {s.get("name"): s for s in self.scenarios}
        incoming: dict[str, list[str]] = {}

        def add(target: Any, cond: str) -> None:
            if isinstance(target, str) and target in by_name:
                incoming.setdefault(target, []).append(cond)

        for sc in self.scenarios:
            evs = self.scenario_evts(sc)
            plain = "(" + " or ".join(f"emitted.contains({qstr(e)})" for e in evs) + ")" if evs else None
            nxt = sc.get("next")
            if plain:
                if isinstance(nxt, str):
                    add(nxt, plain)
                elif isinstance(nxt, dict):
                    for v in nxt.values():
                        add(v, plain)
            for br in sc.get("brs") or []:
                if isinstance(br, dict) and br.get("next") and br.get("evt"):
                    add(br["next"], f"emitted.contains({qstr(br['evt'])})")
        policy_msgs = self.policy_msgs()
        result: dict[int, str | None] = {}
        for i, sc in enumerate(self.scenarios):
            if not sc.get("cmd"):
                result[i] = None
                continue
            if sc.get("name") in entries:
                result[i] = "true"
            elif sc.get("name") in incoming:
                result[i] = "(" + " or ".join(sorted(set(incoming[sc["name"]]))) + ")"
            elif self.msg(sc.get("agg"), sc["cmd"]) not in policy_msgs:
                result[i] = "true"  # 時刻駆動・外部起因の自発的な操作
            else:
                result[i] = None  # POLICY 駆動
        return result


class Gen:
    def __init__(self, model: Model):
        self.m = model
        self.vars = [f"st_{ident(n)}" for n in model.stateful] + ["emitted", "inbox", "fired"]
        self.actions: list[str] = []
        self.sys_actions: list[str] = []
        self.env_actions: list[str] = []
        self.enabled: dict[str, str] = {}
        self.lines: list[str] = []
        self.env = False  # True の間に定義する action は利用者操作

    def action(self, name: str, guards: list[str], assigns: dict[str, str]) -> None:
        (self.env_actions if self.env else self.sys_actions).append(name)
        self.enabled[name] = "and { " + ", ".join(guards) + " }" if guards else "true"
        body = list(guards)
        for v in self.vars:
            body.append(f"{v}' = {assigns.get(v, v)}")
        self.lines.append(f"  action {name} = all {{")
        self.lines += [f"    {b}," for b in body[:-1]] + [f"    {body[-1]}"]
        self.lines.append("  }")
        self.lines.append("")
        self.actions.append(name)

    def cond(self, c: dict) -> str:
        agg = c.get("agg")
        if agg not in self.m.stateful:
            raise SystemExit(f"❌ invariant の agg '{agg}' が states を持つ aggregates[] に存在しません")
        v = f"st_{ident(agg)}"
        return "(" + " or ".join(f"{v} == {qstr(s)}" for s in c.get("in") or []) + ")"

    def emit(self, evts: list[str]) -> str:
        return f"emitted.union({qset(evts)})" if evts else "emitted"

    def build(self) -> str:
        m = self.m
        # --- 配送 ---
        for agg_name, cmd in m.all_msgs():
            msg = m.msg(agg_name, cmd)
            base = f"{ident(agg_name or '_')}_{ident(cmd)}"
            trs = m.transitions(agg_name, cmd)
            single, brs = m.evt_plan(agg_name, cmd)
            consume = {"inbox": f"inbox.exclude({qset([msg])})"} if not m.duplicates else {}
            guard_in = f"inbox.contains({qstr(msg)})"
            req = [self.cond(c) for c in m.requires(agg_name, cmd)]
            req_ok = [r for r in req]
            req_ng = ["not(and { " + ", ".join(req) + " })"] if req else []
            stv = f"st_{ident(agg_name)}" if agg_name in m.stateful else None
            if trs and stv:
                groups: list[tuple[str, ...]] = []
                for _, tos in trs:
                    key = tuple(sorted(tos))
                    if key not in groups:
                        groups.append(key)
                for k, (frm, tos) in enumerate(trs):
                    grp = groups.index(tuple(sorted(tos)))
                    evts = list(single)
                    if brs:
                        evts += [brs[grp]] if len(brs) >= len(groups) else brs
                    for ti, to in enumerate(tos):
                        self.action(
                            f"deliver_{base}_{k}_{ti}",
                            [guard_in, f"{stv} == {qstr(frm)}"] + req_ok,
                            {**consume, stv: qstr(to), "emitted": self.emit(evts)},
                        )
                froms = sorted({f for f, _ in trs})
                # 余った brs = 状態不変で EVT だけ発生（例: 条件未達で確定しない）
                if brs and len(brs) > len(groups):
                    for ei, ev in enumerate(brs[len(groups):]):
                        self.action(
                            f"deliver_{base}_noop_{ei}",
                            [guard_in, "(" + " or ".join(f"{stv} == {qstr(f)}" for f in froms) + ")"] + req_ok,
                            {**consume, "emitted": self.emit(list(single) + [ev])},
                        )
                # 現在状態から遷移できない → 拒否
                mismatch = "and { " + ", ".join(f"{stv} != {qstr(f)}" for f in froms) + " }"
                reject = f"or {{ {mismatch}, {req_ng[0]} }}" if req_ng else mismatch
                self.action(f"reject_{base}", [guard_in, reject], consume)
            else:
                # 遷移を持たない CMD（属性変更・副作用）: 状態不変で EVT を発行
                choices = brs if brs else [None]
                for ci, ch in enumerate(choices):
                    evts = list(single) + ([ch] if ch else [])
                    self.action(f"deliver_{base}_{ci}", [guard_in] + req_ok, {**consume, "emitted": self.emit(evts)})
                if req_ng:
                    self.action(f"reject_{base}", [guard_in] + req_ng, consume)

        # --- POLICY 発火 ---
        for i, p in enumerate(m.policies):
            name = p.get("name") or f"P{i}"
            trgs = p.get("trgs") or {}
            if p.get("trg"):
                trig = f"emitted.contains({qstr(p['trg'])})"
            elif isinstance(trgs, dict) and trgs.get("evts"):
                mode = (trgs.get("mode") or "all").lower()
                parts = [f"emitted.contains({qstr(e)})" for e in trgs["evts"]]
                trig = "(" + (" or " if mode in ("any", "or") else " and ").join(parts) + ")"
            else:
                continue
            assigns = {"fired": f"fired.union({qset([name])})"}
            if p.get("cmd"):
                assigns["inbox"] = f"inbox.union({qset([m.msg(p.get('agg'), p['cmd'])])})"
            self.action(f"fire_{ident(name)}", [trig, f"not(fired.contains({qstr(name)}))"], assigns)

        # --- 利用者操作 ---
        self.env = True
        conds = m.issue_conditions()
        for i, sc in enumerate(m.scenarios):
            c = conds.get(i)
            if not c:
                continue
            msg = m.msg(sc.get("agg"), sc["cmd"])
            guards = [] if c == "true" else [c]
            guards.append(f"not(inbox.contains({qstr(msg)}))")
            self.action(f"issue_{i}_{ident(sc['cmd'])}", guards, {"inbox": f"inbox.union({qset([msg])})"})

        # --- 組み立て ---
        out = [
            "// 自動生成: scripts/dml_to_quint.py（手で編集しない）",
            f"module {MODULE} {{",
        ]
        for n in m.stateful:
            out.append(f"  var st_{ident(n)}: str")
        out += ["  var emitted: Set[str]", "  var inbox: Set[str]", "  var fired: Set[str]", ""]
        init = [f"st_{ident(n)}' = {qstr(m.initial(a))}" for n, a in m.stateful.items()]
        init += ["emitted' = Set()", "inbox' = Set()", "fired' = Set()"]
        out.append("  action init = all {")
        out += [f"    {x}," for x in init[:-1]] + [f"    {init[-1]}"]
        out += ["  }", ""]
        out += self.lines
        out.append("  action step = any {")
        out += [f"    {a}," for a in self.actions[:-1]] + ([f"    {self.actions[-1]}"] if self.actions else ["    all { true }"])
        out += ["  }", ""]

        # liveness 用の優先スケジューラ（システム遷移 → 利用者操作 → 停止）
        def any_of(names: list[str], prefix: str) -> list[str]:
            return [f"  val {prefix}_{n} = {self.enabled[n]}" for n in names]

        out += any_of(self.sys_actions, "en") + any_of(self.env_actions, "en")
        sys_en = "or { " + ", ".join(f"en_{n}" for n in self.sys_actions) + " }" if self.sys_actions else "false"
        env_en = "or { " + ", ".join(f"en_{n}" for n in self.env_actions) + " }" if self.env_actions else "false"
        out.append(f"  val sysEnabled = {sys_en}")
        out.append(f"  val envEnabled = {env_en}")
        unchanged = ", ".join(f"{v}' = {v}" for v in self.vars)
        sys_step = "any { " + ", ".join(self.sys_actions) + " }" if self.sys_actions else "all { false }"
        env_step = "any { " + ", ".join(self.env_actions) + " }" if self.env_actions else "all { false }"
        out += [
            "  action stepLive = any {",
            f"    all {{ sysEnabled, {sys_step} }},",
            f"    all {{ not(sysEnabled), envEnabled, {env_step} }},",
            f"    all {{ not(sysEnabled), not(envEnabled), {unchanged} }}",
            "  }",
            "",
        ]

        safeties, quiets = [], []
        for inv in m.invariants:
            nm = ident(inv.get("name") or "Unnamed")
            if inv.get("kind") == "safety":
                body = ", ".join(self.cond(c) for c in inv.get("never") or [])
                out.append(f"  val inv_{nm} = not(and {{ {body} }})")
                safeties.append(f"inv_{nm}")
            elif inv.get("kind") == "liveness":
                w, e = self.cond(inv["when"]), self.cond(inv["eventually"])
                out.append(f"  temporal live_{nm} = always({w} implies eventually({e}))")
                out.append(f"  val quiet_{nm} = not(sysEnabled) implies (not({w}) or {e})")
                quiets.append(f"quiet_{nm}")
        out.append("  val allInvariants = " + ("and { " + ", ".join(safeties) + " }" if safeties else "true"))
        out.append("  val allQuiescent = " + ("and { " + ", ".join(quiets) + " }" if quiets else "true"))
        out += ["}", ""]
        return "\n".join(out)


def generate(dml: dict, duplicates: bool = False) -> str:
    return Gen(Model(dml, duplicates)).build()


def find_quint() -> list[str] | None:
    env = os.environ.get("QUINT_BIN")
    if env:
        return [env]
    exe = shutil.which("quint")
    return [exe] if exe else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DML → Quint 変換（モデル検査用）")
    ap.add_argument("file")
    ap.add_argument("--out", help="出力 .qnt（省略時 dist/verify/<name>.qnt）")
    ap.add_argument("--duplicates", action="store_true", help="メッセージの重複配信（at-least-once）を許す")
    ap.add_argument("--run", action="store_true", help="quint run で safety を検査する")
    ap.add_argument("--verify", action="store_true", help="quint verify（Apalache）で safety と liveness を検査する")
    ap.add_argument("--verify-steps", type=int, default=12, help="--verify の探索ステップ数上限")
    ap.add_argument("--max-steps", type=int, default=30)
    ap.add_argument("--max-samples", type=int, default=6000)
    args = ap.parse_args(argv)

    src = Path(args.file)
    dml = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
    qnt = generate(dml, args.duplicates)
    out = Path(args.out) if args.out else Path("dist/verify") / (src.name.split(".")[0] + ".qnt")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(qnt, encoding="utf-8")
    print(f"✅ 生成: {out}（invariants {len(dml.get('invariants') or [])} 件）")
    if not (args.run or args.verify):
        return 0
    quint = find_quint()
    if not quint:
        print("❌ quint が見つかりません（`npm i -g @informalsystems/quint` または QUINT_BIN を指定）", file=sys.stderr)
        return 2
    rc = 0
    has_live = any(isinstance(i, dict) and i.get("kind") == "liveness" for i in dml.get("invariants") or [])
    # (ラベル, 不変条件名, 遷移名)
    jobs = [("safety（step 上の allInvariants）", "allInvariants", "step")]
    if has_live:
        jobs.append(("liveness（stepLive 上の allQuiescent）", "allQuiescent", "stepLive"))
    for label, inv_name, step in jobs:
        if args.run:
            print(f"▶ quint run {label}")
            rc = subprocess.run(
                quint + ["run", str(out), f"--main={MODULE}", f"--invariant={inv_name}", f"--step={step}",
                         f"--max-steps={args.max_steps}", f"--max-samples={args.max_samples}"],
                check=False).returncode or rc
        if args.verify:
            print(f"▶ quint verify {label}（{args.verify_steps} ステップまで網羅）")
            rc = subprocess.run(
                quint + ["verify", str(out), f"--main={MODULE}", f"--invariant={inv_name}", f"--step={step}",
                         f"--max-steps={args.verify_steps}"],
                check=False).returncode or rc
    return rc


if __name__ == "__main__":
    sys.exit(main())
