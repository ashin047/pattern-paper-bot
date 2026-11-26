"""Public market data (no API key, read-only) with a local CSV cache."""
from __future__ import annotations

import csv
import json
import time
import urllib.request
from pathlib import Path

from .engine import M15, Bar

BASE = "https://data-api.binance.vision/api/v3/klines"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _get(url: str, tries=5):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "session-bot/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 ** i)


def fetch(symbol: str, start_ms: int, end_ms: int | None = None) -> list[Bar]:
    """Closed 15m candles with open time in [start_ms, end_ms)."""
    now = int(time.time() * 1000)
    end_ms = min(end_ms or now, now)
    out: list[Bar] = []
    t = start_ms - start_ms % M15
    while t < end_ms:
        rows = _get(f"{BASE}?symbol={symbol}&interval=15m&startTime={t}&limit=1000")
        if not rows:
            break
        for r in rows:
            if r[0] + M15 <= end_ms:          # closed candles only
                out.append(Bar(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])))
        t = rows[-1][0] + M15
        if len(rows) < 1000:
            break
    return out


def history(symbol: str, days: int) -> list[Bar]:
    """Cached history: reuses data/<symbol>_15m.csv and appends what is missing."""
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"{symbol}_15m.csv"
    start = int(time.time() * 1000) - days * 86_400_000
    bars: list[Bar] = []
    if path.exists():
        with path.open() as f:
            bars = [Bar(int(r[0]), *map(float, r[1:5])) for r in csv.reader(f)]
    if not bars or bars[0].t > start + M15:
        bars = fetch(symbol, start)
    else:
        bars += fetch(symbol, bars[-1].t + M15)
    with path.open("w", newline="") as f:
        csv.writer(f).writerows((b.t, b.o, b.h, b.l, b.c) for b in bars)
    return [b for b in bars if b.t >= start]
