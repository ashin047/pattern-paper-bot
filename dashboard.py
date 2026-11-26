"""Build dashboard.html: a self-contained page for the chart-pattern study.

    python pnl_history.py      # refresh the 2018-to-now backtest (logs/pattern_history_trades.csv)
    python dashboard.py        # rebuild the page

Tab 1: backtest statistics since 2018 (filter by direction, pattern, coin, volume).
Tab 2: the live paper test (logs/pattern_trades.csv, pattern_events.jsonl, pattern_state.json).
"""
from __future__ import annotations

import csv
import json
import sys
import time
from bisect import bisect_left
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
TF = 14_400_000
sys.stdout.reconfigure(encoding="utf-8")


def history():
    path = LOGS / "pattern_history_trades.csv"
    if not path.exists():
        return {"patterns": [], "symbols": [], "sides": [], "groups": [], "rows": [], "tf": {}}
    with path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    pats, syms, side, grp = [], [], {}, {}
    for r in rows:
        if r["pattern"] not in side:
            pats.append(r["pattern"])
            side[r["pattern"]] = 1 if r["side"] == "LONG" else -1
        if r["symbol"] not in syms:
            syms.append(r["symbol"])
            grp[r["symbol"]] = {"new": 1, "batch3": 2}.get(r.get("group"), 0)
    syms.sort()
    out = [[int(r["t_entry"]), syms.index(r["symbol"]), pats.index(r["pattern"]), int(r["volume_confirmed"]),
            round(float(r["R_net"]), 3), int(r["t_exit"]),
            round(float(r["strong_R_net"]), 3) if r.get("strong_R_net") else None,
            int(r["strong_t_exit"]) if r.get("strong_t_exit") else None] for r in rows]
    tf = {}                                        # strong version on the other timeframes
    extra = LOGS / "pattern_history_strong_tf.csv"
    if extra.exists():
        with extra.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["symbol"] in grp and r["pattern"] in side:
                    tf.setdefault(r["timeframe"], []).append(
                        [int(r["t_entry"]), syms.index(r["symbol"]), pats.index(r["pattern"]), 1, round(float(r["R_net"]), 3), int(r["t_exit"])])
    return {"patterns": pats, "sides": [side[p] for p in pats], "symbols": syms, "groups": [grp[s] for s in syms], "rows": out, "tf": tf}


TF_MS = {"4H": TF, "8H": 2 * TF, "12H": 3 * TF, "日線": 6 * TF}


