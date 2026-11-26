"""Round 4 research: classic chart patterns, detected by rule, with and without volume.

    python research4.py dev                     # 2022-08 .. 2024-12
    python research4.py holdout KEY ...         # 2025-01 .. now, e.g. W_1h TRI_long_4h_vol

Patterns are built from confirmed swing points (a high/low with K lower/higher
candles on each side, known only K candles later - no look-ahead):

  W      double bottom: two similar lows, buy the close above the middle high
  M      double top (mirror)
  HS     head & shoulders top: sell the close below the neckline lows
  IHS    inverse head & shoulders (mirror)
  TRI    converging triangle (lower highs + higher lows): trade the breakout, either way
  TB/TT  triple bottom / triple top
  ASC    ascending triangle (flat top, rising lows), DESC descending triangle (mirror)
  RECT   rectangle (flat top and flat bottom): trade the breakout, either way
  RW     rising wedge breaking down, FW falling wedge breaking up
  FLAG   a sharp move (pole) then a shallow pause, entered when the pole's end is broken

Entry at the breakout candle's close, stop at the pattern's last swing on the
other side, target = the pattern height projected from the entry (measured move).
"""
from __future__ import annotations

import csv
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data_research"
COST = 0.0006          # per side, taker fee + slippage
K = 3                  # swing strength
MIN_RISK_PCT = 0.5     # skip trades whose stop is closer than this (cost control, learned in round 1)
MAX_HOLD = 96          # candles
ARM_BARS = 30          # breakout must come within this many candles of the last swing
TFS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "6h": 21_600_000, "8h": 28_800_000, "1d": 86_400_000}


def ms(y, m, d):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)


PERIODS = {"dev": (ms(2022, 8, 1), ms(2025, 1, 1)), "holdout": (ms(2025, 1, 1), ms(2100, 1, 1))}


def load(sym, tf_ms):
    out, key = [], None
    with (DATA / f"{sym}.csv").open() as f:
        for r in csv.reader(f):
            t, o, h, l, c, v = int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
            k = t // tf_ms
            if k != key:
                key = k
                out.append([k * tf_ms, o, h, l, c, v])
            else:
                b = out[-1]
                b[2], b[3], b[4], b[5] = max(b[2], h), min(b[3], l), c, b[5] + v
    out.pop()
    return out


