"""Round 7: can exits or entry-quality filters improve the 16 patterns? (4H, 2018 to now)

    python research7.py                          # exits table + feature terciles
    python research7.py combo EXIT FEATURE LOW|MID|HIGH [FEATURE ...]   # one chosen combination

Entries are the same ~6,700 trades as pnl_history.py. Each exit rule is replayed
on those same entries, so only the exit differs. "調整期" = entries before 2023,
"檢查期" = 2023 onward. Results are per trade, in R, net of 0.06% per side.

NOTE: the 2023+ totals of the base rules were already known when this was written,
so the check period is a sanity check, not a clean out-of-sample test.
"""
from __future__ import annotations

import bisect
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

import research4 as R

TF = R.TFS["4h"]
SPLIT = int(datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
FEATURES = ["形態高度/ATR", "預期賺賠比", "突破K棒實體/ATR", "突破量/均量", "止損距離%", "距最後波段點K數"]

# name -> (target: ("H", k) = k x pattern height | ("R", k) = k x risk | None, breakeven_at_R, partial_at_1R, trail_atr, trail_after_R, max_hold)
EXITS = {
    "目前規則（等幅目標）": (("H", 1.0), None, 0.0, None, None, 96),
    "目標 1R": (("R", 1.0), None, 0.0, None, None, 96),
    "目標 1.5R": (("R", 1.5), None, 0.0, None, None, 96),
    "目標 2R": (("R", 2.0), None, 0.0, None, None, 96),
    "目標 3R": (("R", 3.0), None, 0.0, None, None, 96),
    "目標 半個形態高度": (("H", 0.5), None, 0.0, None, None, 96),
    "目標 1.5 倍形態高度": (("H", 1.5), None, 0.0, None, None, 96),
    "到 1R 後保本": (("H", 1.0), 1.0, 0.0, None, None, 96),
    "到 1R 平一半並保本": (("H", 1.0), 1.0, 0.5, None, None, 96),
    "移動止損 2 ATR（無目標）": (None, None, 0.0, 2.0, 0.0, 96),
    "移動止損 3 ATR（無目標）": (None, None, 0.0, 3.0, 0.0, 96),
    "到 1R 後才移動止損 2 ATR": (None, None, 0.0, 2.0, 1.0, 96),
    "到 1R 後才移動止損 3 ATR": (None, None, 0.0, 3.0, 1.0, 96),
    "到 1R 平一半，其餘移動止損 3 ATR": (None, None, 0.5, 3.0, 1.0, 96),
    "最多持有 24 根": (("H", 1.0), None, 0.0, None, None, 24),
    "最多持有 48 根": (("H", 1.0), None, 0.0, None, None, 48),
}


def replay(bars, atr, i0, s, entry, stop0, height, rule, info=None):
    """Replay one trade from the candle after i0. -> (gross R, cost R, exit price, exit candle index or None if still open)"""
    target, be_at, part, trail, trail_after, max_hold = rule
    risk = abs(entry - stop0)
    tgt = None if target is None else entry + s * (height * target[1] if target[0] == "H" else risk * target[1])
    r1 = entry + s * risk
    stop, banked, best, armed = stop0, 0.0, entry, trail is not None and not trail_after
    px = ix = None
    for i in range(i0 + 1, min(len(bars), i0 + 1 + max_hold)):
        _, o, h, l, c, _ = bars[i]
        adv, fav = (l, h) if s > 0 else (h, l)
        if (adv - stop) * s <= 0:
            px, ix = (stop if (o - stop) * s > 0 else o), i
            break
        if tgt is not None and (fav - tgt) * s >= 0:
            px, ix = tgt, i
            break
        if (fav - r1) * s >= 0:
            if part and not banked:
                banked = part
            if be_at and (stop - entry) * s < 0:
                stop = entry
            if trail is not None:
                armed = True
        if (fav - best) * s > 0:
            best = fav
        if armed:
            new = best - s * trail * atr[i]
            if (new - stop) * s > 0:
                stop = new
    if px is None:
        last = min(len(bars) - 1, i0 + max_hold)
        px = bars[last][4]
        ix = last if last == i0 + max_hold else None      # None: history ends before the trade does
    g = banked * 1.0 + (1 - banked) * (px - entry) * s / risk
    cost = R.COST * (entry + banked * r1 + (1 - banked) * px) / risk
    if info is not None:
        info["stop"] = stop                      # where the (possibly trailed) stop stands now
    return g, cost, px, ix


def atr_series(bars):
    atr, a, trs = [], None, []
    for i, b in enumerate(bars):
        pc = bars[i - 1][4] if i else b[4]
        tr = max(b[2] - b[3], abs(b[2] - pc), abs(b[3] - pc))
        if a is None:
            trs.append(tr)
            a = sum(trs) / 14 if len(trs) == 14 else None
        else:
            a = (a * 13 + tr) / 14
        atr.append(a or tr)
    return atr


def job(sym):
    bars = R.load(sym, TF)
    closed, _ = R.simulate(bars, sym, TF)
    close_t = [b[0] + TF for b in bars]
    atr = atr_series(bars)
    out = []
    for r in closed:
        if r[3]:
            continue                               # use the no-volume-requirement stream (all trades)
        i0 = bisect.bisect_left(close_t, r[0])
        s, entry, stop = r[11], r[6], r[7]
        height = abs(r[8] - entry)
        res = {name: replay(bars, atr, i0, s, entry, stop, height, rule) for name, rule in EXITS.items()}
        out.append({"t": r[0], "sym": sym, "pat": r[2], "s": s, "feat": r[13], "res": {k: round(v[0] - v[1], 4) for k, v in res.items()}})
    return out


def stat(vals):
    n = len(vals)
    if n < 20:
        return f"{n:>6}     —       —      —"
    m, se = statistics.fmean(vals), statistics.stdev(vals) / n ** 0.5
    return f"{n:>6}{sum(v > 0.05 for v in vals) / n * 100:>6.1f}{m:>+8.3f}{m / se:>+7.2f}"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    syms = sorted(p.stem for p in R.DATA.glob("*.csv"))
    with ProcessPoolExecutor() as ex:
        trades = [t for rows in ex.map(job, syms) for t in rows]
    dev = [t for t in trades if t["t"] < SPLIT]
    chk = [t for t in trades if t["t"] >= SPLIT]
    head = "  筆數 勝率%   每筆R    t值"
    base = next(iter(EXITS))

    if len(sys.argv) > 1 and sys.argv[1] == "combo":
        ex_name, conds = sys.argv[2], sys.argv[3:]
        keep = lambda t: True
        label = ex_name
        for f, band in zip(conds[::2], conds[1::2]):
            k = FEATURES.index(f)
            v = sorted(t["feat"][k] for t in dev)
            q1, q2 = v[len(v) // 3], v[2 * len(v) // 3]
            lo, hi = {"LOW": (-1e18, q1), "MID": (q1, q2), "HIGH": (q2, 1e18)}[band]
            keep = (lambda t, p=keep, k=k, lo=lo, hi=hi: p(t) and lo <= t["feat"][k] < hi)
            label += f" + {f} {band}（{lo:.2f}~{hi:.2f}）" if abs(lo) < 1e17 and hi < 1e17 else f" + {f} {band}"
        print(label)
        for name in (base, ex_name):
            print(f"  {name:<24}| 調整期 {stat([t['res'][name] for t in dev if keep(t)])} | 檢查期 {stat([t['res'][name] for t in chk if keep(t)])}")
        for side, lab in ((1, "做多"), (-1, "做空")):
            print(f"  {ex_name}，{lab:<14}| 調整期 {stat([t['res'][ex_name] for t in dev if keep(t) and t['s'] == side])} | 檢查期 {stat([t['res'][ex_name] for t in chk if keep(t) and t['s'] == side])}")
        sel = [t for t in trades if keep(t)]
        print("  每年：" + "  ".join(
            f"{y} {statistics.fmean(v):+.2f}R({len(v)})" for y in range(2018, 2027)
            if (v := [t["res"][ex_name] for t in sel if datetime.fromtimestamp(t["t"] / 1000, tz=timezone.utc).year == y])))
        for lab, part in (("調整期", dev), ("檢查期", chk)):     # weekly totals: trades in the same week are not independent
            wk = {}
            for t in part:
                if keep(t):
                    wk[t["t"] // (7 * 86_400_000)] = wk.get(t["t"] // (7 * 86_400_000), 0.0) + t["res"][ex_name]
            v = list(wk.values())
            print(f"  {lab} 以週合計：{len(v)} 週，每週 {statistics.fmean(v):+.2f}R，t 值 {statistics.fmean(v) / (statistics.stdev(v) / len(v) ** 0.5):+.2f}，虧損週 {sum(x < 0 for x in v) / len(v):.0%}")
        return

    print(f"出場方式（同一批進場），調整期 2018~2022 / 檢查期 2023 之後\n{'':<34}| 調整期 {head} | 檢查期 {head}")
    for name in EXITS:
        print(f"{name:<{34 - sum(ord(c) > 255 for c in name)}}| {'':<7}{stat([t['res'][name] for t in dev])} | {'':<7}{stat([t['res'][name] for t in chk])}")

    print(f"\n進場特徵三等分（用調整期的分界），以「{base}」計\n{'':<34}| 調整期 {head} | 檢查期 {head}")
    for k, f in enumerate(FEATURES):
        v = sorted(t["feat"][k] for t in dev)
        q1, q2 = v[len(v) // 3], v[2 * len(v) // 3]
        for band, lo, hi in (("低", -1e18, q1), ("中", q1, q2), ("高", q2, 1e18)):
            lab = f"{f} {band}" + (f"（<{q1:.2f}）" if band == "低" else f"（≥{q2:.2f}）" if band == "高" else "")
            sel = lambda ts: [t["res"][base] for t in ts if lo <= t["feat"][k] < hi]
            print(f"{lab:<{34 - sum(ord(c) > 255 for c in lab)}}| {'':<7}{stat(sel(dev))} | {'':<7}{stat(sel(chk))}")


if __name__ == "__main__":
    main()