class Candles:
    """4H candles per coin, aggregated on demand to 8H / 12H / daily, cut into windows around a trade."""

    def __init__(self):
        self.cache = {}

    def _load(self, sym):
        import research4
        from run_pattern import aggregate
        name = sym.replace("USDT", "") + ".csv"
        if (research4.DATA / f"{sym}.csv").exists():
            bars = research4.load(sym, TF)
        else:                                  # extra coins are stored directly as 4H candles
            oos = next(ROOT / d / name for d in ("data_oos", "data_oos3") if (ROOT / d / name).exists())
            with oos.open() as fh:
                bars = [[int(x[0]), *map(float, x[1:6])] for x in csv.reader(fh)]
        out = {}
        for tfn, ms in TF_MS.items():
            bb = bars if tfn == "4H" else aggregate(bars, ms // TF)
            out[tfn] = ([x[0] for x in bb], bb)
        return out

    def windows(self, sym, t_entry, t_exit):
        if sym not in self.cache:
            self.cache[sym] = self._load(sym)
        end = t_exit or int(time.time() * 1000)
        res = {}
        for tfn, ms in TF_MS.items():
            ts, bb = self.cache[sym][tfn]
            res[tfn] = [x[:5] for x in bb[max(0, bisect_left(ts, t_entry - 45 * ms)):bisect_left(ts, end + 12 * ms)]]
        return res


def backtest_by_combo():
    """(rule, timeframe, symbol, pattern) -> [trades, total R] over the whole backtest, for display next to a trade."""
    tot = {}

    def add(key, v):
        c = tot.setdefault(key, [0, 0.0])
        c[0] += 1
        c[1] += v
    h = LOGS / "pattern_history_trades.csv"
    if h.exists():
        with h.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                add(("原規則", "4H", r["symbol"], r["pattern"]), float(r["R_net"]))
                if r.get("strong_R_net"):
                    add(("強勢", "4H", r["symbol"], r["pattern"]), float(r["strong_R_net"]))
    e = LOGS / "pattern_history_strong_tf.csv"
    if e.exists():
        with e.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                add(("強勢", r["timeframe"], r["symbol"], r["pattern"]), float(r["R_net"]))
    return tot


def combo_key(label, timeframe, symbol):
    parts = label.split("｜")
    return ("強勢" if len(parts) > 1 else "原規則", timeframe or "4H", symbol, parts[0])


def paper():
    path = LOGS / "pattern_trades.csv"
    trades, candles, bt = [], Candles(), backtest_by_combo()
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                for k in ("t_entry", "t_exit"):
                    r[k] = int(r[k])
                for k in ("entry", "stop", "target", "exit", "R_gross", "R_net", "pnl", "equity"):
                    r[k] = float(r[k]) if r[k] != "" else None      # no target = trailing-stop version
                r["timeframe"] = r.get("timeframe") or "4H"
                for k in ("crowding", "funding_R"):
                    r[k] = float(r[k]) if r.get(k) not in (None, "") else None
                r["breadth"] = int(r["breadth"]) if r.get("breadth") not in (None, "") else None
                r["candles"] = candles.windows(r["symbol"], r["t_entry"], r["t_exit"])
                trades.append(r)
    # label each paper trade: does it belong to a combination that lost in the backtest? (frozen list)
    neg, frozen = set(), None
    nf = LOGS / "backtest_negative_combos.csv"
    if nf.exists():
        with nf.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                neg.add((r["rule"], r["timeframe"], r["symbol"], r["pattern"]))
                frozen = r["frozen_on"]
    for r in trades:
        key = combo_key(r["pattern"], r["timeframe"], r["symbol"])
        r["neg_combo"] = key in neg
        r["bt"] = [bt[key][0], round(bt[key][1], 1)] if key in bt else None
    trades.sort(key=lambda r: r["t_entry"])
    sp = LOGS / "pattern_state.json"
    state = json.loads(sp.read_text()) if sp.exists() else {"started_at": None, "equity": {}}
    ev = LOGS / "pattern_events.jsonl"
    events = [json.loads(x) for x in ev.read_text(encoding="utf-8").splitlines()[-80:] if x.strip()][::-1] if ev.exists() else []
    open_ids = set(state.get("entries", [])) - set(state.get("exits", []))
    pf = LOGS / "pattern_positions.json"
    pos = json.loads(pf.read_text(encoding="utf-8")) if pf.exists() else {"updated": None, "positions": []}
    started = state.get("started_at") or 0
    pos["positions"] = sorted((p for p in pos["positions"] if p["t_entry"] >= started), key=lambda p: (p["symbol"], p["t_entry"]))
    for p in pos["positions"]:
        key = combo_key(p["account"], p["timeframe"], p["symbol"])
        p["neg_combo"] = key in neg
        p["bt"] = [bt[key][0], round(bt[key][1], 1)] if key in bt else None
        p["candles"] = candles.windows(p["symbol"], p["t_entry"], None)
    return {"trades": trades, "equity": state.get("equity", {}), "started_at": state.get("started_at"),
            "events": events, "open": sorted(open_ids), "neg_frozen": frozen, "neg_count": len(neg), "positions": pos["positions"], "positions_updated": pos["updated"]}


def main():
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    data = {"generated": int(time.time() * 1000), "start_equity": cfg["start_equity"], "risk_pct": 0.5,
            "history": history(), "paper": paper()}
    html = (ROOT / "bot" / "dashboard_template.html").read_text(encoding="utf-8")
    out = ROOT / "dashboard.html"
    out.write_text(html.replace("/*DATA*/null", json.dumps(data, ensure_ascii=False, separators=(",", ":"))), encoding="utf-8")
    print(f"已產生 {out.name}（回測 {len(data['history']['rows'])} 筆，模擬交易 {len(data['paper']['trades'])} 筆）")


if __name__ == "__main__":
    main()
