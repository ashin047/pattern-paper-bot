"""Candlestick charts (SVG) for every paper trade, in the trade's own timeframe, with entry and exit marked.

GitHub shows SVG files inside Markdown, so these work on the website and in the mobile app
without any hosting. Closed trades get one chart each, written once (charts/closed/<month>/).
Open positions are redrawn every run (charts/open/).

Used by status_md.py; can also be run alone:  python charts.py
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sys
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGS, DATA, OUT = ROOT / "logs", ROOT / "data_oos", ROOT / "charts"
TPE = timezone(timedelta(hours=8))
TF4 = 14_400_000
TF_MS = {"4H": TF4, "8H": 2 * TF4, "12H": 3 * TF4, "日線": 6 * TF4}
TF_CODE = {"4H": "4H", "8H": "8H", "12H": "12H", "日線": "1D"}
SIDE = {"LONG": "做多", "SHORT": "做空"}
W, H, ML, MR, MT, MB = 760, 330, 10, 128, 34, 24
UP, DOWN, INK, MUTED, GRID = "#1a9c6f", "#d9453f", "#111111", "#6b6b6b", "#e6e6e6"

_bars: dict = {}


def bars_for(symbol, tfn):
    """Closed candles of one coin in one timeframe, built from the stored 4H candles."""
    key = (symbol, tfn)
    if key not in _bars:
        path = DATA / f"{symbol.replace('USDT', '')}.csv"
        out = []
        if path.exists():
            tf, cur, last4 = TF_MS[tfn], None, None
            with path.open() as f:
                for r in csv.reader(f):
                    t, o, h, l, c = int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])
                    last4 = t
                    k = t // tf
                    if cur is None or cur[0] != k * tf:
                        cur = [k * tf, o, h, l, c]
                        out.append(cur)
                    else:
                        cur[2], cur[3], cur[4] = max(cur[2], h), min(cur[3], l), c
            if out and last4 != out[-1][0] + tf - TF4:
                out.pop()                              # the last candle of this timeframe has not closed yet
        _bars[key] = ([b[0] for b in out], out)
    return _bars[key]


def price(v):
    return f"{v:,.1f}" if v >= 1000 else f"{v:.3f}" if v >= 1 else f"{v:.4g}"


def when(ms, day_only=False):
    return datetime.fromtimestamp(ms / 1000, tz=TPE).strftime("%m-%d" if day_only else "%m-%d %H:%M")


def svg_chart(title, symbol, tfn, side, t_entry, t_exit, entry, stop, target, exit_px, stop0=None):
    """-> SVG text, or None when there is no candle data for this coin."""
    ts, bars = bars_for(symbol, tfn)
    if not bars:
        return None
    tf = TF_MS[tfn]
    i_e = max(0, min(len(bars) - 1, bisect_left(ts, t_entry - tf)))          # the candle that closed at t_entry
    i_x = len(bars) - 1 if t_exit is None else max(i_e, min(len(bars) - 1, bisect_left(ts, t_exit - tf)))
    a, b = max(0, i_e - 45), min(len(bars), i_x + 11)
    cs = bars[a:b]
    levels = [v for v in (entry, stop, target, exit_px, stop0) if v is not None]
    lo, hi = min(min(c[3] for c in cs), *levels), max(max(c[2] for c in cs), *levels)
    pad = (hi - lo) * 0.06 or hi * 0.01
    lo, hi = lo - pad, hi + pad
    slot = (W - ML - MR) / len(cs)
    bw = max(1.2, slot * 0.62)
    x = lambda i: ML + slot * (i + 0.5)
    y = lambda v: MT + (hi - v) / (hi - lo) * (H - MT - MB)
    f = lambda v: f"{v:.1f}"
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="system-ui,Segoe UI,Microsoft JhengHei,sans-serif" font-size="11">',
           f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
           f'<text x="{ML}" y="18" font-size="13" font-weight="600" fill="{INK}">{title}</text>']
    for k in range(5):                                                     # horizontal grid with prices on the left edge
        v = lo + (hi - lo) * k / 4
        out.append(f'<line x1="{ML}" x2="{W - MR}" y1="{f(y(v))}" y2="{f(y(v))}" stroke="{GRID}"/>')
        out.append(f'<text x="{ML + 3}" y="{f(y(v) - 3)}" fill="{MUTED}" font-size="10">{price(v)}</text>')
    x0, x1 = x(i_e - a) - slot / 2, x(i_x - a) + slot / 2
    out.append(f'<rect x="{f(x0)}" y="{MT}" width="{f(x1 - x0)}" height="{H - MT - MB}" fill="#2a78d6" opacity="0.09"/>')
    wick = {UP: [], DOWN: []}
    body = {UP: [], DOWN: []}
    for i, (_, o, h, l, c) in enumerate(cs):
        col = UP if c >= o else DOWN
        wick[col].append(f"M{f(x(i))} {f(y(h))}V{f(y(l))}")
        top, hgt = y(max(o, c)), max(1.0, abs(y(o) - y(c)))
        body[col].append(f"M{f(x(i) - bw / 2)} {f(top)}h{f(bw)}v{f(hgt)}h-{f(bw)}z")
    out.append(f'<path d="{"".join(wick[UP])}" stroke="{UP}" fill="none"/><path d="{"".join(wick[DOWN])}" stroke="{DOWN}" fill="none"/>')
    out.append(f'<path d="{"".join(body[UP])}" stroke="{UP}" fill="#ffffff"/><path d="{"".join(body[DOWN])}" stroke="{DOWN}" fill="{DOWN}"/>')
    moved = stop0 is not None and stop0 != stop
    lines = [("進場", entry, INK, ""), ("目前止損" if moved else "止損", stop, DOWN, "5 3"), ("原止損", stop0 if moved else None, MUTED, "2 4"),
             ("目標", target, UP, "5 3"), ("出場", exit_px, "#2a78d6", "1 3")]
    lines = sorted(((y(v), n, v, c, d) for n, v, c, d in lines if v is not None))
    ly_prev = -99
    for yy, name, v, col, dash in lines:
        out.append(f'<line x1="{f(x0)}" x2="{W - MR}" y1="{f(yy)}" y2="{f(yy)}" stroke="{col}" stroke-width="1.5"' + (f' stroke-dasharray="{dash}"' if dash else "") + "/>")
        ly = max(yy, ly_prev + 13)
        ly_prev = ly
        out.append(f'<text x="{W - MR + 6}" y="{f(ly + 4)}" fill="{INK}">{name} {price(v)}</text>')
    long = side == "LONG"
    ce = cs[i_e - a]
    ye = y(ce[3]) + 5 if long else y(ce[2]) - 5                          # entry marker: under the candle for buys, above for sells
    d = 11 if long else -11
    out.append(f'<path d="M{f(x(i_e - a))} {f(ye)}l6 {d}h-12z" fill="{INK}" stroke="#ffffff"/>')
    if t_exit is not None:
        cx_ = cs[i_x - a]
        yx = y(cx_[2]) - 5 if long else y(cx_[3]) + 5
        out.append(f'<path d="M{f(x(i_x - a))} {f(yx)}l6 {-d}h-12z" fill="#2a78d6" stroke="#ffffff"/>')
    step = max(1, len(cs) // 5)
    for i in range(0, len(cs), step):
        if x(i) > W - MR - 45:                       # leave room for the timeframe note at the right edge
            continue
        out.append(f'<text x="{f(x(i))}" y="{H - 7}" fill="{MUTED}" text-anchor="{"start" if i == 0 else "middle"}">{when(cs[i][0], tf >= 86_400_000)}</text>')
    out.append(f'<text x="{W - MR + 6}" y="{H - 7}" fill="{MUTED}">{tfn} K 棒 · 台北時間</text>')
    out.append("</svg>")
    return "\n".join(out)


def file_name(label, symbol, tfn, t_entry):
    h = hashlib.md5(label.encode("utf-8")).hexdigest()[:6]
    return f"{symbol.replace('USDT', '')}_{TF_CODE.get(tfn, '4H')}_{t_entry}_{h}.svg"


def load():
    state = json.loads((LOGS / "pattern_state.json").read_text())
    pf = LOGS / "pattern_positions.json"
    pos = json.loads(pf.read_text(encoding="utf-8")) if pf.exists() else {"updated": None, "positions": []}
    P = sorted((p for p in pos["positions"] if p["t_entry"] >= state["started_at"]), key=lambda p: (p["symbol"], p["t_entry"]))
    tp = LOGS / "pattern_trades.csv"
    T = []
    if tp.exists():
        with tp.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                for k in ("t_entry", "t_exit"):
                    r[k] = int(r[k])
                for k in ("entry", "stop", "exit", "R_net"):
                    r[k] = float(r[k])
                r["target"] = float(r["target"]) if r["target"] != "" else None
                r["timeframe"] = r.get("timeframe") or "4H"
                T.append(r)
    return state, pos, P, T


def build(P, T):
    """Draw what is missing. Adds a 'chart' key (path relative to the repo root, or None) to every position and trade."""
    open_dir = OUT / "open"
    if open_dir.exists():
        shutil.rmtree(open_dir)
    open_dir.mkdir(parents=True)
    for p in P:
        title = f"{p['symbol'].replace('USDT', '')} · {p['account']} · {SIDE[p['side']]} · 持倉中"
        svg = svg_chart(title, p["symbol"], p["timeframe"], p["side"], p["t_entry"], None, p["entry"], p["stop"], p["target"], None, p.get("stop0"))
        name = file_name(p["account"], p["symbol"], p["timeframe"], p["t_entry"])
        if svg:
            (open_dir / name).write_text(svg, encoding="utf-8")
        p["chart"] = f"charts/open/{name}" if svg else None
    for t in T:
        month = datetime.fromtimestamp(t["t_exit"] / 1000, tz=TPE).strftime("%Y-%m")
        t["month"] = month
        name = file_name(t["pattern"], t["symbol"], t["timeframe"], t["t_entry"])
        path = OUT / "closed" / month / name
        if not path.exists():
            title = f"{t['symbol'].replace('USDT', '')} · {t['pattern']} · {SIDE[t['side']]} · {t['R_net']:+.2f}R（{t['reason']}）"
            svg = svg_chart(title, t["symbol"], t["timeframe"], t["side"], t["t_entry"], t["t_exit"], t["entry"], t["stop"], t["target"], t["exit"])
            if svg:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(svg, encoding="utf-8")
        t["chart"] = f"charts/closed/{month}/{name}" if path.exists() else None


def trade_md(t, prefix=""):
    """One closed trade as Markdown: a heading, the facts, the chart."""
    tgt = price(t["target"]) if t["target"] is not None else "移動止損"
    out = [f"### {t['symbol'].replace('USDT', '')} · {t['pattern']} · {SIDE[t['side']]} · {t['R_net']:+.2f}R", "",
           f"{t['timeframe']} 週期｜進場 {when(t['t_entry'])} @ {price(t['entry'])}｜止損 {price(t['stop'])}｜目標 {tgt}｜"
           f"出場 {when(t['t_exit'])} @ {price(t['exit'])}（{t['reason']}）", ""]
    out.append(f"![{t['symbol']} {t['pattern']}]({prefix}{t['chart']})" if t["chart"] else "（沒有這個商品的 K 線資料，無法畫圖）")
    return out + [""]


def position_md(p):
    tgt = price(p["target"]) if p["target"] is not None else "移動止損"
    out = [f"### {p['symbol'].replace('USDT', '')} · {p['account']} · {SIDE[p['side']]} · 浮動 {p['R']:+.2f}R", "",
           f"{p['timeframe']} 週期｜進場 {when(p['t_entry'])} @ {price(p['entry'])}｜現價 {price(p['last'])}｜目前止損 {price(p['stop'])}｜目標 {tgt}", ""]
    out.append(f"![{p['symbol']} {p['account']}]({p['chart']})" if p["chart"] else "（沒有這個商品的 K 線資料，無法畫圖）")
    return out + [""]


def write_trade_pages(T):
    """trades/<month>.md with every closed trade of that month, and TRADES.md as the index."""
    months = sorted({t["month"] for t in T}, reverse=True)
    (ROOT / "trades").mkdir(exist_ok=True)
    idx = ["# 全部已結束交易的圖表", "", "每筆交易一張圖，用它自己的進場週期畫，標出進場、止損、目標與出場。最新的月份在最上面。", ""]
    if not months:
        idx.append("還沒有已結束的交易。")
    else:
        idx += ["| 月份 | 筆數 | 合計 |", "|---|---|---|"]
    for m in months:
        rows = sorted((t for t in T if t["month"] == m), key=lambda t: t["t_exit"], reverse=True)
        idx.append(f"| [{m}](trades/{m}.md) | {len(rows)} | {sum(t['R_net'] for t in rows):+.1f}R |")
        page = [f"# {m} 已結束的交易", "", f"共 {len(rows)} 筆，合計 {sum(t['R_net'] for t in rows):+.1f}R。最新的在最上面。[回到索引](../TRADES.md)", ""]
        for t in rows:
            page += trade_md(t, "../")
        (ROOT / "trades" / f"{m}.md").write_text("\n".join(page), encoding="utf-8")
    (ROOT / "TRADES.md").write_text("\n".join(idx) + "\n", encoding="utf-8")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    _, _, P, T = load()
    build(P, T)
    write_trade_pages(T)
    print(f"圖表：持倉 {sum(p['chart'] is not None for p in P)}/{len(P)} 張，已結束 {sum(t['chart'] is not None for t in T)}/{len(T)} 張")


if __name__ == "__main__":
    main()
