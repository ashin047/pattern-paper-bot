"""Paper-trade 16 classic 4H chart patterns going forward, bullish and bearish.

    python run_pattern.py

Simulated money only: no exchange account, no API key, no real orders.

After every 4-hour candle close (00, 04, 08, 12, 16, 20 UTC) it refreshes the
price history, re-runs the exact detector used in research4.py, and reports
any new short entry or exit. Because the detector is re-run on the full
history each time, a restart simply catches up on what happened while it was off.

    logs/pattern_events.jsonl   every entry / exit with prices
    logs/pattern_trades.csv     closed paper trades, net of fees and slippage
    logs/pattern_state.json     paper equity and what has already been reported
"""
from __future__ import annotations

import bisect
import csv
import json
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot.data import BASE, _get
from research4 import DATA, simulate
from research7 import atr_series, replay

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
OOS_DIRS = [ROOT / "data_oos", ROOT / "data_oos3"]   # extra coins, stored directly as 4H candles (research8.py, research9.py)
M15, TF = 900_000, 14_400_000
# label -> pattern name in research4. Every pattern keeps its own paper account.
# Each trade also records whether the breakout candle had 1.5x average volume.
PATTERNS = {
    # bullish
    "W底": "W", "三重底": "TB", "頭肩底": "IHS", "三角向上突破": "TRI_long", "上升三角": "ASC",
    "下降楔形": "FW", "箱型向上突破": "RECT_long", "多頭旗形": "FLAG_long",
    # bearish
    "M頭": "M", "三重頂": "TT", "頭肩頂": "HS", "三角向下跌破": "TRI_short", "下降三角": "DESC",
    "上升楔形": "RW", "箱型向下跌破": "RECT_short", "空頭旗形": "FLAG_short",
}
STRONG = "｜強勢"                     # suffix of the second account kept for every pattern
STRONG_VOL, STRONG_BODY = 1.77, 1.21  # breakout volume / average, breakout candle body / ATR (from research7)
STRONG_EXIT = (None, None, 0.0, 3.0, 1.0, 96)   # no target; after +1R trail the stop 3 ATR; max 96 candles
TFS_EXTRA = {"8H": 2, "12H": 3, "日線": 6}   # strong version also runs on these (multiples of 4H), see research10.py
FUND = ROOT / "data_funding"           # perpetual funding-rate history (see research11.py)
FUND_API = "https://fapi.binance.com/fapi/v1/fundingRate"
FUT_NAME = {"SHIB": "1000SHIB"}        # futures tickers that differ from spot
NEUTRAL = 0.0001                       # the neutral funding rate (0.01% per 8 hours)
FUND_STATE = {"down": False}           # set when the funding endpoint is unreachable (it is blocked in some regions)
RISK_PCT = 0.5                      # % of paper equity risked per trade
TPE = timezone(timedelta(hours=8))


def ts(ms):
    return datetime.fromtimestamp(ms / 1000, tz=TPE).strftime("%m-%d %H:%M")


def update(sym):
    """Append missing closed 15m candles (with volume) to data_research/<sym>.csv; return all rows."""
    path = DATA / f"{sym}.csv"
    with path.open() as f:
        rows = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in csv.reader(f)]
    now = int(time.time() * 1000)
    t, new = rows[-1][0] + M15, []
    while t + M15 <= now:
        got = _get(f"{BASE}?symbol={sym}&interval=15m&startTime={t}&limit=1000")
        got = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in got if r[0] + M15 <= now]
        if not got:
            break
        new += got
        t = got[-1][0] + M15
    if new:
        with path.open("a", newline="") as f:
            csv.writer(f).writerows(new)
    return rows + new