def detect(P, atr, close, prev_close, i):
    """Yield (pattern, side, stop, height, id) for every pattern completed by this close."""
    if len(P) >= 3 and i - P[-1][0] <= ARM_BARS:
        a, b, c = P[-3], P[-2], P[-1]
        if a[1] == "L" and c[1] == "L":                       # W
            h = b[2] - min(a[2], c[2])
            if h >= 2 * atr and abs(a[2] - c[2]) <= 0.25 * h and prev_close <= b[2] < close:
                yield "W", 1, min(a[2], c[2]), h, c[0]
        if a[1] == "H" and c[1] == "H":                       # M
            h = max(a[2], c[2]) - b[2]
            if h >= 2 * atr and abs(a[2] - c[2]) <= 0.25 * h and prev_close >= b[2] > close:
                yield "M", -1, max(a[2], c[2]), h, c[0]
    if len(P) >= 5 and i - P[-1][0] <= ARM_BARS:
        s1, n1, hd, n2, s2 = P[-5:]
        if s1[1] == "H":                                      # head & shoulders top
            neck = (n1[2] + n2[2]) / 2
            h = hd[2] - neck
            line = min(n1[2], n2[2])
            if (hd[2] > s1[2] and hd[2] > s2[2] and min(s1[2], s2[2]) > neck and h >= 2 * atr
                    and abs(s1[2] - s2[2]) <= 0.3 * h and prev_close >= line > close):
                yield "HS", -1, s2[2], h, s2[0]
        else:                                                 # inverse
            neck = (n1[2] + n2[2]) / 2
            h = neck - hd[2]
            line = max(n1[2], n2[2])
            if (hd[2] < s1[2] and hd[2] < s2[2] and max(s1[2], s2[2]) < neck and h >= 2 * atr
                    and abs(s1[2] - s2[2]) <= 0.3 * h and prev_close <= line < close):
                yield "IHS", 1, s2[2], h, s2[0]
    if len(P) >= 4 and i - P[-1][0] <= ARM_BARS:
        hs = [p for p in P[-4:] if p[1] == "H"]
        ls = [p for p in P[-4:] if p[1] == "L"]
        if len(hs) == 2 and len(ls) == 2:
            base, tip = hs[0][2] - ls[0][2], hs[1][2] - ls[1][2]
            if hs[1][2] < hs[0][2] and ls[1][2] > ls[0][2] and base >= 2 * atr and 0 < tip <= 0.75 * base:
                if prev_close <= hs[1][2] < close:
                    yield "TRI_long", 1, ls[1][2], base, P[-1][0]
                if prev_close >= ls[1][2] > close:
                    yield "TRI_short", -1, hs[1][2], base, P[-1][0]
            if base >= 2 * atr:
                flat_top = abs(hs[1][2] - hs[0][2]) <= 0.15 * base
                flat_bot = abs(ls[1][2] - ls[0][2]) <= 0.15 * base
                top, bot = max(hs[0][2], hs[1][2]), min(ls[0][2], ls[1][2])
                if flat_top and ls[1][2] >= ls[0][2] + 0.2 * base and prev_close <= top < close:
                    yield "ASC", 1, ls[1][2], base, P[-1][0]          # ascending triangle
                if flat_bot and hs[1][2] <= hs[0][2] - 0.2 * base and prev_close >= bot > close:
                    yield "DESC", -1, hs[1][2], base, P[-1][0]        # descending triangle
                if flat_top and flat_bot:                             # rectangle
                    if prev_close <= top < close:
                        yield "RECT_long", 1, ls[1][2], base, P[-1][0]
                    if prev_close >= bot > close:
                        yield "RECT_short", -1, hs[1][2], base, P[-1][0]
                dh, dl = hs[1][2] - hs[0][2], ls[1][2] - ls[0][2]
                if dh > 0 and dl > dh and 0 < tip <= 0.75 * base and prev_close >= ls[1][2] > close:
                    yield "RW", -1, hs[1][2], base, P[-1][0]          # rising wedge breaks down
                if dl < 0 and dh < dl and 0 < tip <= 0.75 * base and prev_close <= hs[1][2] < close:
                    yield "FW", 1, ls[1][2], base, P[-1][0]           # falling wedge breaks up
    if len(P) >= 5 and i - P[-1][0] <= ARM_BARS:
        a, b, c, d, e = P[-5:]
        if a[1] == "L":                                               # triple bottom
            lo, hi = min(a[2], c[2], e[2]), max(b[2], d[2])
            h = hi - lo
            if h >= 2 * atr and max(a[2], c[2], e[2]) - lo <= 0.25 * h and prev_close <= hi < close:
                yield "TB", 1, lo, h, e[0]
        else:                                                         # triple top
            hi, lo = max(a[2], c[2], e[2]), min(b[2], d[2])
            h = hi - lo
            if h >= 2 * atr and hi - min(a[2], c[2], e[2]) <= 0.25 * h and prev_close >= lo > close:
                yield "TT", -1, hi, h, e[0]
    if len(P) >= 3 and i - P[-1][0] <= ARM_BARS:
        a, b, c = P[-3:]
        pole = abs(b[2] - a[2])
        fast = b[0] - a[0] <= 12 and pole >= 4 * atr                  # a sharp move: the flag pole
        if a[1] == "L" and fast and c[2] >= b[2] - 0.5 * pole and prev_close <= b[2] < close:
            yield "FLAG_long", 1, c[2], pole, c[0]
        if a[1] == "H" and fast and c[2] <= b[2] + 0.5 * pole and prev_close >= b[2] > close:
            yield "FLAG_short", -1, c[2], pole, c[0]


def job(args):
    sym, tf = args
    return tf, simulate(load(sym, TFS[tf]), sym, TFS[tf])[0]


