"""Write STATUS.md: a plain summary of the paper test that GitHub shows directly (also in the mobile app).

    python status_md.py
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
TPE = timezone(timedelta(hours=8))
SIDE = {"LONG": "做多", "SHORT": "做空"}


def ts(ms):
    return datetime.fromtimestamp(ms / 1000, tz=TPE).strftime("%m-%d %H:%M")


def price(v):
    return f"{v:,.1f}" if v >= 1000 else f"{v:.3f}" if v >= 1 else f"{v:.4g}"


def r_(v, d=2):
    return f"{v:+.{d}f}R"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    state = json.loads((LOGS / "pattern_state.json").read_text())
    pf = LOGS / "pattern_positions.json"
    pos = json.loads(pf.read_text(encoding="utf-8")) if pf.exists() else {"updated": None, "positions": []}
    P = sorted((p for p in pos["positions"] if p["t_entry"] >= state["started_at"]), key=lambda p: (p["symbol"], p["t_entry"]))
    tp = LOGS / "pattern_trades.csv"
    T = list(csv.DictReader(tp.open(encoding="utf-8"))) if tp.exists() else []
    for t in T:
        t["R"] = float(t["R_net"])
    start = cfg["start_equity"]
    eq = state["equity"]
    out = ["# 形態模擬交易狀態", "",
           f"更新時間：{ts(pos['updated']) if pos['updated'] else '—'}（台北時間）。每 4 小時自動更新一次。",
           "", "全部是模擬資金，沒有真實下單，也不是投資建議。", ""]

    n = len(T)
    tot = sum(t["R"] for t in T)
    out += ["## 總覽", "", "| 項目 | 數值 |", "|---|---|",
            f"| 開始實測 | {ts(state['started_at'])} |",
            f"| 已結束交易 | {n} 筆 |",
            f"| 勝率 | {sum(t['R'] > 0.05 for t in T) / n:.1%} |" if n else "| 勝率 | — |",
            f"| 每筆平均 | {r_(tot / n)} |" if n else "| 每筆平均 | — |",
            f"| 合計 | {r_(tot, 1)} |",
            f"| 持倉中 | {len(P)} 筆，{len({p['symbol'] for p in P})} 個商品 |",
            f"| 持倉浮動損益合計 | {r_(sum(p['R'] for p in P), 1)} |",
            f"| 有交易的帳戶 | {sum(v != start for v in eq.values())} / {len(eq)} |",
            f"| 全部帳戶合計損益 | {sum(v - start for v in eq.values()):+,.0f} USDT |", ""]

    out += ["## 持倉中", ""]
    if P:
        out += ["| 商品 | 帳戶 | 方向 | 進場時間 | 進場價 | 現價 | 目前止損 | 目標 | 浮動損益 |", "|---|---|---|---|---|---|---|---|---|"]
        for p in P:
            tgt = price(p["target"]) if p["target"] is not None else "移動止損"
            out.append(f"| {p['symbol'].replace('USDT', '')} | {p['account']} | {SIDE[p['side']]} | {ts(p['t_entry'])} | {price(p['entry'])} | "
                       f"{price(p['last'])} | {price(p['stop'])} | {tgt} | {r_(p['R'])} |")
    else:
        out.append("目前沒有持倉。")
    out.append("")

    out += ["## 規則與週期比較（已結束交易）", "", "| 組別 | 筆數 | 勝率 | 每筆平均 | 合計 |", "|---|---|---|---|---|"]
    groups = [("4H 原規則", lambda t: "強勢" not in t["pattern"]),
              ("4H 強勢版", lambda t: "強勢" in t["pattern"] and (t.get("timeframe") or "4H") == "4H"),
              ("8H 強勢版", lambda t: t.get("timeframe") == "8H"), ("12H 強勢版", lambda t: t.get("timeframe") == "12H"),
              ("日線 強勢版", lambda t: t.get("timeframe") == "日線")]
    for name, keep in groups:
        g = [t["R"] for t in T if keep(t)]
        out.append(f"| {name} | {len(g)} | {sum(x > 0.05 for x in g) / len(g):.1%} | {r_(sum(g) / len(g))} | {r_(sum(g), 1)} |" if g
                   else f"| {name} | 0 | — | — | — |")
    out.append("")

    out += ["## 最近 20 筆已結束交易", ""]
    if T:
        out += ["| 出場時間 | 商品 | 帳戶 | 方向 | 結果 | 出場原因 |", "|---|---|---|---|---|---|"]
        for t in sorted(T, key=lambda t: int(t["t_exit"]), reverse=True)[:20]:
            out.append(f"| {ts(int(t['t_exit']))} | {t['symbol'].replace('USDT', '')} | {t['pattern']} | {SIDE[t['side']]} | {r_(t['R'])} | {t['reason']} |")
    else:
        out.append("還沒有已結束的交易。")
    out.append("")

    moved = sorted(((v - start, k) for k, v in eq.items() if v != start), reverse=True)
    out += ["## 各帳戶損益（只列有交易的）", ""]
    if moved:
        out += ["| 帳戶 | 損益 USDT |", "|---|---|"] + [f"| {k} | {d:+,.0f} |" for d, k in moved]
    else:
        out.append("還沒有帳戶出現損益。")
    out += ["", "完整圖表在每次執行的 `dashboard` 附件裡（Actions 分頁 → 最新一次執行 → Artifacts），或在電腦上執行 `view_dashboard.bat`。", ""]
    (ROOT / "STATUS.md").write_text("\n".join(out), encoding="utf-8")
    print(f"已寫出 STATUS.md（持倉 {len(P)} 筆，已結束 {n} 筆）")


if __name__ == "__main__":
    main()