def update_4h(path):
    """Coins kept as 4H candles (<folder>/<name>.csv): append the newly closed ones; return all rows as lists."""
    name = path.stem
    with path.open() as f:
        rows = [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in csv.reader(f)]
    now = int(time.time() * 1000)
    t, new = rows[-1][0] + TF, []
    while t + TF <= now:
        got = _get(f"{BASE}?symbol={name}USDT&interval=4h&startTime={t}&limit=1000")
        got = [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in got if r[0] + TF <= now]
        if not got:
            break
        new += got
        t = got[-1][0] + TF
    if new:
        with path.open("a", newline="") as f:
            csv.writer(f).writerows(new)
    return rows + new


def candles_4h(rows):
    """Aggregate to 4H, keeping only candles whose last 15m candle has closed."""
    out, key = [], None
    for t, o, h, l, c, v in rows:
        k = t // TF
        if k != key:
            key = k
            out.append([k * TF, o, h, l, c, v])
        else:
            b = out[-1]
            b[2], b[3], b[4], b[5] = max(b[2], h), min(b[3], l), c, b[5] + v
    if rows[-1][0] != out[-1][0] + TF - M15:
        out.pop()
    return out


def funding_series(name):
    """Funding history of one coin (times, rates), topped up with the latest prints. Failures just keep what is on disk."""
    path = FUND / f"{name}.csv"
    rows = []
    if path.exists():
        with path.open() as f:
            rows = [(int(a), float(b)) for a, b in csv.reader(f)]
    if FUND_STATE["down"]:                             # the endpoint already failed this cycle: do not retry 68 times
        return [a for a, _ in rows], [b for _, b in rows]
    try:
        start = rows[-1][0] + 1 if rows else 0
        got = _get(f"{FUND_API}?symbol={FUT_NAME.get(name, name)}USDT&startTime={start}&limit=1000", tries=2)
        new = [(int(r["fundingTime"]), float(r["fundingRate"])) for r in got] if isinstance(got, list) else []
        if new:
            FUND.mkdir(exist_ok=True)
            with path.open("a", newline="") as f:
                csv.writer(f).writerows(new)
            rows += new
    except Exception:
        FUND_STATE["down"] = True
    return [a for a, _ in rows], [b for _, b in rows]


def aggregate(bars, mult):
    """4H candles -> candles of mult x 4H, keeping only those whose last 4H candle has closed."""
    tf, out, key = mult * TF, [], None
    for t, o, h, l, c, v in bars:
        k = t // tf
        if k != key:
            key = k
            out.append([k * tf, o, h, l, c, v])
        else:
            b = out[-1]
            b[2], b[3], b[4], b[5] = max(b[2], h), min(b[3], l), c, b[5] + v
    if out and bars[-1][0] != out[-1][0] + tf - TF:
        out.pop()
    return out