def simulate(bars, sym, tf_ms):
    """-> (closed trades, still-open trades). Row: t_entry, sym, pattern, vol, R, cost_R, entry, stop, target, exit, t_exit, side"""
    P, atr, trs, vols = [], None, [], []
    open_tr, done_ids, out = {}, set(), []
    for i, (t, o, h, l, c, v) in enumerate(bars):
        pc = bars[i - 1][4] if i else c
        tr = max(h - l, abs(h - pc), abs(l - pc))
        if atr is None:
            trs.append(tr)
            if len(trs) == 14:
                atr = sum(trs) / 14
        else:
            atr = (atr * 13 + tr) / 14
        # manage open trades (stop first when a candle touches both)
        for key in list(open_tr):
            T = open_tr[key]
            s = T["s"]
            adv, fav = (l, h) if s > 0 else (h, l)
            px = None
            if (adv - T["stop"]) * s <= 0:
                px = T["stop"] if (o - T["stop"]) * s > 0 else o
            elif (fav - T["target"]) * s >= 0:
                px = T["target"]
            elif i - T["i"] >= MAX_HOLD:
                px = c
            if px is not None:
                g = (px - T["entry"]) * s / T["risk"]
                cost = COST * (T["entry"] + px) / T["risk"]
                out.append([T["t"], sym, key[0], key[1], round(g, 4), round(cost, 4),
                            T["entry"], T["stop"], T["target"], px, t + tf_ms, s, T["volok"], T["feat"]])
                del open_tr[key]
        # confirmed swings
        j = i - K
        if j >= K:
            win = bars[j - K:i + 1]
            for typ, val, ext in (("H", bars[j][2], max(x[2] for x in win)), ("L", bars[j][3], min(x[3] for x in win))):
                if val == ext:
                    if P and P[-1][1] == typ:
                        if (val > P[-1][2]) == (typ == "H"):
                            P[-1] = (j, typ, val)
                    else:
                        P.append((j, typ, val))
            del P[:-8]
        if atr and i >= 20:
            avg_v = sum(vols[-20:]) / 20
            for name, s, stop, height, pid in detect(P, atr, c, pc, i):
                risk = abs(c - stop)
                if (c - stop) * s <= 0 or risk / c * 100 < MIN_RISK_PCT or (name, pid) in done_ids:
                    continue
                done_ids.add((name, pid))
                for vol in (False, True):
                    if vol and v < 1.5 * avg_v:
                        continue
                    key = (name, vol)
                    if key not in open_tr:
                        open_tr[key] = {"s": s, "entry": c, "stop": stop, "risk": risk, "target": c + s * height,
                                        "i": i, "t": t + tf_ms, "volok": v >= 1.5 * avg_v,
                                        # entry-time features for research7: pattern height / ATR, planned reward:risk,
                                        # breakout candle body / ATR, volume / average, stop distance %, candles since last swing
                                        "feat": [round(height / atr, 3), round(height / risk, 3), round(abs(c - o) / atr, 3),
                                                 round(v / avg_v, 3) if avg_v else 0.0, round(risk / c * 100, 3), i - pid]}
        vols.append(v)
    return out, open_tr


def summarize(tr):
    n = len(tr)
    if n < 2:
        return None
    net = [x[4] - x[5] for x in tr]
    mean, se = statistics.fmean(net), statistics.stdev(net) / n ** 0.5
    by = {}
    for x, v in zip(tr, net):
        by.setdefault(x[1], []).append(v)
    return (n, sum(v > 0.05 for v in net) / n * 100, statistics.fmean(x[4] for x in tr),
            statistics.fmean(x[5] for x in tr), mean, sum(net), mean / se if se else 0.0,
            sum(statistics.fmean(v) > 0 for v in by.values()), len(by))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    stage, picks = sys.argv[1], sys.argv[2:]
    tfs = list(TFS)
    if picks and picks[0].startswith("--tf="):       # e.g. --tf=6h,8h,1d
        tfs, picks = picks[0][5:].split(","), picks[1:]
    syms = sorted(p.stem for p in DATA.glob("*.csv"))
    lo, hi = PERIODS[stage]
    hi = min(hi, int(time.time() * 1000))
    with ProcessPoolExecutor() as ex:
        res = list(ex.map(job, [(s, tf) for tf in tfs for s in syms]))
    groups = {}
    for tf, rows in res:
        for r in rows:
            if lo <= r[0] < hi:
                groups.setdefault(f"{r[2]}_{tf}{'_vol' if r[3] else ''}", []).append(r)
    f = lambda t: datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    print(f"{'開發期' if stage == 'dev' else '驗證期'} {f(lo)} ~ {f(hi)}，{len(syms)} 個商品，成本 {COST:.2%}/邊，_vol = 突破K棒成交量 ≥ 1.5 倍均量\n")
    print(f"{'pattern':<22}{'筆數':>6}{'勝率%':>7}{'未扣成本':>9}{'成本':>6}{'扣成本後':>9}{'合計R':>8}{'t值':>7}{'正商品':>6}")
    for key in (picks or sorted(groups, key=lambda k: (k.split("_")[-1] == "vol", k))):
        r = summarize(groups.get(key, []))
        if not r:
            print(f"{key:<22} 交易太少")
            continue
        print(f"{key:<22}{r[0]:>7}{r[1]:>8.1f}{r[2]:>+10.3f}{r[3]:>7.2f}{r[4]:>+10.3f}{r[5]:>+9.1f}{r[6]:>+8.2f}{r[7]:>5}/{r[8]}")


if __name__ == "__main__":
    main()
