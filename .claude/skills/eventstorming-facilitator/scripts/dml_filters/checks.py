"""DML 構造チェック観点別関数群。

各関数は `model: dict` を入力に取り、違反一覧（list[Finding]）を返す。違反 0 件＝合格。

これらは **LLM を呼ばない純構造チェック**。意味チェック（観点別 LLM プロンプト）は
`references/checks/*.md` を Agent から起動する形で行う。両者の連携は
`references/quality-check.md` を参照。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .flow_walk import index_policies_by_trg
from .shared import INITIAL_STATE as INITIAL_PSEUDO_STATE
from .shared import PHASES, policy_triggers, scenario_evts
from .shared import to_list as _to_list


# ============================================================
# 共通: Finding 構造
# ============================================================


@dataclass
class Finding:
    kind: str                              # チェック名（orphan_agg / dangling_cmd / ...）
    path: str                              # 違反箇所の YAML パス（例: aggregates[2]）
    message: str                           # 違反内容の日本語サマリ
    slice: dict[str, Any] = field(default_factory=dict)  # LLM への補助スライス（任意）
    # "error"（既定・check の終了コードに影響）/ "warn"（報告のみ・終了コードに影響しない）
    severity: str = "error"


def finding_to_dict(f: Finding) -> dict[str, Any]:
    return {
        "kind": f.kind,
        "severity": f.severity,
        "path": f.path,
        "message": f.message,
        "slice": f.slice,
    }


def _iter_ctx_lang(model: dict):
    """contexts[] を走査し、dict 形の lang を持つ要素だけ `(i, ctx, lang)` を yield する。

    `lang.{cmds,evts,pols,...}` を横断するチェックが繰り返す
    「isinstance ガード → lang 取得 → isinstance ガード」の共通プリアンブルを集約。
    ctx 名の導出（`ctx.get("name") or f"#{i}"` 等）は用途がまちまちなので呼び出し側に残す。
    """
    for i, ctx in enumerate(model.get("contexts") or []):
        if not isinstance(ctx, dict):
            continue
        lang = ctx.get("lang") or {}
        if not isinstance(lang, dict):
            continue
        yield i, ctx, lang


def _scenario_events(model: dict) -> set[str]:
    """scenarios[].evt と scenarios[].brs[].evt に現れる EVT 名の集合。"""
    out: set[str] = set()
    for s in model.get("scenarios") or []:
        if isinstance(s, dict):
            out.update(scenario_evts(s))
    return out


def _policy_events(model: dict) -> set[str]:
    """policies[] がトリガー/発行する EVT 名の集合（trg / trgs.evts / evt）。"""
    out: set[str] = set()
    for p in model.get("policies") or []:
        if not isinstance(p, dict):
            continue
        if p.get("trg"):
            out.add(p["trg"])
        if p.get("evt"):
            out.add(p["evt"])
        trgs = p.get("trgs") or {}
        if isinstance(trgs, dict):
            for ev in trgs.get("evts") or []:
                out.add(ev)
    return out


def _aggregate_events(model: dict) -> set[str]:
    """aggregates[].events[].name に宣言された EVT 名の集合。"""
    out: set[str] = set()
    for a in model.get("aggregates") or []:
        if isinstance(a, dict):
            out |= _agg_event_names(a)
    return out


# ============================================================
# 個別チェック
# ============================================================


def orphan_agg(model: dict) -> list[Finding]:
    """どの scenarios[].agg からも参照されない AGG を列挙する。"""
    aggregates = model.get("aggregates") or []
    scenarios = model.get("scenarios") or []
    policies = model.get("policies") or []
    referenced: set[str] = set()
    for s in scenarios:
        if isinstance(s, dict) and s.get("agg"):
            referenced.add(s["agg"])
    # policies は AGG ではなく EVT/CMD を持つので scenarios だけ走査するが、
    # AGG を `policies[].agg` で明示するスキーマ拡張時のため一応見る（v5 では未定義 OK）。
    for p in policies:
        if isinstance(p, dict) and p.get("agg"):
            referenced.add(p["agg"])

    findings: list[Finding] = []
    for i, agg in enumerate(aggregates):
        if not isinstance(agg, dict):
            continue
        name = agg.get("name") or ""
        if name and name not in referenced:
            findings.append(
                Finding(
                    kind="orphan_agg",
                    path=f"aggregates[{i}]",
                    message=f"AGG '{name}' はどの scenario からも参照されていません",
                    slice={"agg": name, "ctx": agg.get("ctx")},
                )
            )
    return findings


def dangling_cmd(model: dict) -> list[Finding]:
    """aggregates[].transitions[].via が scenarios[].cmd / policies[].cmd に一致しない場合に警告する。

    v7: 内部 CMD（時刻駆動・外部コールバック・副作用）は scenario でなく policy.cmd として
    宣言されるケースも実体として正当なので、両方をチェック対象に含める。
    """
    scenarios = model.get("scenarios") or []
    policies = model.get("policies") or []
    aggregates = model.get("aggregates") or []
    declared_cmds: set[str] = {
        s["cmd"] for s in scenarios if isinstance(s, dict) and s.get("cmd")
    }
    declared_cmds |= {
        p["cmd"] for p in policies if isinstance(p, dict) and p.get("cmd")
    }
    findings: list[Finding] = []
    for i, agg in enumerate(aggregates):
        if not isinstance(agg, dict):
            continue
        agg_name = agg.get("name") or ""
        transitions = agg.get("transitions") or []
        for j, tr in enumerate(transitions):
            if not isinstance(tr, dict):
                continue
            via = tr.get("via")
            if via and via not in declared_cmds:
                findings.append(
                    Finding(
                        kind="dangling_cmd",
                        path=f"aggregates[{i}].transitions[{j}].via",
                        message=f"AGG '{agg_name}' の遷移 via '{via}' が scenarios[].cmd に未宣言",
                        slice={"agg": agg_name, "via": via, "transition": tr},
                    )
                )
    return findings


def unknown_evt_in_policy(model: dict) -> list[Finding]:
    """policies[].trg / policies[].trgs.evts が aggregates[].events[].name に未宣言ならフラグ。"""
    # aggregates[].events に加え、scenarios[].evt / brs[].evt も宣言済み扱い
    # （実運用で aggregates に書き忘れる例があるため）
    declared_evts = _aggregate_events(model) | _scenario_events(model)

    findings: list[Finding] = []
    for i, pol in enumerate(model.get("policies") or []):
        if not isinstance(pol, dict):
            continue
        name = pol.get("name") or ""
        trg = pol.get("trg")
        if trg and trg not in declared_evts:
            findings.append(
                Finding(
                    kind="unknown_evt_in_policy",
                    path=f"policies[{i}].trg",
                    message=f"POL '{name}' のトリガー '{trg}' が EVT として宣言されていません",
                    slice={"policy": name, "trg": trg},
                )
            )
        trgs = pol.get("trgs") or {}
        if isinstance(trgs, dict):
            for k, ev in enumerate(trgs.get("evts") or []):
                if ev and ev not in declared_evts:
                    findings.append(
                        Finding(
                            kind="unknown_evt_in_policy",
                            path=f"policies[{i}].trgs.evts[{k}]",
                            message=f"POL '{name}' の join トリガー '{ev}' が EVT として宣言されていません",
                            slice={"policy": name, "evt": ev},
                        )
                    )
    return findings


def language_coverage(model: dict) -> list[Finding]:
    """`contexts[].lang` に登録されていない名前付き要素を列挙する。

    語彙辞書の網羅性チェック。HTML フロー図のラベル日本語化に使うため、
    scenarios[].cmd / scenarios[].evt / policies[].name / aggregates[].name / aggregates[].events[].name が
    どこかの contexts[].lang.{cmds/evts/pols/aggs} に登録されているか確認する。
    """
    registered: set[str] = set()
    for _i, _ctx, lang in _iter_ctx_lang(model):
        for cat in ("aggs", "actors", "cmds", "evts", "pols", "qrys", "vos"):
            cat_dict = lang.get(cat) or {}
            if isinstance(cat_dict, dict):
                registered.update(cat_dict.keys())

    findings: list[Finding] = []
    reported: set[str] = set()  # warn は同じ識別子を 1 回だけ報告する

    def _check(items: list, getter, kind_label: str, path_prefix: str,
               *, severity: str = "error", suffix: str = "") -> None:
        for i, it in enumerate(items or []):
            if not isinstance(it, dict):
                continue
            names = getter(it)
            for name in names if isinstance(names, list) else [names]:
                if not isinstance(name, str) or not name or name in registered:
                    continue
                if severity == "warn":
                    if name in reported:
                        continue
                    reported.add(name)
                findings.append(
                    Finding(
                        kind="language_coverage",
                        path=f"{path_prefix}[{i}]{suffix}",
                        message=f"{kind_label} '{name}' が contexts[].lang に未登録",
                        slice={"identifier": name, "kind": kind_label},
                        severity=severity,
                    )
                )

    scenarios = model.get("scenarios") or []
    policies = model.get("policies") or []
    _check(model.get("aggregates") or [], lambda x: x.get("name"), "AGG", "aggregates")
    _check(scenarios, lambda x: x.get("cmd"), "CMD", "scenarios")
    _check(scenarios, lambda x: x.get("evt"), "EVT", "scenarios")
    _check(policies, lambda x: x.get("name"), "POL", "policies")
    # 以下はフロー図の付箋に出る識別子。既存 DML の終了コードを変えないよう warn にとどめる。
    _check(scenarios, lambda x: x.get("actor"), "ACTOR", "scenarios", severity="warn", suffix=".actor")
    _check(scenarios, lambda x: [b.get("evt") for b in x.get("brs") or [] if isinstance(b, dict)],
           "EVT", "scenarios", severity="warn", suffix=".brs")
    _check(scenarios, lambda x: _to_list(x.get("qry")), "QRY", "scenarios", severity="warn", suffix=".qry")
    _check(policies, lambda x: [x.get("evt")] + _to_list(x.get("qry")), "EVT/QRY", "policies", severity="warn")
    return findings


def state_reachability(model: dict) -> list[Finding]:
    """aggregates[].transitions による状態到達可能性を検査する。

    `aggregates[].states` のうち、遷移グラフから到達できない（=どの to にも現れない）状態を
    「孤立状態」として報告する。ただし AGG の初期状態（先頭 state）は除外する。
    """
    findings: list[Finding] = []
    for i, agg in enumerate(model.get("aggregates") or []):
        if not isinstance(agg, dict):
            continue
        states = agg.get("states") or []
        if not states:
            continue
        initial = states[0] if states else None
        targets: set[str] = set()
        for tr in agg.get("transitions") or []:
            if not isinstance(tr, dict):
                continue
            targets.update(_to_list(tr.get("to")))

        for state in states:
            if state == initial:
                continue
            if state not in targets:
                findings.append(
                    Finding(
                        kind="state_reachability",
                        path=f"aggregates[{i}].states",
                        message=f"AGG '{agg.get('name')}' の状態 '{state}' へ遷移する transitions[] が見つかりません",
                        slice={"agg": agg.get("name"), "unreachable": state},
                    )
                )
    return findings


def orphan_event(model: dict) -> list[Finding]:
    """aggregates[].events で宣言されたが、どの scenarios/policies からも参照されていない EVT。

    v7: `events[].terminal: true` のイベントは「業務的にここで止まる」終端イベント
    （失敗系・タイムアウト系など）として orphan 判定から除外する。
    """
    referenced = _scenario_events(model) | _policy_events(model)

    findings: list[Finding] = []
    for i, agg in enumerate(model.get("aggregates") or []):
        if not isinstance(agg, dict):
            continue
        for j, ev in enumerate(agg.get("events") or []):
            if not isinstance(ev, dict):
                continue
            name = ev.get("name") or ""
            if not name or name in referenced:
                continue
            if ev.get("terminal") is True:
                continue  # v7: 明示終端は orphan 扱いしない
            findings.append(
                Finding(
                    kind="orphan_event",
                    path=f"aggregates[{i}].events[{j}]",
                    message=f"EVT '{name}' は AGG '{agg.get('name')}' で emit 宣言されているが参照されていません",
                    slice={"agg": agg.get("name"), "evt": name},
                )
            )
    return findings


def flow_chain_resolution(model: dict) -> list[Finding]:
    """フロー連鎖（v6）の参照整合性チェック。

    検出対象:
      - narratives[].entry が scenarios[].name に解決できない
      - scenarios[].next（string / dict 値）が scenarios[].name に解決できない
      - scenarios[].next が dict 形式の時、キーが narratives[].id に存在しない
      - scenarios[].brs[].next が scenarios[].name に解決できない
      - scenarios[].brs[].terminal が narratives[].id に存在しない
    """
    scs_names = {s.get("name") for s in (model.get("scenarios") or []) if isinstance(s, dict)}
    narrative_ids = {
        n.get("id") for n in (model.get("narratives") or []) if isinstance(n, dict)
    }

    findings: list[Finding] = []

    for i, n in enumerate(model.get("narratives") or []):
        if not isinstance(n, dict):
            continue
        entry = n.get("entry")
        nid = n.get("id") or ""
        if entry and entry not in scs_names:
            findings.append(
                Finding(
                    kind="flow_chain_resolution",
                    path=f"narratives[{i}].entry",
                    message=f"narrative '{nid}' の entry '{entry}' が scenarios[].name に解決できません",
                    slice={"narrative": nid, "entry": entry},
                )
            )

    for i, s in enumerate(model.get("scenarios") or []):
        if not isinstance(s, dict):
            continue
        sname = s.get("name") or ""

        # next: string or dict
        next_value = s.get("next")
        if isinstance(next_value, str):
            if next_value not in scs_names:
                findings.append(
                    Finding(
                        kind="flow_chain_resolution",
                        path=f"scenarios[{i}].next",
                        message=f"scenario '{sname}' の next '{next_value}' が scenarios[].name に解決できません",
                        slice={"scenario": sname, "next": next_value},
                    )
                )
        elif isinstance(next_value, dict):
            for fid, target in next_value.items():
                if narrative_ids and fid not in narrative_ids:
                    findings.append(
                        Finding(
                            kind="flow_chain_resolution",
                            path=f"scenarios[{i}].next.{fid}",
                            message=f"scenario '{sname}' の next キー '{fid}' が narratives[].id に存在しません",
                            slice={"scenario": sname, "flow_id": fid},
                        )
                    )
                if isinstance(target, str) and target and target not in scs_names:
                    findings.append(
                        Finding(
                            kind="flow_chain_resolution",
                            path=f"scenarios[{i}].next.{fid}",
                            message=f"scenario '{sname}' の next.{fid} '{target}' が scenarios[].name に解決できません",
                            slice={"scenario": sname, "flow_id": fid, "next": target},
                        )
                    )

        # brs[].next / brs[].terminal
        for j, br in enumerate(s.get("brs") or []):
            if not isinstance(br, dict):
                continue
            br_next = br.get("next")
            if br_next and br_next not in scs_names:
                findings.append(
                    Finding(
                        kind="flow_chain_resolution",
                        path=f"scenarios[{i}].brs[{j}].next",
                        message=f"scenario '{sname}' brs[{j}] の next '{br_next}' が scenarios[].name に解決できません",
                        slice={"scenario": sname, "next": br_next},
                    )
                )
            term = br.get("terminal")
            if term and narrative_ids and term not in narrative_ids:
                findings.append(
                    Finding(
                        kind="flow_chain_resolution",
                        path=f"scenarios[{i}].brs[{j}].terminal",
                        message=f"scenario '{sname}' brs[{j}] の terminal '{term}' が narratives[].id に存在しません",
                        slice={"scenario": sname, "terminal": term},
                    )
                )

    return findings


def _diverges_downstream(start: str | None, by_name: dict[str, dict]) -> bool:
    """start から単一 next（str）チェーンを辿り、下流に「narrative を区別する分岐点」
    （next の dict 化 or brs[].terminal）があれば True を返す。

    共有 entry の直後しばらく同じ経路を辿ってから下流で分岐する「合流区間」を
    正当と認めるための判定。visited でサイクルガードする。
    """
    visited: set[str] = set()
    cur = start
    while cur and cur not in visited:
        visited.add(cur)
        sc = by_name.get(cur)
        if sc is None:
            return False
        if isinstance(sc.get("next"), dict):
            return True
        for br in sc.get("brs") or []:
            if isinstance(br, dict) and br.get("terminal"):
                return True
        nxt = sc.get("next")
        cur = nxt if isinstance(nxt, str) else None
    return False


def narrative_entry_consistency(model: dict) -> list[Finding]:
    """複数の narrative が同一 scenario を `entry` に指す場合、
    対応する scenarios[].next が narrative.id をキーとする dict 形式に
    なっているかを検証する（v6 規約）。

    そうでないと、すべての narrative が同じフローを描画してしまい、
    意味チェック agent が「フロー broken」と誤検出する原因になる。

    OK パターン：
      - scenarios[].next が dict 形式で全 narrative.id をカバー
      - 漏れている narrative.id が brs[].terminal で別途終端宣言されている
      - entry 直下は単一 next でも、下流のどこかで narrative を区別する分岐点がある
        （合流区間。`_diverges_downstream` で判定）
    """
    narratives = model.get("narratives") or []
    scenarios = model.get("scenarios") or []
    by_name: dict[str, dict] = {
        s.get("name"): s for s in scenarios
        if isinstance(s, dict) and s.get("name")
    }

    by_entry: dict[str, list[str]] = {}
    for n in narratives:
        if not isinstance(n, dict):
            continue
        entry = n.get("entry")
        nid = n.get("id")
        if entry and nid:
            by_entry.setdefault(entry, []).append(nid)

    findings: list[Finding] = []
    for entry_name, nids in by_entry.items():
        if len(nids) < 2:
            continue
        sc = by_name.get(entry_name)
        if not sc:
            # flow_chain_resolution で別途検出されるためここはスキップ
            continue

        # brs[].terminal で narrative ごとに終端宣言されているものを集計
        terminal_narratives: set[str] = set()
        for br in sc.get("brs") or []:
            if isinstance(br, dict) and br.get("terminal"):
                terminal_narratives.add(br["terminal"])

        nxt = sc.get("next")
        if isinstance(nxt, dict):
            # dict 形式 — narrative ID 全てがキー or terminal でカバーされているか
            missing = [
                nid for nid in nids
                if nid not in nxt and nid not in terminal_narratives
            ]
            if missing:
                findings.append(
                    Finding(
                        kind="narrative_entry_consistency",
                        path=f"scenarios[].next",
                        message=(
                            f"narratives {nids} が entry '{entry_name}' を共有していますが、"
                            f"scenario.next dict に {missing} のキーが無く、brs[].terminal でも"
                            f"終端宣言されていません"
                        ),
                        slice={
                            "entry": entry_name,
                            "shared_by": nids,
                            "missing_keys": missing,
                        },
                    )
                )
        else:
            # 単一値 or 未設定 — brs[].terminal で各 narrative が個別に終端
            # 宣言されていれば OK、そうでなければ全 narrative が同じフローを辿る
            unhandled = [nid for nid in nids if nid not in terminal_narratives]
            # 下流で narrative を区別する分岐点があれば「合流区間」として許容
            if len(unhandled) >= 2 and not _diverges_downstream(
                nxt if isinstance(nxt, str) else None, by_name
            ):
                findings.append(
                    Finding(
                        kind="narrative_entry_consistency",
                        path=f"scenarios[].next",
                        message=(
                            f"narratives {unhandled} が entry '{entry_name}' を共有しているのに、"
                            f"scenario.next が単一値（または未設定）で、下流にも分岐点がありません。"
                            f"narrative.id をキーとする dict 形式に書き換えるか、brs[].terminal で"
                            f"フロー別に終端宣言してください（dml-spec.md v6 規約）"
                        ),
                        slice={
                            "entry": entry_name,
                            "shared_by": unhandled,
                            "current_next": nxt,
                        },
                    )
                )
    return findings


def narrative_happy_unique(model: dict) -> list[Finding]:
    """`kind: happy` の narrative は 0 or 1 件であることを確認する（v8）。

    ハッピーパスは「正規フロー」を 1 本だけ示す概念なので、複数 happy が宣言された場合は
    モデリングミスとみなす（旧トップレベル `story:` を統合した v8 で導入）。
    """
    findings: list[Finding] = []
    happy_indices = [
        i for i, n in enumerate(model.get("narratives") or [])
        if isinstance(n, dict) and n.get("kind") == "happy"
    ]
    if len(happy_indices) >= 2:
        ids = [
            (model["narratives"][i].get("id") or f"#{i}") for i in happy_indices
        ]
        findings.append(
            Finding(
                kind="narrative_happy_unique",
                path="narratives",
                message=(
                    f"kind:happy が {len(happy_indices)} 件あります "
                    f"（ハッピーパスは 1 本だけ）: {ids}"
                ),
                slice={"happy_ids": ids},
            )
        )
    return findings


def dangling_lang_entry(model: dict) -> list[Finding]:
    """contexts[].lang.{pols,cmds,evts,aggs,qrys} に登録された名前が
    モデル本体（policies/scenarios/aggregates/queries）に存在しない場合に警告する。

    `language_coverage` は逆方向（モデル要素が lang に未登録）を見るが、本チェックは
    「lang に書いたけれど実体が無い」ケースを検出する。typo / リネーム漏れ / lang だけ
    先行追加した実装忘れを早期発見するのが目的。
    """
    declared = {
        "pols": {p.get("name") for p in (model.get("policies") or []) if isinstance(p, dict)},
        "cmds": set(),
        "evts": set(),
        "aggs": {a.get("name") for a in (model.get("aggregates") or []) if isinstance(a, dict)},
        "qrys": {q.get("name") for q in (model.get("queries") or []) if isinstance(q, dict)},
        "actors": set(),
        # vos は意図的に非対象: scenarios/policies/aggregates 本体に現れず（attrs.type の
        # 自由文字列で参照される程度）、厳密な cross-ref は誤検知を招くため declared に含めない。
    }
    for s in model.get("scenarios") or []:
        if isinstance(s, dict):
            if s.get("cmd"):
                declared["cmds"].add(s["cmd"])
            if s.get("evt"):
                declared["evts"].add(s["evt"])
            if s.get("actor"):
                declared["actors"].add(s["actor"])
            for q in s.get("qry") or []:
                if q:
                    declared["qrys"].add(q)
            for br in s.get("brs") or []:
                if isinstance(br, dict) and br.get("evt"):
                    declared["evts"].add(br["evt"])
    for p in model.get("policies") or []:
        if isinstance(p, dict):
            if p.get("cmd"):
                declared["cmds"].add(p["cmd"])
            if p.get("evt"):
                declared["evts"].add(p["evt"])
            if p.get("trg"):
                declared["evts"].add(p["trg"])
            if p.get("qry"):
                declared["qrys"].add(p["qry"])
            trgs = p.get("trgs") or {}
            if isinstance(trgs, dict):
                for ev in trgs.get("evts") or []:
                    declared["evts"].add(ev)
    for a in model.get("aggregates") or []:
        if isinstance(a, dict):
            for ev in a.get("events") or []:
                if isinstance(ev, dict) and ev.get("name"):
                    declared["evts"].add(ev["name"])

    findings: list[Finding] = []
    for i, ctx, lang in _iter_ctx_lang(model):
        ctx_name = ctx.get("name") or f"#{i}"
        for cat, real_set in declared.items():
            cat_dict = lang.get(cat) or {}
            if not isinstance(cat_dict, dict):
                continue
            for name in cat_dict.keys():
                if name not in real_set:
                    findings.append(
                        Finding(
                            kind="dangling_lang_entry",
                            path=f"contexts[{i}].lang.{cat}.{name}",
                            message=(
                                f"BC '{ctx_name}' の lang.{cat} に '{name}' が登録されていますが、"
                                f"対応する実体が見つかりません（typo / 未実装 / リネーム漏れの可能性）"
                            ),
                            slice={"ctx": ctx_name, "category": cat, "identifier": name},
                        )
                    )
    return findings


def cross_bc_state_name_collision(model: dict) -> list[Finding]:
    """異なる AGG / BC で同じ state 名（UPPER_SNAKE）が使われている場合、
    lang.states の日本語ラベルが一致しなければ「同名異義」として警告する。

    例: event-planning.Event.CANCELLED と participation.Participation.CANCELLED は
    英語 ID 完全一致だが意味が異なる（前者=イベント中止、後者=参加辞退）。HTML フロー図
    のラベル日本語化で混乱を招くため、lang.states のラベルで差別化されているかを確認する。
    """
    # state -> [(ctx_name, agg_name, label_or_None), ...]
    by_state: dict[str, list[tuple[str, str, str | None]]] = {}
    ctx_lang_states: dict[str, dict] = {}
    for ctx in model.get("contexts") or []:
        if not isinstance(ctx, dict):
            continue
        cname = ctx.get("name") or ""
        lang = ctx.get("lang") or {}
        if isinstance(lang, dict):
            states = lang.get("states") or {}
            if isinstance(states, dict):
                ctx_lang_states[cname] = states

    for agg in model.get("aggregates") or []:
        if not isinstance(agg, dict):
            continue
        ctx = agg.get("ctx") or ""
        agg_name = agg.get("name") or ""
        label_map = ctx_lang_states.get(ctx, {})
        for state in agg.get("states") or []:
            by_state.setdefault(state, []).append((ctx, agg_name, label_map.get(state)))

    findings: list[Finding] = []
    for state, occurrences in by_state.items():
        if len(occurrences) < 2:
            continue
        # 異なる AGG / ctx で同じ state が使われている
        labels = {label for _, _, label in occurrences if label}
        if len(labels) <= 1:
            # ラベル無し or 全て同一ラベル → 同義かもしれないが同名なので警告のみ
            # 但しラベル無しなら lang.states に未登録の警告は language_coverage 系の責務外。
            # ここでは「ラベルで差別化されていない同名 state」のみ警告。
            owners = [f"{c}.{a}" for c, a, _ in occurrences]
            findings.append(
                Finding(
                    kind="cross_bc_state_name_collision",
                    path="aggregates[].states",
                    message=(
                        f"state '{state}' が複数 AGG '{owners}' で使われていますが、"
                        f"lang.states での日本語ラベル差別化が確認できません。"
                        f"BC 間で意味が異なる場合は lang.states のラベルを各 BC で書き分けてください"
                    ),
                    slice={"state": state, "owners": owners, "labels": list(labels)},
                )
            )
    return findings


def question_decision_link(model: dict) -> list[Finding]:
    """questions[].status==closed の decision_id が decisions[].id と整合するか確認。"""
    decision_ids = {
        d.get("id") for d in (model.get("decisions") or []) if isinstance(d, dict)
    }
    findings: list[Finding] = []
    for i, q in enumerate(model.get("questions") or []):
        if not isinstance(q, dict):
            continue
        if q.get("status") != "closed":
            continue
        did = q.get("decision_id")
        qid = q.get("id") or ""
        if not did:
            findings.append(
                Finding(
                    kind="question_decision_link",
                    path=f"questions[{i}].decision_id",
                    message=f"question '{qid}' が closed なのに decision_id が無い",
                    slice={"question": qid},
                )
            )
            continue
        if did not in decision_ids:
            findings.append(
                Finding(
                    kind="question_decision_link",
                    path=f"questions[{i}].decision_id",
                    message=f"question '{qid}' の decision_id '{did}' が decisions[].id に存在しない",
                    slice={"question": qid, "decision_id": did},
                )
            )
    return findings


def agg_purpose_minlength(model: dict) -> list[Finding]:
    """aggregates[].purpose が欠落 or 30 字未満なら警告する。

    `references/checks/agg-purpose-quality.md`（意味チェック）の観点 1「purpose は 30 字以上か」
    を構造チェックに降格したもの。単一責任・業務語彙の質的評価は引き続き LLM の責務。
    """
    MIN_LEN = 30
    findings: list[Finding] = []
    for i, agg in enumerate(model.get("aggregates") or []):
        if not isinstance(agg, dict):
            continue
        name = agg.get("name") or f"#{i}"
        purpose = (agg.get("purpose") or "").strip()
        if not purpose:
            findings.append(
                Finding(
                    kind="agg_purpose_minlength",
                    path=f"aggregates[{i}].purpose",
                    message=f"AGG '{name}' に purpose がありません（単一責任を 30 字以上で言語化してください）",
                    slice={"agg": name, "purpose_len": 0},
                )
            )
        elif len(purpose) < MIN_LEN:
            findings.append(
                Finding(
                    kind="agg_purpose_minlength",
                    path=f"aggregates[{i}].purpose",
                    message=(
                        f"AGG '{name}' の purpose が {len(purpose)} 字で短すぎます"
                        f"（{MIN_LEN} 字以上で「何を保証する集約か」を言語化してください）"
                    ),
                    slice={"agg": name, "purpose": purpose, "purpose_len": len(purpose)},
                )
            )
    return findings


def decision_chosen_adopted(model: dict) -> list[Finding]:
    """decisions[].chosen と options[].adopted の整合を検査する。

    採用は chosen から導出できるため adopted の明示は任意（ビルダーも
    `adopted is None → name == chosen` で描画する）。検査するのは:
    - chosen が options[].name に存在しない（必須）
    - adopted: true を明示している場合のみ、2 件以上 / chosen と不一致 を検出
    `references/checks/decision-rationale-clarity.md` の観点 4 を構造チェックに降格したもの。
    """
    findings: list[Finding] = []
    for i, d in enumerate(model.get("decisions") or []):
        if not isinstance(d, dict):
            continue
        did = d.get("id") or f"#{i}"
        options = [o for o in (d.get("options") or []) if isinstance(o, dict)]
        if not options:
            continue  # options 未記入の進行中 decision はスキップ
        chosen = d.get("chosen")
        option_names = [o.get("name") for o in options]
        adopted = [o.get("name") for o in options if o.get("adopted") is True]

        if chosen and chosen not in option_names:
            findings.append(
                Finding(
                    kind="decision_chosen_adopted",
                    path=f"decisions[{i}].chosen",
                    message=f"decision '{did}' の chosen '{chosen}' が options[].name に存在しません",
                    slice={"decision": did, "chosen": chosen, "options": option_names},
                )
            )
        if len(adopted) >= 2:
            findings.append(
                Finding(
                    kind="decision_chosen_adopted",
                    path=f"decisions[{i}].options",
                    message=f"decision '{did}' で adopted: true が {len(adopted)} 件あります（採用は 1 件だけ。通常は adopted を省いて chosen に任せる）: {adopted}",
                    slice={"decision": did, "adopted": adopted},
                )
            )
        elif adopted and chosen and adopted[0] != chosen:
            findings.append(
                Finding(
                    kind="decision_chosen_adopted",
                    path=f"decisions[{i}]",
                    message=f"decision '{did}' の chosen '{chosen}' と adopted: true の option '{adopted[0]}' が不一致",
                    slice={"decision": did, "chosen": chosen, "adopted": adopted[0]},
                )
            )
    return findings


def decision_affects_presence(model: dict) -> list[Finding]:
    """decisions[].affects が欠落 / 空なら警告する。

    採用判断の影響範囲（AGG / Policy / BC）が無い decision は将来の読者がトレースできない。
    `references/checks/decision-rationale-clarity.md` の観点 3 を構造チェックに降格したもの
    （粒度の適切さ＝Policy 連鎖の漏れ等は引き続き LLM の責務）。
    """
    findings: list[Finding] = []
    for i, d in enumerate(model.get("decisions") or []):
        if not isinstance(d, dict):
            continue
        did = d.get("id") or f"#{i}"
        affects = d.get("affects")
        if not affects:
            findings.append(
                Finding(
                    kind="decision_affects_presence",
                    path=f"decisions[{i}].affects",
                    message=(
                        f"decision '{did}' に affects[] がありません"
                        f"（影響を受ける AGG / Policy / BC を記載してください）"
                    ),
                    slice={"decision": did, "topic": d.get("topic")},
                )
            )
    return findings


def err_name_quality(model: dict) -> list[Finding]:
    """scenarios[].errs[].err がコード風 / 汎用的すぎる名前でないかを検査する。

    PascalCase 形式そのもの（`^[A-Z][A-Za-z0-9]*$`）は JSON Schema が担保するため、
    ここでは schema を通過してしまう「業務エラー名として弱い」パターンだけを拾う:
      - 数字を含む（`Err001` / `Http404` のようなコード風）
      - 1 語のみ（`Invalid` / `Error` のような汎用語。大文字が 1 つ＝単語 1 個とみなす）
    `references/checks/scenario-rules-quality.md` の観点 3 の機械化可能部分。
    """
    findings: list[Finding] = []
    for i, s in enumerate(model.get("scenarios") or []):
        if not isinstance(s, dict):
            continue
        sname = s.get("name") or f"#{i}"
        for j, e in enumerate(s.get("errs") or []):
            if not isinstance(e, dict):
                continue
            err = e.get("err") or ""
            if not err:
                continue  # 必須欠落は schema 検証の責務
            if any(ch.isdigit() for ch in err):
                findings.append(
                    Finding(
                        kind="err_name_quality",
                        path=f"scenarios[{i}].errs[{j}].err",
                        message=(
                            f"scenario '{sname}' のエラー名 '{err}' が数字を含みコード風です"
                            f"（業務語彙のエラー名に書き換えを検討: 例 Err001 → QuoteAlreadyConsumed）"
                        ),
                        slice={"scenario": sname, "err": err},
                    )
                )
            elif sum(1 for ch in err if ch.isupper()) < 2:
                findings.append(
                    Finding(
                        kind="err_name_quality",
                        path=f"scenarios[{i}].errs[{j}].err",
                        message=(
                            f"scenario '{sname}' のエラー名 '{err}' が 1 語だけで汎用的すぎます"
                            f"（何が・どう違反したかを含む複合語にしてください: 例 Invalid → ApplyDeadlineInvalid）"
                        ),
                        slice={"scenario": sname, "err": err},
                    )
                )
    return findings


def bc_vocabulary_collision(model: dict) -> list[Finding]:
    """contexts[].lang 辞書の EN↔JP 完全一致衝突を検出する。

    - 同名異義: 同カテゴリの同じ英語識別子が複数 BC で **異なる日本語ラベル** を持つ
    - 異名同義: 同カテゴリの同じ日本語ラベルが **異なる英語識別子** に対応している（BC 内 / BC 間とも）
    `references/checks/bc-vocabulary-consistency.md` の観点 1・2 のうち完全一致で判定できる部分の降格。
    表記ゆれ（近縁語）や意図的な Conformist / ACL の判断は引き続き LLM の責務。
    states は `cross_bc_state_name_collision`（同名 state はラベルで差別化すべし）と方向が
    逆になるため対象外。
    """
    CATEGORIES = ("aggs", "actors", "cmds", "evts", "pols", "qrys", "vos")
    # (cat, en) -> [(ctx, label)], (cat, label) -> [(ctx, en)]
    by_en: dict[tuple[str, str], list[tuple[str, str]]] = {}
    by_label: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for _i, ctx, lang in _iter_ctx_lang(model):
        cname = ctx.get("name") or ""
        for cat in CATEGORIES:
            cat_dict = lang.get(cat) or {}
            if not isinstance(cat_dict, dict):
                continue
            for en, label in cat_dict.items():
                if not en or not isinstance(label, str) or not label:
                    continue
                by_en.setdefault((cat, en), []).append((cname, label))
                by_label.setdefault((cat, label), []).append((cname, en))

    findings: list[Finding] = []
    for (cat, en), occurrences in by_en.items():
        labels = {label for _, label in occurrences}
        if len(labels) >= 2:
            owners = [f"{c}({label})" for c, label in occurrences]
            findings.append(
                Finding(
                    kind="bc_vocabulary_collision",
                    path=f"contexts[].lang.{cat}.{en}",
                    message=(
                        f"同名異義: '{en}' が複数 BC で異なる日本語ラベルを持ちます: {owners}。"
                        f"解消手段: (1) 同一概念ならラベルを両 BC で統一 "
                        f"(2) 別概念なら `dmlctl rename --from={en} --to=<新名> --ctx=<bc>` で識別子を分離 "
                        f"(3) 意図的な流用なら contexts[].note（lang エントリ単位の note は持てない）に "
                        f"Conformist / ACL の関係を明記"
                    ),
                    slice={"category": cat, "identifier": en, "occurrences": owners},
                )
            )
    for (cat, label), occurrences in by_label.items():
        ens = {en for _, en in occurrences}
        if len(ens) >= 2:
            owners = [f"{c}.{en}" for c, en in occurrences]
            findings.append(
                Finding(
                    kind="bc_vocabulary_collision",
                    path=f"contexts[].lang.{cat}",
                    message=(
                        f"異名同義: 日本語ラベル '{label}' が異なる英語識別子 {sorted(ens)} に対応しています: {owners}。"
                        f"解消手段: (1) 同一概念なら `dmlctl rename` で識別子を 1 つに統一 "
                        f"(2) 別概念ならラベルを書き分ける "
                        f"(3) BC 間の意図的な同義なら contexts[].note に Conformist / ACL を明記"
                    ),
                    slice={"category": cat, "label": label, "identifiers": sorted(ens), "occurrences": owners},
                )
            )
    return findings


def crud_cmd_naming(model: dict) -> list[Finding]:
    """CMD 名が CRUD 風接頭辞（Create/Add/Update/Delete/Get/Set 等）で始まっていないかを検査する。

    CMD は業務行為（Publish / Apply / Conclude ...）で命名するのが Ubiquitous Language の作法。
    `references/checks/bc-vocabulary-consistency.md` の観点 3 のヒューリスティック降格。
    接頭辞の直後が大文字の場合のみフラグ（`Address...` のような偶然の前方一致は除外）。
    """
    CRUD_PREFIXES = (
        "Create", "Add", "Update", "Delete", "Remove",
        "Get", "Set", "Fetch", "Edit", "Modify", "Insert",
    )

    def _is_crud(name: str) -> str | None:
        for p in CRUD_PREFIXES:
            rest = name[len(p):]
            if name.startswith(p) and rest and rest[0].isupper():
                return p
        return None

    # cmd 名の出現箇所を収集（scenarios / policies / lang.cmds）
    seen: dict[str, str] = {}  # cmd -> 最初に見つけた path
    for i, s in enumerate(model.get("scenarios") or []):
        if isinstance(s, dict) and s.get("cmd"):
            seen.setdefault(s["cmd"], f"scenarios[{i}].cmd")
    for i, p in enumerate(model.get("policies") or []):
        if isinstance(p, dict) and p.get("cmd"):
            seen.setdefault(p["cmd"], f"policies[{i}].cmd")
    for i, ctx in enumerate(model.get("contexts") or []):
        if not isinstance(ctx, dict):
            continue
        lang = ctx.get("lang") or {}
        cmds = lang.get("cmds") if isinstance(lang, dict) else None
        if isinstance(cmds, dict):
            for cmd in cmds.keys():
                seen.setdefault(cmd, f"contexts[{i}].lang.cmds.{cmd}")

    findings: list[Finding] = []
    for cmd, path in seen.items():
        prefix = _is_crud(cmd)
        if prefix:
            findings.append(
                Finding(
                    kind="crud_cmd_naming",
                    path=path,
                    message=(
                        f"CMD '{cmd}' が CRUD 風接頭辞 '{prefix}' で始まっています"
                        f"（業務行為の動詞への言い換えを検討: 例 UpdateEvent → RescheduleEvent / PublishEvent）"
                    ),
                    slice={"cmd": cmd, "prefix": prefix},
                )
            )
    return findings


def subdomain_classification(model: dict) -> list[Finding]:
    """コアドメイン蒸留（Core/Supporting/Generic 分類）の実施状況を検査する。

    戦略 DDD の「どこに投資するか」を決める分類が DML に残っているかの構造チェック：
      1. contexts[] があるのに domains[].subs[] / contexts[].sub が全く無い → 分類未実施
      2. contexts[].sub が未設定の BC がある → 所属サブドメイン未割当
      3. contexts[].sub が domains[].subs[].name に存在しない → 参照切れ
      4. CORE_SUBDOMAIN が 1 件も無い → コアが特定されていない
      5. サブドメインが複数あるのに全件 CORE → 蒸留されていない（優先順位がつかない）

    type の値は CORE_SUBDOMAIN / CORE_DOMAIN（後方互換）等の両表記を許容し、
    接頭辞（CORE / SUPPORTING / GENERIC）で正規化して判定する。
    """
    contexts = [c for c in model.get("contexts") or [] if isinstance(c, dict)]
    if not contexts:
        return []

    # domains[].subs[] を name → type(正規化済み) に展開
    sub_types: dict[str, str] = {}
    for d in model.get("domains") or []:
        if not isinstance(d, dict):
            continue
        for s in d.get("subs") or []:
            if isinstance(s, dict) and s.get("name"):
                raw = s.get("type") or ""
                sub_types[s["name"]] = raw.split("_")[0] if raw else ""

    ctx_subs = [c.get("sub") for c in contexts]

    findings: list[Finding] = []

    # 1. 分類が丸ごと未実施
    if not sub_types and not any(ctx_subs):
        findings.append(
            Finding(
                kind="subdomain_classification",
                path="domains",
                message=(
                    "サブドメイン分類（コアドメイン蒸留）が未実施です。"
                    "domains[].subs[] に CORE/SUPPORTING/GENERIC を定義し、"
                    "各 contexts[].sub で所属を割り当ててください"
                ),
                slice={"contexts": [c.get("name") for c in contexts]},
            )
        )
        return findings

    # 2. / 3. contexts[].sub の未設定・参照切れ
    for i, ctx in enumerate(contexts):
        name = ctx.get("name") or ""
        sub = ctx.get("sub")
        if not sub:
            findings.append(
                Finding(
                    kind="subdomain_classification",
                    path=f"contexts[{i}]",
                    message=f"BC '{name}' に所属サブドメイン（sub）が未設定です",
                    slice={"ctx": name, "known_subs": sorted(sub_types)},
                )
            )
        elif sub not in sub_types:
            findings.append(
                Finding(
                    kind="subdomain_classification",
                    path=f"contexts[{i}].sub",
                    message=(
                        f"BC '{name}' の sub '{sub}' が domains[].subs[].name に存在しません"
                        "（typo か domains 側の定義漏れ）"
                    ),
                    slice={"ctx": name, "sub": sub, "known_subs": sorted(sub_types)},
                )
            )

    # 4. / 5. CORE の特定状況（subs が定義済みの場合のみ判定）
    if sub_types:
        core_count = sum(1 for t in sub_types.values() if t == "CORE")
        if core_count == 0:
            findings.append(
                Finding(
                    kind="subdomain_classification",
                    path="domains",
                    message=(
                        "CORE サブドメインが 1 件もありません。"
                        "「間違えると事業が成り立たない領域」を 1 つ特定してください"
                    ),
                    slice={"subs": sub_types},
                )
            )
        elif len(sub_types) > 1 and core_count == len(sub_types):
            findings.append(
                Finding(
                    kind="subdomain_classification",
                    path="domains",
                    message=(
                        "全サブドメインが CORE です。蒸留されていない可能性があります"
                        "（買ってくれば済む・真似すれば済む領域を SUPPORTING/GENERIC に降格）"
                    ),
                    slice={"subs": sub_types},
                )
            )

    return findings


# ============================================================
# レジストリ
# ============================================================


# ============================================================
# 集約・集約間連携の整合チェック（構造チェック拡張）
# ============================================================
# 状態機械（aggregates[].states/transitions/events）と、それを駆動する
# scenarios / policies の突き合わせ。集約間の状態前提や非同期挙動は構造では判定できないため
# モデル検査（別途）の領分とし、ここでは「宣言同士が矛盾していないか」だけを見る。

def _dict_aggs(model: dict):
    """(i, agg) を dict の AGG だけ yield する。"""
    for i, agg in enumerate(model.get("aggregates") or []):
        if isinstance(agg, dict):
            yield i, agg


def _aggs_by_name(model: dict) -> dict[str, dict]:
    return {a["name"]: a for _, a in _dict_aggs(model) if a.get("name")}


def _agg_event_names(agg: dict) -> set[str]:
    return {
        ev["name"]
        for ev in (agg.get("events") or [])
        if isinstance(ev, dict) and ev.get("name")
    }


def _ambiguous(a: dict, b: dict) -> bool:
    """同じ (from, via) の 2 遷移が曖昧か（to が重なる、または when が欠ける）。"""
    overlap = set(_to_list(a.get("to"))) & set(_to_list(b.get("to")))
    return bool(overlap) or not a.get("when") or not b.get("when")


def transition_state_integrity(model: dict) -> list[Finding]:
    """transitions[].from/to が states に存在するか、(from, via) が重複していないかを検査する。

    - `from: NONE` は「生成前」の擬似状態として許容
    - `terminalStates` の要素も states に存在する必要がある
    - 同じ (from, via) の遷移が複数あるとき、`to` が重なる、または `when` の無い遷移が混じると
      どちらが発火するか曖昧（`when` 付きで `to` が異なる分岐は正当な書き方として許容）
    """
    findings: list[Finding] = []
    for i, agg in _dict_aggs(model):
        states = agg.get("states") or []
        if not states:
            continue
        name = agg.get("name") or ""
        known = set(states)
        seen: dict[tuple[str, str], list[tuple[int, dict]]] = {}
        for j, tr in enumerate(agg.get("transitions") or []):
            if not isinstance(tr, dict):
                continue
            frm = tr.get("from")
            if frm and frm != INITIAL_PSEUDO_STATE and frm not in known:
                findings.append(Finding(
                    kind="transition_state_integrity",
                    path=f"aggregates[{i}].transitions[{j}].from",
                    message=f"AGG '{name}' の遷移 from '{frm}' が states に存在しません",
                    slice={"agg": name, "from": frm},
                ))
            for t in _to_list(tr.get("to")):
                if t not in known:
                    findings.append(Finding(
                        kind="transition_state_integrity",
                        path=f"aggregates[{i}].transitions[{j}].to",
                        message=f"AGG '{name}' の遷移 to '{t}' が states に存在しません",
                        slice={"agg": name, "to": t},
                    ))
            key = (frm or "", tr.get("via") or "")
            if not key[1]:
                continue
            prev = next((pj for pj, other in seen.get(key, []) if _ambiguous(tr, other)), None)
            if prev is not None:
                findings.append(Finding(
                    kind="transition_state_integrity",
                    path=f"aggregates[{i}].transitions[{j}]",
                    message=(
                        f"AGG '{name}' の遷移 (from '{key[0]}', via '{key[1]}') が "
                        f"transitions[{prev}] と曖昧です（to が重複、または when が無い）"
                    ),
                    slice={"agg": name, "from": key[0], "via": key[1]},
                ))
            seen.setdefault(key, []).append((j, tr))
        for k, ts in enumerate(agg.get("terminalStates") or []):
            if ts not in known:
                findings.append(Finding(
                    kind="transition_state_integrity",
                    path=f"aggregates[{i}].terminalStates[{k}]",
                    message=f"AGG '{name}' の terminalStates '{ts}' が states に存在しません",
                    slice={"agg": name, "terminalState": ts},
                ))
    return findings


def scenario_agg_consistency(model: dict) -> list[Finding]:
    """scenarios[].agg が指す AGG と、scenario の cmd / evt が整合しているかを検査する。

    - agg が aggregates[] に存在しない
    - evt / brs[].evt が agg の events にない（severity: warn・events を宣言している AGG のみ対象）

    cmd が transitions[].via に無いことは検査しない。playbook §AGG が「生成（creation）」
    「同一状態内の属性変更」を transitions[] に書かないと定めており、正当な省略と区別できないため。
    """
    aggs = _aggs_by_name(model)
    findings: list[Finding] = []
    for i, sc in enumerate(model.get("scenarios") or []):
        if not isinstance(sc, dict):
            continue
        agg_name = sc.get("agg")
        if not agg_name:
            continue
        sc_name = sc.get("name") or ""
        agg = aggs.get(agg_name)
        if agg is None:
            findings.append(Finding(
                kind="scenario_agg_consistency",
                path=f"scenarios[{i}].agg",
                message=f"シナリオ '{sc_name}' の agg '{agg_name}' が aggregates[] に存在しません",
                slice={"scenario": sc_name, "agg": agg_name},
            ))
            continue
        declared = _agg_event_names(agg)
        if not declared:
            continue
        evts = []
        if sc.get("evt"):
            evts.append((f"scenarios[{i}].evt", sc["evt"]))
        for k, br in enumerate(sc.get("brs") or []):
            if isinstance(br, dict) and br.get("evt"):
                evts.append((f"scenarios[{i}].brs[{k}].evt", br["evt"]))
        for path, ev in evts:
            if ev not in declared:
                findings.append(Finding(
                    kind="scenario_agg_consistency",
                    severity="warn",
                    path=path,
                    message=f"シナリオ '{sc_name}' の evt '{ev}' が AGG '{agg_name}' の events に宣言されていません",
                    slice={"scenario": sc_name, "agg": agg_name, "evt": ev},
                ))
    return findings


def evt_ownership(model: dict) -> list[Finding]:
    """EVT の所有 AGG が一意かを検査する。

    - 同じ EVT 名を複数の AGG が events に宣言している（emit 元が曖昧）

    副作用専用 POLICY（agg なし）が発行する EVT はどの AGG にも属さないのが正当なため、
    「未所属」は検査しない。
    """
    owners: dict[str, list[str]] = {}
    for _, agg in _dict_aggs(model):
        for ev in _agg_event_names(agg):
            owners.setdefault(ev, []).append(agg.get("name") or "")
    findings: list[Finding] = []
    for ev, names in sorted(owners.items()):
        if len(names) > 1:
            findings.append(Finding(
                kind="evt_ownership",
                path="aggregates",
                message=f"EVT '{ev}' が複数の AGG（{', '.join(names)}）の events に宣言されています",
                slice={"evt": ev, "aggs": names},
            ))
    return findings


def policy_agg_consistency(model: dict) -> list[Finding]:
    """policies[] の cmd / evt が、対象 agg の状態機械と整合しているかを検査する。

    - agg が aggregates[] に存在しない
    - evt が agg の events にない（severity: warn・events を宣言している AGG のみ対象）

    cmd だけで agg を持たない POLICY は副作用専用（通知など）として正当なので検査しない。
    cmd が transitions[].via に無いことも、scenario と同じ理由で検査しない。
    """
    aggs = _aggs_by_name(model)
    findings: list[Finding] = []
    for i, pol in enumerate(model.get("policies") or []):
        if not isinstance(pol, dict):
            continue
        name = pol.get("name") or ""
        agg_name = pol.get("agg")
        if not agg_name:
            continue
        agg = aggs.get(agg_name)
        if agg is None:
            findings.append(Finding(
                kind="policy_agg_consistency",
                path=f"policies[{i}].agg",
                message=f"POL '{name}' の agg '{agg_name}' が aggregates[] に存在しません",
                slice={"policy": name, "agg": agg_name},
            ))
            continue
        declared = _agg_event_names(agg)
        ev = pol.get("evt")
        if ev and declared and ev not in declared:
            findings.append(Finding(
                kind="policy_agg_consistency",
                severity="warn",
                path=f"policies[{i}].evt",
                message=f"POL '{name}' の evt '{ev}' が AGG '{agg_name}' の events に宣言されていません",
                slice={"policy": name, "agg": agg_name, "evt": ev},
            ))
    return findings


def policy_cycle(model: dict) -> list[Finding]:
    """EVT → POLICY → CMD → EVT の連鎖が閉路を作っていないかを検査する（severity: warn）。

    辺: EVT e を trg/trgs に持つ policy p について、p.evt および p.cmd を発行元とする
    scenario の evt / brs[].evt へ e → e' を張る。排他分岐の一部だけが循環する場合も
    「起こりうる」ので警告にとどめる（意図した再試行ループは note などで人が判断する）。
    """
    cmd_evts: dict[str, set[str]] = {}
    for sc in model.get("scenarios") or []:
        if isinstance(sc, dict) and sc.get("cmd"):
            cmd_evts.setdefault(sc["cmd"], set()).update(scenario_evts(sc))

    graph: dict[str, set[str]] = {}
    via_pol: dict[tuple[str, str], set[str]] = {}
    for pol in model.get("policies") or []:
        if not isinstance(pol, dict):
            continue
        triggers, _mode = policy_triggers(pol)
        outs = {pol["evt"]} if pol.get("evt") else set()
        if pol.get("cmd"):
            outs |= cmd_evts.get(pol["cmd"], set())
        for t in triggers:
            graph.setdefault(t, set())
            for o in outs:
                graph[t].add(o)
                graph.setdefault(o, set())
                via_pol.setdefault((t, o), set()).add(pol.get("name") or "")

    # Tarjan SCC（再帰なし）
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    sccs: list[list[str]] = []
    counter = 0
    for root in sorted(graph):
        if root in index:
            continue
        work = [(root, iter(sorted(graph[root])))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(sorted(graph[nxt]))))
                    advanced = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                sccs.append(comp)

    findings: list[Finding] = []
    for comp in sccs:
        members = sorted(comp)
        if len(members) == 1 and members[0] not in graph[members[0]]:
            continue  # 自己ループでない単独ノードは閉路ではない
        pols = sorted({
            p for (a, b), ps in via_pol.items() if a in comp and b in comp for p in ps
        })
        findings.append(Finding(
            kind="policy_cycle",
            severity="warn",
            path="policies",
            message=(
                f"EVT 連鎖が循環しうる: {' → '.join(members)}"
                f"（POL: {', '.join(pols)}）。意図した再試行でなければ停止条件を設ける"
            ),
            slice={"events": members, "policies": pols},
        ))
    return findings


def state_dead_end(model: dict) -> list[Finding]:
    """出る遷移がなく、終端と宣言されていない状態を報告する（severity: warn）。

    終端状態は `aggregates[].terminalStates` で宣言する。transitions を持たない AGG は対象外。
    """
    findings: list[Finding] = []
    for i, agg in _dict_aggs(model):
        states = agg.get("states") or []
        transitions = [t for t in (agg.get("transitions") or []) if isinstance(t, dict)]
        if not states or not transitions:
            continue
        terminals = set(agg.get("terminalStates") or [])
        outgoing = {t.get("from") for t in transitions}
        for state in states:
            if state in terminals or state in outgoing:
                continue
            findings.append(Finding(
                kind="state_dead_end",
                severity="warn",
                path=f"aggregates[{i}].states",
                message=(
                    f"AGG '{agg.get('name')}' の状態 '{state}' から出る遷移がありません"
                    "（終端なら terminalStates に宣言する）"
                ),
                slice={"agg": agg.get("name"), "state": state},
            ))
    return findings


def invariant_refs(model: dict) -> list[Finding]:
    """invariants[] と scenarios/policies[].requires の参照整合性を検査する。

    agg が aggregates[] に存在し、in の状態が agg.states か生成前の NONE であること。
    """
    aggs = _aggs_by_name(model)
    findings: list[Finding] = []
    conds_by_owner: list[tuple[str, list[tuple[str, Any]]]] = []
    for i, inv in enumerate(model.get("invariants") or []):
        if not isinstance(inv, dict):
            continue
        name = inv.get("name") or f"#{i}"
        conds: list[tuple[str, Any]] = []
        for k, c in enumerate(inv.get("never") or []):
            conds.append((f"invariants[{i}].never[{k}]", c))
        for key in ("when", "eventually"):
            if inv.get(key):
                conds.append((f"invariants[{i}].{key}", inv[key]))
        conds_by_owner.append((name, conds))
    for coll in ("scenarios", "policies"):
        for i, item in enumerate(model.get(coll) or []):
            if isinstance(item, dict) and item.get("requires"):
                label = item.get("name") or f"#{i}"
                conds_by_owner.append((
                    label, [(f"{coll}[{i}].requires[{k}]", c) for k, c in enumerate(item["requires"])]
                ))
    for name, conds in conds_by_owner:
        for path, c in conds:
            if not isinstance(c, dict):
                continue
            agg = aggs.get(c.get("agg"))
            if agg is None:
                findings.append(Finding(
                    kind="invariant_refs",
                    path=f"{path}.agg",
                    message=f"'{name}' の条件の agg '{c.get('agg')}' が aggregates[] に存在しません",
                    slice={"invariant": name, "agg": c.get("agg")},
                ))
                continue
            known = set(agg.get("states") or []) | {INITIAL_PSEUDO_STATE}  # NONE=生成前
            for st in c.get("in") or []:
                if st not in known:
                    findings.append(Finding(
                        kind="invariant_refs",
                        path=f"{path}.in",
                        message=f"'{name}' の条件の状態 '{st}' が AGG '{agg.get('name')}' の states に存在しません",
                        slice={"invariant": name, "agg": agg.get("name"), "state": st},
                    ))
    return findings


# ============================================================
# フロー到達性・ポリシーとの整合・進行状況（2026-11 追加。すべて warn）
# ============================================================


def _next_targets(sc: dict) -> list[tuple[list[str], str]]:
    """scenario から `next` / `brs[].next` で進む先を `(その経路で発火する evt 群, 先の scenario 名)` で返す。"""
    out: list[tuple[list[str], str]] = []
    nxt = sc.get("next")
    evts = scenario_evts(sc)
    if isinstance(nxt, str) and nxt:
        out.append((evts, nxt))
    elif isinstance(nxt, dict):
        for v in nxt.values():
            if isinstance(v, str) and v:
                out.append((evts, v))
    for br in sc.get("brs") or []:
        if isinstance(br, dict) and br.get("next"):
            out.append(([br["evt"]] if br.get("evt") else [], br["next"]))
    return out


def _policy_cmds_triggered_by(by_trg: dict[str, list[dict]], evts: list[str]) -> set[str]:
    """evts から（policy.evt の連鎖も辿って）起動される policies[].cmd の集合。

    `by_trg` は `index_policies_by_trg(model)`（EVT → POLICY の解釈の単一の定義元）。"""
    out: set[str] = set()
    seen: set[str] = set()
    frontier = [e for e in evts if e]
    while frontier:
        ev = frontier.pop()
        if ev in seen:
            continue
        seen.add(ev)
        for p in by_trg.get(ev, []):
            if p.get("cmd"):
                out.add(p["cmd"])
            if p.get("evt"):
                frontier.append(p["evt"])
    return out


def undeclared_ctx(model: dict) -> list[Finding]:
    """scenarios / policies / aggregates / queries の `ctx` が contexts[] に宣言されていないものを警告する。

    日本語ラベルの置き場 `contexts[].lang` は BC の下にあるため、BC が無いとラベルを登録できず、
    HTML のフロー図が英語の識別子のまま出る。フェーズ 3 の仮の ctx でも、BC は同じターンに作る。"""
    declared = {c.get("name") for c in model.get("contexts") or [] if isinstance(c, dict)}
    missing: dict[str, str] = {}  # ctx → 最初に使われたパス
    for key in ("scenarios", "policies", "aggregates", "queries"):
        for i, it in enumerate(model.get(key) or []):
            ctx = it.get("ctx") if isinstance(it, dict) else None
            if isinstance(ctx, str) and ctx and ctx not in declared:
                missing.setdefault(ctx, f"{key}[{i}].ctx")
    return [
        Finding(
            kind="undeclared_ctx",
            severity="warn",
            path=path,
            message=(
                f"ctx '{ctx}' は contexts[] に宣言されていません。仮の BC を作ってください: "
                f"dmlctl add <file> --to=contexts --item='{{name: {ctx}, label_ja: <日本語名>}}'"
            ),
            slice={"ctx": ctx},
        )
        for ctx, path in missing.items()
    ]


def alt_narrative_entry(model: dict) -> list[Finding]:
    """`kind: alt` の narrative に `entry` が無いものを警告する。

    entry が無い代替シナリオは散文だけになり、フロー図に展開されない（どのシナリオが
    代替の流れに属するかが構造に残らない）。フェーズ 2〜3 の散文だけの段階で騒がないよう、
    フロー連鎖を書き始めた（いずれかの narrative に entry がある）DML だけを対象にする。"""
    narratives = model.get("narratives") or []
    if not any(isinstance(n, dict) and n.get("entry") for n in narratives):
        return []
    findings: list[Finding] = []
    for i, n in enumerate(narratives):
        if not isinstance(n, dict) or n.get("kind") != "alt" or n.get("entry"):
            continue
        findings.append(Finding(
            kind="alt_narrative_entry",
            severity="warn",
            path=f"narratives[{i}].entry",
            message=f"代替シナリオ '{n.get('id') or n.get('title') or i}' に entry がありません（フロー図に展開されません）",
            slice={"id": n.get("id"), "title": n.get("title")},
        ))
    return findings


def unreachable_scenario(model: dict) -> list[Finding]:
    """どの narrative の entry からも辿れないシナリオを警告する。

    辿り方: entry → `next` / `brs[].next` → 発火した evt を trg に持つ policy の cmd を実行する
    scenario（policy.evt の連鎖も辿る）。フロー連鎖を書き始めた DML だけを対象にする。
    孤立したシナリオはフロー図に出ず、因果の意味チェックからも漏れる。"""
    scenarios = [s for s in model.get("scenarios") or [] if isinstance(s, dict) and s.get("name")]
    entries = [n.get("entry") for n in model.get("narratives") or [] if isinstance(n, dict) and n.get("entry")]
    if not entries or not scenarios:
        return []
    by_name = {s["name"]: s for s in scenarios}
    by_cmd: dict[str, list[str]] = {}
    for s in scenarios:
        if s.get("cmd"):
            by_cmd.setdefault(s["cmd"], []).append(s["name"])
    by_trg = index_policies_by_trg(model)
    reached: set[str] = set()
    frontier = [e for e in entries if e in by_name]
    while frontier:
        name = frontier.pop()
        if name in reached:
            continue
        reached.add(name)
        sc = by_name[name]
        for _evts, target in _next_targets(sc):
            if target in by_name and target not in reached:
                frontier.append(target)
        for cmd in _policy_cmds_triggered_by(by_trg, scenario_evts(sc)):
            for target in by_cmd.get(cmd, []):
                if target not in reached:
                    frontier.append(target)
    findings: list[Finding] = []
    for i, s in enumerate(model.get("scenarios") or []):
        if not isinstance(s, dict) or not s.get("name") or s["name"] in reached:
            continue
        findings.append(Finding(
            kind="unreachable_scenario",
            severity="warn",
            path=f"scenarios[{i}]",
            message=(
                f"シナリオ '{s['name']}' はどの narrative の entry からも辿れません"
                "（next / brs[].next / policy でつなぐか、代替シナリオの entry にする）"
            ),
            slice={"scenario": s["name"], "ctx": s.get("ctx")},
        ))
    return findings


def next_bypasses_policy(model: dict) -> list[Finding]:
    """POLICY が起動する CMD のシナリオへ、そのポリシーの trg を発行しないシナリオから `next` でつないでいるものを警告する。

    ビルダーは「前のシナリオの evt → policy を挿入 → next で policy の CMD を実行するシナリオへ」と描く。
    前のシナリオが trg を発行していないのに next でつなぐと、図の上では手順の直結に見え、
    「〜が起きたら〜する」という起動条件（例: 在庫が減ったら焙煎）が消える。
    人が直接起動する経路も本当にあるなら、actor の違うシナリオに分ける（playbook §6）。"""
    pols_by_cmd: dict[str, list[str]] = {}
    for p in model.get("policies") or []:
        if isinstance(p, dict) and p.get("cmd"):
            pols_by_cmd.setdefault(p["cmd"], []).append(p.get("name") or "?")
    if not pols_by_cmd:
        return []
    by_trg = index_policies_by_trg(model)
    by_name = {
        s["name"]: s for s in model.get("scenarios") or [] if isinstance(s, dict) and s.get("name")
    }
    findings: list[Finding] = []
    for i, sc in enumerate(model.get("scenarios") or []):
        if not isinstance(sc, dict):
            continue
        reported: set[str] = set()
        for evts, target in _next_targets(sc):
            tgt = by_name.get(target)
            if not tgt or target in reported:
                continue
            cmd = tgt.get("cmd")
            if cmd not in pols_by_cmd or cmd in _policy_cmds_triggered_by(by_trg, evts):
                continue
            reported.add(target)
            pols = pols_by_cmd[cmd]
            findings.append(Finding(
                kind="next_bypasses_policy",
                severity="warn",
                path=f"scenarios[{i}].next",
                message=(
                    f"'{sc.get('name')}' から next で '{target}'（{cmd}）へ直結していますが、{cmd} は"
                    f" POLICY {', '.join(pols)} が起動します。前のシナリオはその起動イベントを発行していません"
                ),
                slice={"from": sc.get("name"), "to": target, "cmd": cmd, "policies": pols, "evts": evts},
            ))
    return findings


def policy_scenario_overlap(model: dict) -> list[Finding]:
    """同じ動きを POLICY とシナリオの両方で宣言し、中身が食い違っているものを警告する。

    - policy.cmd と同じ cmd のシナリオがあるのに、policy.evt がそのシナリオの evt に無い
    - policy.cmd と同じ cmd のシナリオの agg と policy.agg が違う
    - cmd を持たない副作用専用 POLICY の evt を、シナリオも発行している（同じ通知を二重に書いている）"""
    scenarios = [s for s in model.get("scenarios") or [] if isinstance(s, dict)]
    findings: list[Finding] = []
    for i, p in enumerate(model.get("policies") or []):
        if not isinstance(p, dict):
            continue
        name = p.get("name") or f"#{i}"
        cmd = p.get("cmd")
        if cmd:
            same = [s for s in scenarios if s.get("cmd") == cmd]
            if not same:
                continue
            evts = {e for s in same for e in scenario_evts(s)}
            if p.get("evt") and p["evt"] not in evts:
                findings.append(Finding(
                    kind="policy_scenario_overlap", severity="warn", path=f"policies[{i}].evt",
                    message=f"POLICY '{name}' の evt '{p['evt']}' が、{cmd} のシナリオの evt（{', '.join(sorted(evts)) or 'なし'}）と食い違います",
                    slice={"policy": name, "cmd": cmd, "policy_evt": p["evt"], "scenario_evts": sorted(evts)},
                ))
            aggs = {s.get("agg") for s in same if s.get("agg")}
            if p.get("agg") and aggs and p["agg"] not in aggs:
                findings.append(Finding(
                    kind="policy_scenario_overlap", severity="warn", path=f"policies[{i}].agg",
                    message=f"POLICY '{name}' の agg '{p['agg']}' が、{cmd} のシナリオの agg（{', '.join(sorted(aggs))}）と食い違います",
                    slice={"policy": name, "cmd": cmd, "policy_agg": p["agg"], "scenario_aggs": sorted(aggs)},
                ))
        elif p.get("evt"):
            emitters = [s.get("name") for s in scenarios if p["evt"] in scenario_evts(s)]
            if emitters:
                findings.append(Finding(
                    kind="policy_scenario_overlap", severity="warn", path=f"policies[{i}]",
                    message=(
                        f"副作用専用 POLICY '{name}' の evt '{p['evt']}' を、シナリオ {', '.join(emitters)} も発行しています。"
                        "内部の集約を変えないなら POLICY だけに、変えるならシナリオを書いて POLICY に cmd を付ける"
                    ),
                    slice={"policy": name, "evt": p["evt"], "scenarios": emitters},
                ))
    return findings


def _content_phase(model: dict) -> tuple[str, str] | None:
    """DML の中身から「少なくともここまでは完了している」フェーズと、その根拠を返す。"""
    aggs = [a for a in model.get("aggregates") or [] if isinstance(a, dict)]
    scenarios = [s for s in model.get("scenarios") or [] if isinstance(s, dict)]
    if any(a.get("transitions") or a.get("attrs") for a in aggs) or any(
        s.get("rules") or s.get("errs") for s in scenarios
    ):
        return "4.6", "遷移・属性・ルール・エラーが書かれている（フェーズ 5 に入っている）"
    if aggs:
        return "4.5", "集約が書かれている（フェーズ 4.6 に入っている）"
    if model.get("policies") or any(
        isinstance(n, dict) and n.get("entry") for n in model.get("narratives") or []
    ):
        return "3", "ポリシーかフロー連鎖（entry）が書かれている（フェーズ 4 に入っている）"
    if scenarios:
        return "2", "シナリオが書かれている（フェーズ 3 に入っている）"
    if model.get("narratives"):
        return "1", "ストーリーの散文が書かれている（フェーズ 2 に入っている）"
    return None


def phase_drift(model: dict) -> list[Finding]:
    """`session.phase`（直近完了フェーズ）が、DML の中身から見た進み具合より遅れていたら警告する。

    phase は `dmlctl advance` を手で回さないと進まず、HTML の進捗表示と再開時の判断がずれる。"""
    inferred = _content_phase(model)
    if not inferred:
        return []
    want, reason = inferred
    session = model.get("session") if isinstance(model.get("session"), dict) else {}
    have = str(session.get("phase")) if session.get("phase") is not None else None
    if have in PHASES and PHASES.index(have) >= PHASES.index(want):
        return []
    return [Finding(
        kind="phase_drift",
        severity="warn",
        path="session.phase",
        message=(
            f"session.phase が '{have or '未設定'}' ですが、中身から見ると少なくともフェーズ {want} は完了しています"
            f"（{reason}）。dmlctl advance <file> --phase={want} で進めてください"
        ),
        slice={"phase": have, "inferred_at_least": want},
    )]


CHECKS: dict[str, Callable[[dict], list[Finding]]] = {
    "orphan_agg": orphan_agg,
    "dangling_cmd": dangling_cmd,
    "unknown_evt_in_policy": unknown_evt_in_policy,
    "language_coverage": language_coverage,
    "state_reachability": state_reachability,
    "orphan_event": orphan_event,
    "flow_chain_resolution": flow_chain_resolution,
    "narrative_entry_consistency": narrative_entry_consistency,
    "narrative_happy_unique": narrative_happy_unique,
    "dangling_lang_entry": dangling_lang_entry,
    "cross_bc_state_name_collision": cross_bc_state_name_collision,
    "question_decision_link": question_decision_link,
    "agg_purpose_minlength": agg_purpose_minlength,
    "decision_chosen_adopted": decision_chosen_adopted,
    "decision_affects_presence": decision_affects_presence,
    "err_name_quality": err_name_quality,
    "bc_vocabulary_collision": bc_vocabulary_collision,
    "crud_cmd_naming": crud_cmd_naming,
    "subdomain_classification": subdomain_classification,
    "transition_state_integrity": transition_state_integrity,
    "scenario_agg_consistency": scenario_agg_consistency,
    "evt_ownership": evt_ownership,
    "policy_agg_consistency": policy_agg_consistency,
    "policy_cycle": policy_cycle,
    "state_dead_end": state_dead_end,
    "invariant_refs": invariant_refs,
    "undeclared_ctx": undeclared_ctx,
    "alt_narrative_entry": alt_narrative_entry,
    "unreachable_scenario": unreachable_scenario,
    "next_bypasses_policy": next_bypasses_policy,
    "policy_scenario_overlap": policy_scenario_overlap,
    "phase_drift": phase_drift,
}