def notify(url, text):
    if not url:
        return
    try:
        body = json.dumps({"content": text, "text": text}).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}), timeout=10).close()
    except Exception as e:
        print(f"  (通知失敗：{e})")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    global _lock                               # one instance only: two would book every trade twice
    _lock = socket.socket()
    try:
        _lock.bind(("127.0.0.1", 47631))
    except OSError:
        print("機器人已經在另一個視窗執行中，這個視窗不再重複啟動。")
        time.sleep(15)
        return
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    LOGS.mkdir(exist_ok=True)
    sp = LOGS / "pattern_state.json"
    state = json.loads(sp.read_text()) if sp.exists() else {
        "started_at": int(time.time() * 1000), "equity": {k: cfg["start_equity"] for k in PATTERNS}, "entries": [], "exits": []}
    labels = [k for p in PATTERNS for k in (p, p + STRONG, *(f"{p}{STRONG}｜{n}" for n in TFS_EXTRA))]
    state["equity"] = {k: state["equity"].get(k, cfg["start_equity"]) for k in labels}
    syms = sorted(p.stem for p in DATA.glob("*.csv"))
    extra_path = {p.stem + "USDT": p for d in OOS_DIRS if d.exists() for p in sorted(d.glob("*.csv")) if p.stem + "USDT" not in syms}
    extra = sorted(extra_path)
    print(f"4H 形態模擬交易（不會下真單）：{len(PATTERNS)} 種形態，多空都有；4H 每種形態兩個帳戶（原規則、強勢版），強勢版另外跑 8H、12H、日線；"
          f"{len(syms) + len(extra)} 個商品，每筆風險 {RISK_PCT}%。Ctrl+C 停止。")

    def say(ev):
        with (LOGS / "pattern_events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        msg = f"[{ts(ev['t'])}] {ev['symbol']} {ev['event']}: {ev['text']}"
        print(msg)
        notify(cfg.get("notify_webhook"), msg)

    def book(label, sym, T):
        """Report / book one trade of one account. T: t, s, entry, stop, target (None = trailing stop), exit, t_exit, R, cost, volok"""
        if T["t"] < state["started_at"]:
            return False                              # opened before this paper test began
        tid = f"{label}:{sym}:{T['t']}"
        if tid not in state["entries"]:
            state["entries"].append(tid)
            plan = f"目標 {T['target']:g}" if T["target"] is not None else "到 1R 後用 3 ATR 移動止損"
            say({"t": T["t"], "symbol": sym, "pattern": label, "event": "ENTRY",
                 "text": f"〔{label}〕{'做多' if T['s'] > 0 else '做空'}進場 {T['entry']:g}，止損 {T['stop']:g}，{plan}"
                         f"{'，突破有放量' if T['volok'] else ''}，同方向強勢訊號的商品 {T['breadth']} 個"
                         + ("" if T.get("crowd") is None else f"，資金費率擁擠度 {T['crowd'] * 100:+.3f}%")})
        if T["t_exit"] and tid not in state["exits"]:
            state["exits"].append(tid)
            net = T["R"] - T["cost"] - T.get("fund_R", 0.0)      # after fees, slippage and funding paid
            pnl = state["equity"][label] * RISK_PCT / 100 * net
            state["equity"][label] += pnl
            if T["target"] is not None:
                why = "到達目標" if T["exit"] == T["target"] else "止損" if net < 0 else "持倉逾時"
            else:
                why = "止損" if T["R"] <= -0.99 else "移動止損出場" if not T.get("timed") else "持倉逾時"
            row = {"pattern": label, "side": "LONG" if T["s"] > 0 else "SHORT", "volume_confirmed": int(T["volok"]),
                   "symbol": sym, "t_entry": T["t"], "t_exit": T["t_exit"], "entry": T["entry"],
                   "stop": T["stop"], "target": "" if T["target"] is None else T["target"], "exit": T["exit"], "reason": why,
                   "R_gross": round(T["R"], 4), "R_net": round(net, 4), "pnl": round(pnl, 2), "equity": round(state["equity"][label], 2),
                   "breadth": T["breadth"], "timeframe": T.get("tf", "4H"),
                   "crowding": "" if T.get("crowd") is None else round(T["crowd"] * 100, 4), "funding_R": round(T.get("fund_R", 0.0), 4)}
            path = LOGS / "pattern_trades.csv"
            fresh = not path.exists()
            with path.open("a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(row))
                if fresh:
                    w.writeheader()
                w.writerow(row)
            say({"t": T["t_exit"], "symbol": sym, "pattern": label, "event": "EXIT",
                 "text": f"〔{label}〕出場 {T['exit']:g}（{why}），扣成本與資金費率後 {net:+.2f}R，{pnl:+.2f} USDT，該帳戶模擬權益 {state['equity'][label]:,.2f}"})
        return not T["t_exit"]

    while True:
        holding, data, multi, positions, fund = [], {}, {}, [], {}
        FUND_STATE["down"] = False

        def annotate(sym, T):
            """Funding: how crowded the trade's own side was at entry, and funding paid so far (in R)."""
            ts_f, rates = fund.get(sym, ([], []))
            i = bisect.bisect_right(ts_f, T["t"])
            fresh = i >= 3 and T["t"] - ts_f[i - 1] <= 2 * 86_400_000     # stale funding data says nothing about this entry
            T["crowd"] = T["s"] * (sum(rates[i - 3:i]) / 3 - NEUTRAL) if fresh else None
            end = T.get("t_exit") or int(time.time() * 1000)
            T["fund_R"] = T["s"] * sum(rates[i:bisect.bisect_right(ts_f, end)]) * T["entry"] / abs(T["entry"] - T["stop"])

        def hold(label, sym, tfn, T, last, stop_now, r_now):
            """Remember an open paper position for the dashboard."""
            holding.append(f"{sym.replace('USDT', '')}({label})")
            positions.append({"account": label, "symbol": sym, "timeframe": tfn, "side": "LONG" if T["s"] > 0 else "SHORT",
                              "t_entry": T["t"], "entry": T["entry"], "stop0": T["stop"], "stop": stop_now, "target": T["target"],
                              "last": last, "R": round(r_now, 3), "breadth": T.get("breadth"),
                              "crowding": None if T.get("crowd") is None else round(T["crowd"] * 100, 4),
                              "funding_R": round(T.get("fund_R", 0.0), 4)})

        for sym in syms + extra:
            try:
                bars = candles_4h(update(sym)) if sym in syms else update_4h(extra_path[sym])
            except Exception as e:
                print(f"[{ts(time.time() * 1000)}] {sym} 更新資料失敗，下一輪再試：{e}")
                continue
            data[sym] = (bars, *simulate(bars, sym, TF))
            fund[sym] = funding_series(sym[:-4])
            time.sleep(0.2)                            # stay well inside the funding endpoint's rate limit
            multi[sym] = {}
            for tfn, mult in TFS_EXTRA.items():
                b2 = aggregate(bars, mult)
                if len(b2) >= 300:
                    multi[sym][tfn] = (b2, *simulate(b2, sym, mult * TF))

        # breadth: how many different coins gave a strong-breakout signal in the same direction
        # within the last 3 candles of that timeframe. Recorded only, not a rule.
        strong_ok = lambda feat: feat[3] >= STRONG_VOL and feat[2] >= STRONG_BODY
        sig = {tfn: {1: [], -1: []} for tfn in ("4H", *TFS_EXTRA)}

        def collect(tfn, sym, closed, open_tr):
            for r in closed:
                if not r[3] and strong_ok(r[13]):
                    sig[tfn][r[11]].append((r[0], sym))
            for k, T in open_tr.items():
                if not k[1] and strong_ok(T["feat"]):
                    sig[tfn][T["s"]].append((T["t"], sym))

        for sym, (bars, closed, open_tr) in data.items():
            collect("4H", sym, closed, open_tr)
            for tfn, (b2, c2, o2) in multi[sym].items():
                collect(tfn, sym, c2, o2)
        for d in sig.values():
            for lst in d.values():
                lst.sort()

        def breadth(tfn, t, side, tf_ms):
            lst = sig[tfn][side]
            return len({x[1] for x in lst[bisect.bisect_left(lst, (t - 2 * tf_ms, "")):bisect.bisect_right(lst, (t, "~"))]})

        def strong_trade(bars, atr, close_t, t, side, entry, stop, height, tfn, tf_ms):
            i0, info = bisect.bisect_left(close_t, t), {}
            g, cost, px, ix = replay(bars, atr, i0, side, entry, stop, height, STRONG_EXIT, info)
            return {"stop_now": info["stop"], "t": t, "s": side, "entry": entry, "stop": stop, "target": None, "exit": px,
                    "t_exit": close_t[ix] if ix is not None else None, "R": g, "cost": cost, "volok": True,
                    "timed": ix is not None and ix - i0 >= STRONG_EXIT[5], "breadth": breadth(tfn, t, side, tf_ms), "tf": tfn}

        for sym, (bars, closed, open_tr) in data.items():
            short = sym.replace("USDT", "")
            atr, close_t = atr_series(bars), [b[0] + TF for b in bars]
            for label, name in PATTERNS.items():
                key = (name, False)
                live = [dict(T, t_exit=None, height=abs(T["target"] - T["entry"])) for k, T in open_tr.items() if k == key]
                done = [{"t": r[0], "entry": r[6], "stop": r[7], "target": r[8], "exit": r[9], "t_exit": r[10], "R": r[4],
                         "cost": r[5], "s": r[11], "volok": r[12], "feat": r[13], "height": abs(r[8] - r[6])} for r in closed if (r[2], r[3]) == key]
                for T in done + live:
                    T["breadth"] = breadth("4H", T["t"], T["s"], TF)
                    annotate(sym, T)
                    # 1) original rules on 4H: target = pattern height
                    if book(label, sym, T):
                        last = bars[-1][4]
                        hold(label, sym, "4H", T, last, T["stop"], (last - T["entry"]) * T["s"] / abs(T["entry"] - T["stop"]))
                    # 2) strong-breakout version on 4H: same entry, only if the breakout candle is big and on high
                    #    volume; no fixed target: once 1R ahead, trail the stop 3 ATR behind the best price
                    if strong_ok(T["feat"]):
                        S = strong_trade(bars, atr, close_t, T["t"], T["s"], T["entry"], T["stop"], T["height"], "4H", TF)
                        annotate(sym, S)
                        if book(label + STRONG, sym, S):
                            hold(label + STRONG, sym, "4H", S, S["exit"], S["stop_now"], S["R"])
            # 3) the same strong version on 8H, 12H and daily candles
            for tfn, (b2, closed2, open2) in multi[sym].items():
                tf_ms = TFS_EXTRA[tfn] * TF
                atr2, ct2 = atr_series(b2), [b[0] + tf_ms for b in b2]
                for label, name in PATTERNS.items():
                    key = (name, False)
                    ents = [(r[0], r[11], r[6], r[7], abs(r[8] - r[6]), r[13]) for r in closed2 if (r[2], r[3]) == key]
                    ents += [(T["t"], T["s"], T["entry"], T["stop"], abs(T["target"] - T["entry"]), T["feat"]) for k, T in open2.items() if k == key]
                    for t, side, entry, stop, height, feat in ents:
                        if strong_ok(feat):
                            lab = f"{label}{STRONG}｜{tfn}"
                            S = strong_trade(b2, atr2, ct2, t, side, entry, stop, height, tfn, tf_ms)
                            annotate(sym, S)
                            if book(lab, sym, S):
                                hold(lab, sym, tfn, S, S["exit"], S["stop_now"], S["R"])
        sp.write_text(json.dumps(state))
        (LOGS / "pattern_positions.json").write_text(
            json.dumps({"updated": int(time.time() * 1000), "positions": positions}, ensure_ascii=False), encoding="utf-8")
        now = time.time() * 1000
        nxt = (int(now) // TF + 1) * TF + 30_000
        print(f"[{ts(now)}] ✓ 已檢查 {len(syms) + len(extra)} 個商品，持倉 {len(holding)} 筆；"
              f"{sum(v != cfg['start_equity'] for v in state['equity'].values())}/{len(state['equity'])} 個帳戶已有交易，"
              f"合計損益 {sum(v - cfg['start_equity'] for v in state['equity'].values()):+,.0f} USDT。"
              f"下次檢查 {ts(nxt)}")
        try:                                          # keep dashboard.html current; never let it stop the bot
            subprocess.run([sys.executable, str(ROOT / "dashboard.py")], cwd=ROOT, timeout=300,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"  (頁面更新失敗：{e})")
        if "--once" in sys.argv:                      # one cycle and exit: used by the scheduled online run
            return
        time.sleep(max(5, (nxt - time.time() * 1000) / 1000))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已停止。")
