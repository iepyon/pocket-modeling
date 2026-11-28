"""構造チェックと Quint 変換が共有する DML の読み方（単一の定義元）。

「生成前の擬似状態」「scenario が発行しうる EVT」「POLICY のトリガー」の解釈は
checks.py と dml_to_quint.py で一致していなければならないので、ここに集約する。
"""

from __future__ import annotations

from typing import Any

INITIAL_STATE = "NONE"  # transitions[].from に書く「生成前」の擬似状態

# session.phase の並び（schema の enum と同じ順）。schema を読めないときの既定値と、
# 中身から見たフェーズとの比較（phase_drift）が共有する。
PHASES = ["1", "2", "3", "4", "4.5", "4.6", "5", "6", "7"]


def to_list(v: Any) -> list[str]:
    """単一値 / 配列 / None を文字列リストに正規化する。"""
    if isinstance(v, list):
        return [x for x in v if x]
    return [v] if v else []


def scenario_evts(sc: dict) -> list[str]:
    """scenario の evt と brs[].evt（出現順・重複なし）。"""
    out = [sc["evt"]] if sc.get("evt") else []
    for br in sc.get("brs") or []:
        if isinstance(br, dict) and br.get("evt") and br["evt"] not in out:
            out.append(br["evt"])
    return out


def policy_triggers(pol: dict) -> tuple[list[str], str]:
    """(トリガー EVT のリスト, "all" | "any")。trg は単一、trgs は mode に従う（省略時 all）。"""
    if pol.get("trg"):
        return [pol["trg"]], "all"
    trgs = pol.get("trgs") or {}
    if isinstance(trgs, dict) and trgs.get("evts"):
        mode = "any" if str(trgs.get("mode") or "all").lower() in ("any", "or") else "all"
        return [e for e in trgs["evts"] if e], mode
    return [], "all"
