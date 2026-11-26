"""Story / Zone / Strat session strategy engine.

Independent Python implementation of the rules described in the public
specification (2-candle bias, body-based floors & ceilings, clean zones,
range -> breakout candle -> rubber-band dip -> stop order, 1R breakeven).

The engine is fed CLOSED 15-minute candles one at a time and is fully
deterministic, so the same code path serves the backtest and the live
paper-trading loop. It never talks to an exchange.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

M15 = 900_000
TF_MS = {"15m": M15, "1h": 4 * M15, "4h": 16 * M15, "1d": 96 * M15, "1w": 672 * M15}
WEEK_OFFSET = 4 * 86_400_000  # 1970-01-05 was a Monday

DEFAULTS = {
    "sessions": ["ASIA", "LONDON", "NYSE"],
    "trade_weekends": True,
    "max_trades_per_session": 2,
    "mixed_rule": "story",          # story | daily | none
    "require_clean_zone": True,
    "block_at_daily_level": True,
    "pullback_rule": True,
    "pullback_depth": 0.382,
    "range_max": 20,
    "range_min": 3,
    "range_min_opp": 2,
    "range_min_atr": 0.5,
    "range_max_atr": 4.0,
    "bo_body_pct": 0.5,
    "bo_max_range_frac": 0.5,
    "wick_atr": 0.10,
    "wick_frac": 0.20,
    "order_expiry_bars": 6,
    "partial_at_1r": 0.5,
    "target_cap_r": 3.0,
    "too_fast_r": 1.5,
    "too_fast_bars": 3,
    "max_hold_min": 240,
    "zone_min_candles": 3,
    "zone_min_atr": 1.5,
    # research switches (defaults reproduce the original indicator)
    "min_stop_pct": 0.0,            # skip setups whose stop is closer than this % of price
    "stop_mode": "candle",          # candle = far side of the breakout candle, range = far side of the range
    "be_at_1r": True,               # move the stop to entry at 1R / at a level before 1R
    "too_fast_exit": True,
    "window_min": 60,               # how long after each session open setups are allowed
    "vol_mult": 0.0,                # breakout candle volume >= this x the average of the last vol_len candles
    "vol_len": 20,
    "regime_min": 0.0,              # ATR now / its average over regime_len candles must be at least this
    "regime_len": 960,
    "fib": 0.0,                     # with entry_mode limit: retracement level (0.382 / 0.5 / 0.618) to buy at
    "entry_mode": "stop",           # stop = buy the breakout after a dip; limit = resting order at the range edge
}


@dataclass(slots=True)
class Bar:
    t: int  # open time, ms UTC
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0


def sign(x: float) -> int:
    return (x > 0) - (x < 0)


# ----------------------------------------------------------------- sessions
def _nth_sunday(y: int, m: int, n: int) -> date:
    d = date(y, m, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_sunday(y: int, m: int) -> date:
    d = date(y, m + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def uk_dst(d: date) -> bool:
    return _last_sunday(d.year, 3) <= d < _last_sunday(d.year, 10)


def us_dst(d: date) -> bool:
    return _nth_sunday(d.year, 3, 2) <= d < _nth_sunday(d.year, 11, 1)


def session_at(t_ms: int, enabled, weekends=True, window=60):
    """Session whose first hour contains the candle opening at t_ms, else None."""
    dt = datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc)
    d = dt.date()
    if not weekends and d.weekday() >= 5:
        return None
    m = dt.hour * 60 + dt.minute
    starts = {
        "ASIA": 0,                                    # 09:00 Tokyo
        "LONDON": 7 * 60 if uk_dst(d) else 8 * 60,    # 08:00 London
        "NYSE": 13 * 60 + 30 if us_dst(d) else 14 * 60 + 30,  # 09:30 New York
    }
    for name in enabled:
        if starts[name] <= m < starts[name] + window:
            return name
    return None


# --------------------------------------------------------------- indicators
class ATR:
    def __init__(self, n=14):
        self.n, self.v, self.pc, self.buf = n, None, None, []

    def update(self, b: Bar):
        tr = b.h - b.l if self.pc is None else max(b.h - b.l, abs(b.h - self.pc), abs(b.l - self.pc))
        self.pc = b.c
        if self.v is None:
            self.buf.append(tr)
            if len(self.buf) == self.n:
                self.v = sum(self.buf) / self.n
        else:
            self.v = (self.v * (self.n - 1) + tr) / self.n


class Aggregator:
    """Builds higher-timeframe candles from 15m candles; returns the closed one."""

    def __init__(self, tf):
        self.tf, self.cur, self.key = tf, None, None

    def push(self, b: Bar):
        key = (b.t - WEEK_OFFSET) // TF_MS[self.tf] if self.tf == "1w" else b.t // TF_MS[self.tf]
        closed = None
        if key != self.key:
            closed, self.key = self.cur, key
            self.cur = Bar(b.t, b.o, b.h, b.l, b.c)
        else:
            c = self.cur
            c.h, c.l, c.c = max(c.h, b.h), min(c.l, b.l), b.c
        return closed


@dataclass
class Level:
    tf: str
    side: str  # "C" ceiling / "F" floor
    price: float
    t: int
    touches: int = 0
    broken: bool = False


class LevelBook:
    def __init__(self, tf, n):
        self.tf, self.n, self.candles, self.levels, self.atr = tf, n, [], [], ATR()

    def on_close(self, b: Bar):
        """Update breaks/touches, detect a new level. Returns the new Level or None."""
        self.atr.update(b)
        self.candles.append(b)
        del self.candles[:-2 * self.n]
        for L in self.levels:
            if L.side == "C":
                if b.c > L.price:
                    L.broken = True
                elif b.h >= L.price:
                    L.touches += 1
            else:
                if b.c < L.price:
                    L.broken = True
                elif b.l <= L.price:
                    L.touches += 1
        self.levels = [L for L in self.levels if not L.broken]
        n, w = self.n, self.candles
        new = None
        if len(w) == 2 * n:
            first, last = w[:n], w[n:]
            if all(x.c > x.o for x in first) and all(x.c < x.o for x in last):
                new = Level(self.tf, "C", max(max(x.o, x.c) for x in w), b.t)
            elif all(x.c < x.o for x in first) and all(x.c > x.o for x in last):
                new = Level(self.tf, "F", min(min(x.o, x.c) for x in w), b.t)
        if new:
            tol = 0.15 * (self.atr.v or 0)
            for L in self.levels:
                if L.side == new.side and abs(L.price - new.price) <= tol:
                    L.touches += 1
                    return None
            self.levels.append(new)
            same = [L for L in self.levels if L.side == new.side]
            for old in same[:-3]:
                self.levels.remove(old)
        return new


class Staircase:
    def __init__(self, k=2):
        self.k, self.c, self.highs, self.lows = k, [], [], []

    def on_close(self, b: Bar):
        k = self.k
        self.c.append(b)
        del self.c[:-(2 * k + 1)]
        if len(self.c) == 2 * k + 1:
            mid = self.c[k]
            if mid.h == max(x.h for x in self.c):
                self.highs = (self.highs + [mid.h])[-2:]
            if mid.l == min(x.l for x in self.c):
                self.lows = (self.lows + [mid.l])[-2:]

    def state(self):
        if len(self.highs) < 2 or len(self.lows) < 2:
            return None
        if self.highs[1] > self.highs[0] and self.lows[1] > self.lows[0]:
            return "UP"
        if self.highs[1] < self.highs[0] and self.lows[1] < self.lows[0]:
            return "DOWN"
        return "BOX"


# ------------------------------------------------------------------- engine
class Engine:
    def __init__(self, symbol="?", cfg=None):
        self.symbol = symbol
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.aggs = {tf: Aggregator(tf) for tf in ("1h", "4h", "1d", "1w")}
        self.books = {"1d": LevelBook("1d", 1), "4h": LevelBook("4h", 1),
                      "1h": LevelBook("1h", 2), "15m": LevelBook("15m", 2)}
        self.last = {"1d": None, "4h": None}      # last closed HTF candles
        self.stair_w, self.stair_d = Staircase(), Staircase()
        self.hist: list[Bar] = []
        self.zones: list[list[float]] = []        # [lo, hi]
        self.move = None                          # clean-move tracker
        self.sess_id = None
        self.sess = None                          # frozen session context
        self.zone_pending = None
        self.day = None
        self.atr_hist: list[float] = []
        self.atr_sum = 0.0
        self.blk = {1: False, -1: False}
        self.order = None
        self.trade = None
        self.trades: list[dict] = []

    # -- helpers
    @property
    def atr(self):
        return self.books["15m"].atr.v

    def _levels(self):
        for bk in self.books.values():
            yield from bk.levels

    def _ev(self, out, b, kind, text, **kw):
        out.append({"t": b.t + M15, "symbol": self.symbol, "event": kind, "text": text, **kw})

    def _zone_update(self, b: Bar):
        d, mv = sign(b.c - b.o), self.move
        if d == 0:
            return
        if mv is None:
            self.move = {"d": d, "n": 1, "start": b.o, "ext": b.c, "opp": []}
        elif d == mv["d"]:
            mv["n"] += 1
            mv["ext"] = max(mv["ext"], b.c) if d > 0 else min(mv["ext"], b.c)
            mv["opp"] = []
        else:
            mv["opp"].append(b)
            if len(mv["opp"]) == 2:
                size = abs(mv["ext"] - mv["start"])
                if mv["n"] >= self.cfg["zone_min_candles"] and self.atr and size >= self.cfg["zone_min_atr"] * self.atr:
                    self.zone_pending = sorted([mv["start"], mv["ext"]])
                o1, o2 = mv["opp"]
                self.move = {"d": d, "n": 2, "start": o1.o, "opp": [],
                             "ext": max(o1.c, o2.c) if d > 0 else min(o1.c, o2.c)}

    def _zone_kill(self, price):
        keep = []
        for lo, hi in self.zones:
            pad = 0.08 * (hi - lo)
            if not (lo + pad < price < hi - pad):
                keep.append([lo, hi])
        self.zones = keep

    def _at_daily(self, price):
        bk = self.books["1d"]
        a = 0.25 * (bk.atr.v or 0)
        at_c = any(L.side == "C" and price >= L.price - a for L in bk.levels)
        at_f = any(L.side == "F" and price <= L.price + a for L in bk.levels)
        return at_c, at_f

    # -- session story
    def _freeze_bias(self, b: Bar, name, out):
        D, H4 = self.last["1d"], self.last["4h"]
        ctx = {"name": name, "bias": 0, "count": 0, "mode": "BREAKOUT", "h4": H4, "reason": "資料不足"}
        self.sess = ctx
        if not D or not H4 or not self.atr:
            return
        dd, hd = sign(D.c - D.o), sign(H4.c - H4.o)
        col = {1: "綠", -1: "紅", 0: "平"}
        stair = self.stair_w.state() or self.stair_d.state()
        if dd == hd != 0:
            bias, why = dd, f"昨日日K與盤前4H K都是{col[dd]}色"
        else:
            rule = self.cfg["mixed_rule"]
            at_c, at_f = self._at_daily(b.c)
            if rule == "none":
                bias, why = 0, "兩根K棒方向不同，設定為觀望"
            elif rule == "daily":
                bias, why = dd, "兩根K棒方向不同，以日K為準"
            elif at_c and not at_f:
                bias, why = -1, "兩根K棒方向不同，價格位於日線天花板"
            elif at_f and not at_c:
                bias, why = 1, "兩根K棒方向不同，價格位於日線地板"
            elif stair in ("UP", "DOWN"):
                bias, why = (1 if stair == "UP" else -1), f"兩根K棒方向不同，週線階梯向{'上' if stair == 'UP' else '下'}"
            else:
                bias, why = (dd or hd), "兩根K棒方向不同，以日K為準"
        rng = H4.h - H4.l
        h4atr = self.books["4h"].atr.v
        ran = rng > 0 and abs(H4.c - H4.o) >= 0.6 * rng and h4atr and rng >= h4atr
        if self.cfg["pullback_rule"] and ran and sign(H4.c - H4.o) == bias:
            ctx["mode"] = "PULLBACK"
        ctx.update(bias=bias, reason=why)
        side = {1: "只做多", -1: "只做空", 0: "不交易"}[bias]
        self._ev(out, b, "SESSION_BIAS", f"{name} 時段：{side}（{why}；模式 {ctx['mode']}）", bias=bias)

    # -- setup detection
    def _find_range(self, b: Bar, s: int):
        cfg, atr, h = self.cfg, self.atr, self.hist
        top, bot, opp, best = float("-inf"), float("inf"), 0, None
        for k in range(1, min(cfg["range_max"], len(h)) + 1):
            x = h[-k]
            top, bot = max(top, x.o, x.c), min(bot, x.o, x.c)
            if (x.c - x.o) * s < 0:
                opp += 1
            if top - bot > cfg["range_max_atr"] * atr:
                break
            beyond = b.c > top if s > 0 else b.c < bot
            if k >= cfg["range_min"] and opp >= cfg["range_min_opp"] and top - bot >= cfg["range_min_atr"] * atr and beyond:
                best = (k, top, bot)
        return best

    def _look_for_breakout(self, b: Bar, out):
        cfg, ctx = self.cfg, self.sess
        s = ctx["bias"]
        if s == 0 or ctx["count"] >= cfg["max_trades_per_session"] or not self.atr:
            return
        rng = b.h - b.l
        if rng <= 0 or (b.c - b.o) * s <= 0 or abs(b.c - b.o) < cfg["bo_body_pct"] * rng:
            return
        r = self._find_range(b, s)
        if not r:
            return
        k, top, bot = r
        skip = None
        if self.blk[s]:
            skip = "價格頂在日線關卡，今天不做，明天再來"
        elif rng > cfg["bo_max_range_frac"] * (top - bot):
            skip = "突破K棒超過區間高度的50%"
        elif ctx["mode"] == "PULLBACK":
            H4 = ctx["h4"]
            depth = cfg["pullback_depth"] * (H4.h - H4.l)
            if not (bot <= H4.h - depth if s > 0 else top >= H4.l + depth):
                skip = "盤前4H已經跑過，等待回檔"
        if not skip and cfg["require_clean_zone"]:
            p = b.h if s > 0 else b.l
            if not any(lo <= p <= hi for lo, hi in self.zones):
                skip = "不在乾淨區內"
        if not skip and cfg["vol_mult"] > 0:
            seg = self.hist[-cfg["vol_len"]:]
            if b.v < cfg["vol_mult"] * sum(x.v for x in seg) / len(seg):
                skip = "突破K棒成交量不足"
        if not skip and cfg["regime_min"] > 0 and self.atr * len(self.atr_hist) < cfg["regime_min"] * self.atr_sum:
            skip = "近期波動太低"
        if skip:
            self._ev(out, b, "SKIP", f"略過突破：{skip}")
            return
        price, stop = (b.h, b.l) if s > 0 else (b.l, b.h)
        if cfg["stop_mode"] == "range":
            stop = min(stop, bot) if s > 0 else max(stop, top)
        if cfg["entry_mode"] == "limit":     # wait for price to come back to the edge it broke
            price, stop = (top, bot) if s > 0 else (bot, top)
            if cfg["fib"] > 0:               # Fibonacci retracement of the leg: range far side -> breakout extreme
                price = b.h - cfg["fib"] * (b.h - bot) if s > 0 else b.l + cfg["fib"] * (top - b.l)
        if abs(price - stop) / price * 100 < cfg["min_stop_pct"]:
            self._ev(out, b, "SKIP", f"略過突破：止損距離小於 {cfg['min_stop_pct']}%")
            return
        self.order = {"s": s, "price": price, "stop": stop, "top": top, "bot": bot,
                      "bars": 0, "dip": False, "t": b.t + M15, "session": ctx["name"], "limit": cfg["entry_mode"] == "limit"}
        kind = "限價單" if self.order["limit"] else "停損單"
        self._ev(out, b, "STOP_ORDER_PLACED",
                 f"{k}根K棒區間被突破，掛{'買' if s > 0 else '賣'}{kind} {price:g}，止損 {stop:g}，等回踩", price=price, stop=stop)

    def _work_order(self, b: Bar, out):
        o, cfg = self.order, self.cfg
        s, price = o["s"], o["price"]
        o["bars"] += 1
        if o["limit"]:                       # filled only if price trades THROUGH the limit
            if (b.l < price) if s > 0 else (b.h > price):
                return self._fill(b, out, min(b.o, price) if s > 0 else max(b.o, price))
            if o["bars"] >= cfg["order_expiry_bars"]:
                self.order = None
                self._ev(out, b, "STOP_ORDER_CANCELLED", "取消掛單：沒有回踩，掛單逾時")
            return
        rng = b.h - b.l
        wick = (min(b.o, b.c) - b.l) if s > 0 else (b.h - max(b.o, b.c))
        wick_ok = wick >= cfg["wick_atr"] * self.atr and rng > 0 and wick >= cfg["wick_frac"] * rng
        hit = b.h >= price if s > 0 else b.l <= price
        cancel = None
        if hit:
            if o["dip"] or (wick_ok and (b.o - price) * s < 0):
                return self._fill(b, out)
            cancel = "沒有先回踩就直接突破"
        else:
            if wick_ok:
                o["dip"] = True
            if (b.c < o["top"]) if s > 0 else (b.c > o["bot"]):
                cancel = "收盤回到區間內"
            elif o["bars"] >= cfg["order_expiry_bars"]:
                cancel = "掛單逾時"
        if cancel:
            self.order = None
            self._ev(out, b, "STOP_ORDER_CANCELLED", f"取消掛單：{cancel}")

    def _fill(self, b: Bar, out, entry=None):
        o, cfg = self.order, self.cfg
        self.order = None
        s = o["s"]
        if entry is None:
            entry = max(b.o, o["price"]) if s > 0 else min(b.o, o["price"])
        risk = abs(entry - o["stop"])
        if risk <= 0:
            return
        r1, cap = entry + s * risk, entry + s * cfg["target_cap_r"] * risk
        target, src, pre = cap, "3R上限", None
        side = "C" if s > 0 else "F"         # ceilings ahead for buys, floors for sells
        far_from, pre_from = r1 - s * 0.02 * risk, entry + s * 0.05 * risk
        for L in self._levels():
            if L.side != side:
                continue
            if (L.price - far_from) * s > 0 and (target - L.price) * s > 0:
                target, src = L.price, f"{L.tf} {'天花板' if s > 0 else '地板'}"
            if (L.price - pre_from) * s > 0 and (pre is None or (pre - L.price) * s > 0):
                pre = L.price
        if pre is not None and (r1 - pre) * s <= 0:
            pre = None
        self.sess["count"] += 1
        self.trade = {"symbol": self.symbol, "s": s, "side": "LONG" if s > 0 else "SHORT", "session": o["session"],
                      "t_entry": b.t + M15, "t_open": b.t, "entry": entry, "stop0": o["stop"], "stop": o["stop"], "risk": risk,
                      "r1": r1, "target": target, "target_src": src, "pre": pre,
                      "banked": False, "be": False, "streak": 0, "prev_c": b.c, "maker": o["limit"]}
        self._ev(out, b, "ENTRY", f"{'做多' if s > 0 else '做空'}進場 {entry:g}，止損 {o['stop']:g}，1R {r1:g}，目標 {target:g}（{src}）",
                 entry=entry, stop=o["stop"], target=target, side=self.trade["side"])
        worst = (b.l if s > 0 else b.h) if o["limit"] else b.c   # a limit fill has the rest of the candle ahead of it
        if (worst - o["stop"]) * s <= 0:
            self._exit(b, o["stop"], "進場K棒直接打到止損", out)

    def _exit(self, b: Bar, px, reason, out):
        T, p = self.trade, self.cfg["partial_at_1r"]
        self.trade = None
        banked = p if T["banked"] else 0.0
        rest = 1 - banked
        T["R"] = banked + rest * (px - T["entry"]) * T["s"] / T["risk"]
        T["exit"], T["t_exit"], T["reason"] = px, b.t + M15, reason
        T["exit_notional"] = banked * T["r1"] + rest * px   # per unit, for fee model
        self.trades.append(T)
        self._ev(out, b, "EXIT", f"出場 {px:g}（{reason}），結果 {T['R']:+.2f}R", R=T["R"], trade=T)

    def _manage(self, b: Bar, out):
        T, cfg = self.trade, self.cfg
        s, e = T["s"], T["entry"]
        adv, fav = (b.l, b.h) if s > 0 else (b.h, b.l)
        if (adv - T["stop"]) * s <= 0:
            px = T["stop"] if (b.o - T["stop"]) * s > 0 else b.o
            return self._exit(b, px, "保本出場" if T["be"] else "止損", out)
        if not T["banked"] and (fav - T["r1"]) * s >= 0:
            T["banked"] = True
            if cfg["be_at_1r"]:
                T["be"], T["stop"] = True, e
            self._ev(out, b, "BREAKEVEN", f"到達1R，先獲利了結{cfg['partial_at_1r']:.0%}，止損移到進場價")
        if cfg["be_at_1r"] and not T["be"] and T["pre"] is not None and (fav - T["pre"]) * s >= 0:
            T["be"], T["stop"] = True, e
            self._ev(out, b, "BREAKEVEN", "碰到1R前的關卡，止損移到進場價")
        if (fav - T["target"]) * s >= 0:
            return self._exit(b, T["target"], "到達目標", out)
        T["streak"] = T["streak"] + 1 if (b.c - b.o) * s > 0 and (b.c - T["prev_c"]) * s > 0 else 0
        T["prev_c"] = b.c
        if cfg["too_fast_exit"] and (b.c - e) * s / T["risk"] >= cfg["too_fast_r"] and T["streak"] >= cfg["too_fast_bars"]:
            return self._exit(b, b.c, "漲跌太快，提前獲利了結", out)
        if cfg["max_hold_min"] and b.t + M15 - T["t_open"] >= cfg["max_hold_min"] * 60_000:
            return self._exit(b, b.c, "持倉逾時", out)

    # -- main entry point
    def on_bar(self, b: Bar) -> list[dict]:
        out: list[dict] = []
        for tf, ag in self.aggs.items():
            closed = ag.push(b)
            if not closed:
                continue
            if tf == "1w":
                self.stair_w.on_close(closed)
                continue
            if tf == "1d":
                self.stair_d.on_close(closed)
            if tf in self.last:
                self.last[tf] = closed
            new = self.books[tf].on_close(closed)
            if new and tf != "1d":           # only 4H and 1H levels make a clean zone messy
                self._zone_kill(new.price)
        self.books["15m"].on_close(b)
        if self.atr:
            self.atr_hist.append(self.atr)
            self.atr_sum += self.atr
            if len(self.atr_hist) > self.cfg["regime_len"]:
                self.atr_sum -= self.atr_hist.pop(0)
        if self.zone_pending:                # zones appear one candle after the move ended
            self.zones.append(self.zone_pending)
            del self.zones[:-8]
            self.zone_pending = None
        self._zone_update(b)

        day = b.t // TF_MS["1d"]
        if day != self.day:
            self.day, self.blk = day, {1: False, -1: False}

        name = session_at(b.t, self.cfg["sessions"], self.cfg["trade_weekends"], self.cfg["window_min"])
        if name:
            sid = (name, b.t // TF_MS["1d"])
            if sid != self.sess_id:
                self.sess_id = sid
                self._freeze_bias(b, name, out)
            if self.cfg["block_at_daily_level"] and self.sess and self.sess["bias"]:
                at_c, at_f = self._at_daily(b.c)
                if (self.sess["bias"] > 0 and at_c) or (self.sess["bias"] < 0 and at_f):
                    self.blk[self.sess["bias"]] = True

        if self.trade:
            self._manage(b, out)
        elif self.order:
            self._work_order(b, out)
        elif name and self.sess and self.sess["name"] == name:
            self._look_for_breakout(b, out)

        self.hist.append(b)
        del self.hist[:-64]
        return out
