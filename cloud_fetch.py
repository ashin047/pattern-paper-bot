"""Make sure 4H candles (and, where reachable, funding history) exist for every coin.

    python cloud_fetch.py

The scheduled online run keeps data_oos/ and data_funding/ in a cache between runs, so this
normally does nothing. On a fresh machine (or if the cache was lost) it downloads the full
history since 2018 from the public market-data API. No API key, no account.
"""
from __future__ import annotations

import csv
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from bot.data import BASE, _get

ROOT = Path(__file__).resolve().parent
OOS, FUND = ROOT / "data_oos", ROOT / "data_funding"
TF = 14_400_000
START = int(datetime(2018, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
FUND_API = "https://fapi.binance.com/fapi/v1/fundingRate"
FUT_NAME = {"SHIB": "1000SHIB"}
COINS = ["ADA", "BNB", "BTC", "DOGE", "ETH", "LINK", "SOL", "XRP",
         "AVAX", "DOT", "LTC", "ATOM", "TRX", "UNI", "NEAR", "BCH", "ETC", "XLM", "FIL", "AAVE", "ALGO", "VET",
         "ICP", "APT", "ARB", "OP", "SUI", "INJ", "HBAR", "EOS", "ZEC", "DASH", "NEO", "IOTA", "QTUM", "XTZ",
         "SHIB", "MATIC", "SAND", "MANA", "AXS", "THETA", "EGLD", "FLOW", "CHZ", "ENJ", "ZIL", "KAVA", "RUNE", "SNX",
         "COMP", "MKR", "CRV", "LDO", "IMX", "STX", "SEI", "TIA", "GALA", "ONT", "ZRX", "BAT", "KSM", "CAKE",
         "1INCH", "SUSHI", "GRT", "FET"]


def candles(sym):
    path = OOS / f"{sym}.csv"
    if path.exists() and path.stat().st_size > 0:
        return "ok"
    end = int(time.time() * 1000) // TF * TF

    def chunk(t):
        try:
            return _get(f"{BASE}?symbol={sym}USDT&interval=4h&startTime={t}&limit=1000", tries=4)
        except Exception:
            return None
    rows, failed = {}, False
    with ThreadPoolExecutor(8) as ex:
        for got in ex.map(chunk, range(START, end, 1000 * TF)):
            if got is None:
                failed = True
                continue
            for r in got:
                if r[0] < end:
                    rows[r[0]] = (int(r[0]), r[1], r[2], r[3], r[4], r[5])
    if failed or len(rows) < 500:
        return "failed"                          # never save a history with holes in it
    with path.open("w", newline="") as f:
        csv.writer(f).writerows(rows[k] for k in sorted(rows))
    return "downloaded"


def funding(sym):
    path = FUND / f"{sym}.csv"
    if path.exists() and path.stat().st_size > 0:
        return "ok"
    rows, t = [], int(datetime(2019, 9, 1, tzinfo=timezone.utc).timestamp() * 1000)
    while True:
        got = _get(f"{FUND_API}?symbol={FUT_NAME.get(sym, sym)}USDT&startTime={t}&limit=1000", tries=1)
        time.sleep(0.7)                          # about 500 requests per 5 minutes are allowed
        if not isinstance(got, list) or not got:
            break
        rows += [(int(r["fundingTime"]), r["fundingRate"]) for r in got]
        if len(got) < 1000:
            break
        t = rows[-1][0] + 1
    if rows:
        with path.open("w", newline="") as f:
            csv.writer(f).writerows(rows)
    return "downloaded" if rows else "none"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    OOS.mkdir(exist_ok=True)
    FUND.mkdir(exist_ok=True)
    res = {s: candles(s) for s in COINS}
    bad = [s for s, r in res.items() if r == "failed"]
    print(f"K 線：{sum(r == 'ok' for r in res.values())} 個已有，{sum(r == 'downloaded' for r in res.values())} 個新下載"
          + (f"，{len(bad)} 個失敗：{', '.join(bad)}" if bad else ""))
    if len(bad) > len(COINS) // 2:
        sys.exit("大部分商品的 K 線抓不到，這一輪不執行（避免用殘缺資料記帳）。")
    got = 0
    try:
        for s in COINS:
            got += funding(s) == "downloaded"
        print(f"資金費率：{sum((FUND / f'{s}.csv').exists() for s in COINS)} 個商品有資料（新下載 {got} 個）")
    except Exception as e:
        print(f"Binance 的資金費率從這裡連不到（{type(e).__name__}）；機器人會改用 OKX、Bitget 的資金費率。")


if __name__ == "__main__":
    main()
