#!/usr/bin/env python3
"""DML → Quint（モデル検査用の状態遷移系）変換。スパイク版。

集約・集約間連携（POLICY 連鎖）を非同期メッセージの状態遷移系に落とし、
`invariants[]`（safety / liveness）を Quint の性質として書き出す。

使い方:
    python3 scripts/dml_to_quint.py <file.dml.yaml> [--out <file.qnt>] [--duplicates]
    python3 scripts/dml_to_quint.py <file.dml.yaml> --run [--max-steps N] [--max-samples N] [--backend auto|rust|typescript]
    python3 scripts/dml_to_quint.py <file.dml.yaml> --verify [--verify-steps N]

--run   は Quint CLI（`quint`、または環境変数 QUINT_BIN）で `quint run`（ランダムシミュレーション）を実行する。
        safety は `step` 上の allInvariants、liveness は `stepLive` 上の allQuiescent（下記）を検査する。
        --backend は quint run の評価器。既定の auto は rust（速い）で走らせ、評価器を取得できずに
        結果が出なかったとき（ネットワーク制限で GitHub から落とせない等）だけ typescript でやり直す。
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
- requires: scenarios / policies の `requires`（他集約の状態への実行前提）は配送の条件になる。
  満たされないときのメッセージは拒否される（状態不変・EVT なし）。自然文の `errs` / `rules` は読めない
- liveness の検査（quiescence 検査）: Apalache は fairness 演算子に未対応で temporal 検査も
  この規模では終わらないため、別の遷移 `stepLive` の上で「システム側の遷移が尽きた（静止）状態で
  when が成り立つなら eventually も成り立つ」を safety として検査する（`quiet_<Name>` / `allQuiescent`）。
  `stepLive` は「配送・POLICY 発火などシステム側の遷移が可能なら必ずそれを先に行い、なければ
  利用者操作、どちらもなければ停止（stutter）」という優先スケジューラ。静止したのに結果が出ていなければ
  本当の行き詰まりなので反例になる。停止を stutter にしているのは、デッドロックで性質が満たされない
  ケースを違反として検出するため
  仮定: 「メッセージは次の利用者操作より前に配送される」。つまり競合（配送中に別の操作が割り込む）は
  liveness 検査では消えるので、競合は safety 側（`step`）で検査する
- 制約: 1 インスタンス抽象のため、「申込 N 件のうち 1 件」のような多重度は表せない
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from itertools import chain
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dml_filters.shared import INITIAL_STATE, policy_triggers, scenario_evts, to_list  # noqa: E402

MODULE = "DmlModel"
DEFAULT_MAX_STEPS = 30
DEFAULT_MAX_SAMPLES = 6000  # 稀な経路（受付開始後の中止など）を見逃しにくい件数
DEFAULT_VERIFY_STEPS = 12

Key = tuple[Any, str]  # (agg 名 or None, cmd 名)


def ident(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", s)


def qstr(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def qset(items) -> str:
    items = sorted(set(items))
    return "Set(" + ", ".join(qstr(i) for i in items) + ")" if items else "Set()"


class Model:
    """DML dict から変換に必要な情報を取り出す（索引は初期化時に 1 回だけ作る）。"""

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

        # (agg, cmd) → scenario / policy
        self._sc: dict[Key, list[dict]] = defaultdict(list)
        self._pol: dict[Key, list[dict]] = defaultdict(list)
        for sc in self.scenarios:
            if sc.get("cmd"):
                self._sc[self.key(sc)].append(sc)
        for p in self.policies:
            if p.get("cmd"):
                self._pol[self.key(p)].append(p)
        self.msgs: list[Key] = list(dict.fromkeys(chain(self._sc, self._pol)))

        # (agg, cmd) → [(from, [to...])]
        self._trs: dict[Key, list[tuple[str, list[str]]]] = defaultdict(list)
        for name, agg in self.aggs.items():
            for t in agg.get("transitions") or []:
                if isinstance(t, dict) and t.get("via"):
                    self._trs[(name, t["via"])].append((t.get("from") or "", to_list(t.get("to"))))

    @staticmethod
    def key(src: dict) -> Key:
        return (src.get("agg") or None, src["cmd"])

    @staticmethod
    def msg(key: Key) -> str:
        return f"{key[0] or '_'}.{key[1]}"

    @staticmethod
    def initial(agg: dict) -> str:
        if any(isinstance(t, dict) and t.get("from") == INITIAL_STATE for t in agg.get("transitions") or []):
            return INITIAL_STATE
        return (agg.get("states") or [INITIAL_STATE])[0]

    def transitions(self, key: Key) -> list[tuple[str, list[str]]]:
        return self._trs.get(key, [])

    def evt_plan(self, key: Key) -> tuple[list[str], list[str]]:
        """(single_evts, brs_evts)。single は成功時に常に発行、brs は分岐ごとの結果 EVT（出現順）。"""
        single: list[str] = []
        brs: dict[str, None] = {}
        for sc in self._sc.get(key, []):
            if sc.get("evt"):
                single.append(sc["evt"])
            brs.update(dict.fromkeys(e for e in scenario_evts(sc) if e != sc.get("evt")))
        single += [p["evt"] for p in self._pol.get(key, []) if p.get("evt")]
        return single, list(brs)

    def requires(self, key: Key) -> list[dict]:
        """(agg, cmd) の scenarios / policies に書かれた requires（重複は除く）。"""
        out: list[dict] = []
        for src in chain(self._sc.get(key, []), self._pol.get(key, [])):
            out += [c for c in src.get("requires") or [] if isinstance(c, dict) and c not in out]
        return out

    def issue_conditions(self) -> dict[int, str]:
        """発行可能な scenario の index → 発行条件の Quint 式（"true"=常時）。POLICY 駆動は含まない。"""
        entries = {n.get("entry") for n in (self.dml.get("narratives") or []) if isinstance(n, dict)}
        names = {s.get("name") for s in self.scenarios}
        incoming: dict[str, set[str]] = defaultdict(set)

        def add(target: Any, cond: str) -> None:
            if isinstance(target, str) and target in names:
                incoming[target].add(cond)

        for sc in self.scenarios:
            evs = scenario_evts(sc)
            nxt = sc.get("next")
            targets = [nxt] if isinstance(nxt, str) else list(nxt.values()) if isinstance(nxt, dict) else []
            if evs:
                plain = "(" + " or ".join(f"emitted.contains({qstr(e)})" for e in evs) + ")"
                for target in targets:
                    add(target, plain)
            for br in sc.get("brs") or []:
                if isinstance(br, dict) and br.get("next") and br.get("evt"):
                    add(br["next"], f"emitted.contains({qstr(br['evt'])})")
        result: dict[int, str] = {}
        for i, sc in enumerate(self.scenarios):
            if not sc.get("cmd"):
                continue
            if sc.get("name") in entries:
                result[i] = "true"
            elif sc.get("name") in incoming:
                result[i] = "(" + " or ".join(sorted(incoming[sc["name"]])) + ")"
            elif self.key(sc) not in self._pol:
                result[i] = "true"  # 時刻駆動・外部起因の自発的な操作
        return result


class Gen:
    def __init__(self, model: Model):
        self.m = model
        self.vars = [f"st_{ident(n)}" for n in model.stateful] + ["emitted", "inbox", "fired"]
        self.sys_actions: list[str] = []
        self.env_actions: list[str] = []
        self.enabled: dict[str, str] = {}
        self.lines: list[str] = []

    @staticmethod
    def items(xs: list[str], indent: str = "    ") -> list[str]:
        """Quint の all / any の要素列（最後だけカンマ無し）。"""
        return [indent + x + ("," if i < len(xs) - 1 else "") for i, x in enumerate(xs)]

    def action(self, name: str, guards: list[str], assigns: dict[str, str], env: bool = False) -> None:
        (self.env_actions if env else self.sys_actions).append(name)
        self.enabled[name] = "and { " + ", ".join(guards) + " }" if guards else "true"
        body = list(guards) + [f"{v}' = {assigns.get(v, v)}" for v in self.vars]
        self.lines += [f"  action {name} = all {{", *self.items(body), "  }", ""]

    def cond(self, c: dict) -> str:
        agg = c.get("agg")
        if agg not in self.m.stateful:
            raise SystemExit(f"❌ 条件の agg '{agg}' が states を持つ aggregates[] に存在しません")
        v = f"st_{ident(agg)}"
        return "(" + " or ".join(f"{v} == {qstr(s)}" for s in c.get("in") or []) + ")"

    @staticmethod
    def emit(evts: list[str]) -> str:
        return f"emitted.union({qset(evts)})" if evts else "emitted"

    def delivery(self, key: Key) -> None:
        m = self.m
        agg_name, cmd = key
        msg = m.msg(key)
        base = f"{ident(agg_name or '_')}_{ident(cmd)}"
        trs = m.transitions(key)
        single, brs = m.evt_plan(key)
        consume = {} if m.duplicates else {"inbox": f"inbox.exclude({qset([msg])})"}
        guard_in = f"inbox.contains({qstr(msg)})"
        req = [self.cond(c) for c in m.requires(key)]
        req_ng = ["not(and { " + ", ".join(req) + " })"] if req else []
        stv = f"st_{ident(agg_name)}" if agg_name in m.stateful else None

        if trs and stv:
            group_of: dict[tuple[str, ...], int] = {}
            for _, tos in trs:
                group_of.setdefault(tuple(sorted(tos)), len(group_of))
            for k, (frm, tos) in enumerate(trs):
                evts = list(single)
                if brs:
                    evts += [brs[group_of[tuple(sorted(tos))]]] if len(brs) >= len(group_of) else brs
                for ti, to in enumerate(tos):
                    self.action(
                        f"deliver_{base}_{k}_{ti}",
                        [guard_in, f"{stv} == {qstr(frm)}"] + req,
                        {**consume, stv: qstr(to), "emitted": self.emit(evts)},
                    )
            froms = sorted({f for f, _ in trs})
            # 余った brs = 状態不変で EVT だけ発生（例: 条件未達で確定しない）
            for ei, ev in enumerate(brs[len(group_of):]):
                self.action(
                    f"deliver_{base}_noop_{ei}",
                    [guard_in, "(" + " or ".join(f"{stv} == {qstr(f)}" for f in froms) + ")"] + req,
                    {**consume, "emitted": self.emit(single + [ev])},
                )
            # 現在状態から遷移できない、または前提を満たさない → 拒否
            mismatch = "and { " + ", ".join(f"{stv} != {qstr(f)}" for f in froms) + " }"
            reject = f"or {{ {mismatch}, {req_ng[0]} }}" if req_ng else mismatch
            self.action(f"reject_{base}", [guard_in, reject], consume)
        else:
            # 遷移を持たない CMD（属性変更・副作用）: 状態不変で EVT を発行
            for ci, ch in enumerate(brs or [None]):
                evts = single + ([ch] if ch else [])
                self.action(f"deliver_{base}_{ci}", [guard_in] + req, {**consume, "emitted": self.emit(evts)})
            if req_ng:
                self.action(f"reject_{base}", [guard_in] + req_ng, consume)

    def policy_fire(self, i: int, p: dict) -> None:
        name = p.get("name") or f"P{i}"
        events, mode = policy_triggers(p)
        if not events:
            return
        parts = [f"emitted.contains({qstr(e)})" for e in events]
        trig = parts[0] if len(parts) == 1 else "(" + (" or " if mode == "any" else " and ").join(parts) + ")"
        assigns = {"fired": f"fired.union({qset([name])})"}
        if p.get("cmd"):
            assigns["inbox"] = f"inbox.union({qset([self.m.msg(self.m.key(p))])})"
        self.action(f"fire_{ident(name)}", [trig, f"not(fired.contains({qstr(name)}))"], assigns)

    def build(self) -> str:
        m = self.m
        for key in m.msgs:
            self.delivery(key)
        for i, p in enumerate(m.policies):
            self.policy_fire(i, p)
        for i, cond in m.issue_conditions().items():
            sc = m.scenarios[i]
            msg = m.msg(m.key(sc))
            guards = ([] if cond == "true" else [cond]) + [f"not(inbox.contains({qstr(msg)}))"]
            self.action(f"issue_{i}_{ident(sc['cmd'])}", guards, {"inbox": f"inbox.union({qset([msg])})"}, env=True)

        allacts = self.sys_actions + self.env_actions
        out = ["// 自動生成: scripts/dml_to_quint.py（手で編集しない）", f"module {MODULE} {{"]
        out += [f"  var st_{ident(n)}: str" for n in m.stateful]
        out += ["  var emitted: Set[str]", "  var inbox: Set[str]", "  var fired: Set[str]", ""]
        init = [f"st_{ident(n)}' = {qstr(m.initial(a))}" for n, a in m.stateful.items()]
        init += ["emitted' = Set()", "inbox' = Set()", "fired' = Set()"]
        out += ["  action init = all {", *self.items(init), "  }", ""]
        out += self.lines
        out += ["  action step = any {", *self.items(allacts or ["all { true }"]), "  }", ""]

        # liveness 用の優先スケジューラ（システム遷移 → 利用者操作 → 停止）
        out += [f"  val en_{n} = {self.enabled[n]}" for n in allacts]
        sys_en = "or { " + ", ".join(f"en_{n}" for n in self.sys_actions) + " }" if self.sys_actions else "false"
        env_en = "or { " + ", ".join(f"en_{n}" for n in self.env_actions) + " }" if self.env_actions else "false"
        out += [f"  val sysEnabled = {sys_en}", f"  val envEnabled = {env_en}"]
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


def load_dml(path: Path) -> dict:
    """空ファイル・非 dict は {}、YAML 構文エラーは dmlctl と同様に exit 2。"""
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        print(f"❌ YAML 解析失敗: {e}", file=sys.stderr)
        raise SystemExit(2)
    return loaded if isinstance(loaded, dict) else {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DML → Quint 変換（モデル検査用）")
    ap.add_argument("file")
    ap.add_argument("--out", help="出力 .qnt（省略時 dist/verify/<name>.qnt）")
    ap.add_argument("--duplicates", action="store_true", help="メッセージの重複配信（at-least-once）を許す")
    ap.add_argument("--run", action="store_true", help="quint run で safety / liveness を検査する")
    ap.add_argument("--verify", action="store_true", help="quint verify（Apalache）で safety / liveness を検査する")
    ap.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    ap.add_argument("--max-samples", type=int, default=DEFAULT_MAX_SAMPLES)
    ap.add_argument("--verify-steps", type=int, default=DEFAULT_VERIFY_STEPS, help="--verify の探索ステップ数上限")
    ap.add_argument("--backend", choices=["auto", "rust", "typescript"], default="auto",
                    help="quint run の評価器（auto: rust で結果が出なければ typescript でやり直す）")
    args = ap.parse_args(argv)

    src = Path(args.file)
    dml = load_dml(src)
    out = Path(args.out) if args.out else Path("dist/verify") / (src.name.split(".")[0] + ".qnt")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(generate(dml, args.duplicates), encoding="utf-8")
    print(f"✅ 生成: {out}（invariants {len(dml.get('invariants') or [])} 件）")
    if not (args.run or args.verify):
        return 0
    quint = find_quint()
    if not quint:
        print("❌ quint が見つかりません（`npm i -g @informalsystems/quint` または QUINT_BIN を指定）", file=sys.stderr)
        return 2

    has_live = any(isinstance(i, dict) and i.get("kind") == "liveness" for i in dml.get("invariants") or [])
    jobs = [("safety（step 上の allInvariants）", "allInvariants", "step")]
    if has_live:
        jobs.append(("liveness（stepLive 上の allQuiescent）", "allQuiescent", "stepLive"))
    modes = []
    if args.run:
        modes.append(("run", f"--max-steps={args.max_steps}", f"--max-samples={args.max_samples}"))
    if args.verify:
        modes.append(("verify", f"--max-steps={args.verify_steps}"))
    rc = 0
    for label, inv_name, step in jobs:
        for sub, *opts in modes:
            print(f"▶ quint {sub} {label}", flush=True)
            cmd = quint + [sub, str(out), f"--main={MODULE}", f"--invariant={inv_name}", f"--step={step}", *opts]
            if sub == "run":
                rc = run_simulation(cmd, args.backend) or rc
            else:
                rc = subprocess.run(cmd, check=False).returncode or rc
    return rc


# quint run が検査結果を出したかどうかの目印（[ok] / [violation]）。どちらも無く失敗したら
# 評価器そのものが動かなかったと見なす（rust 評価器は初回に GitHub から取得するため、
# ネットワーク制限のある環境では結果を出す前に落ちる）
_RESULT_MARK = re.compile(r"^\[(ok|violation)\]", re.MULTILINE)


def run_simulation(cmd: list[str], backend: str) -> int:
    """quint run を評価器を選んで実行し、終了コードを返す。

    auto は rust で走らせ、結果が出ずに失敗したときだけ typescript でやり直す。"""
    if backend != "auto":
        return subprocess.run(cmd + [f"--backend={backend}"], check=False).returncode
    first = subprocess.run(cmd + ["--backend=rust"], check=False, capture_output=True, text=True)
    sys.stdout.write(first.stdout)
    sys.stderr.write(first.stderr)
    if first.returncode == 0 or _RESULT_MARK.search(first.stdout + first.stderr):
        return first.returncode
    print("⚠ rust の評価器で検査結果が出ませんでした（評価器を取得できない環境の可能性）。typescript でやり直します",
          file=sys.stderr, flush=True)
    return subprocess.run(cmd + ["--backend=typescript"], check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
