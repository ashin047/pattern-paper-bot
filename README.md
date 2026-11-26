# 形態模擬交易（線上版）

每 4 小時由 GitHub Actions 自動執行一次，不需要自己的電腦開著。這個 repo 是公開的。全部是模擬資金：沒有交易所帳號、沒有 API 金鑰、不會下任何真單。這不是投資建議。

## 怎麼看結果

- **完整互動圖表（網頁）**：https://ashin047.github.io/pattern-paper-bot/ 每次執行後自動更新。

- **[STATUS.md](STATUS.md)**：目前持倉、最近的交易、各組比較。每次執行後自動更新，在 GitHub 網站或手機 App 直接看。
- **[TRADES.md](TRADES.md)**：每一筆已結束交易各一張 K 線圖（用它自己的進場週期，標出進場、止損、目標、出場），依月份分頁。持倉的圖在 STATUS.md 裡。
- **在電腦上看**：雙擊 `view_dashboard.bat`，它會同步最新紀錄、重建圖表頁並打開。

## 它在做什麼

- 68 個加密貨幣，16 種 K 線形態（8 多、8 空）。
- 4 小時線：每種形態三個模擬帳戶：原規則（目標是形態等幅）、強勢版（只做放量的強勢突破，到 1R 後用 3 ATR 移動止損）、強勢四段停利。
- 強勢四段停利：和強勢版同一批進場，只有出場不同。1R、2R、3R 各出 25%，第一次停利後止損移到進場價，最後 25% 用 3 ATR 移動止損。
- 強勢版和強勢四段停利另外在 8 小時、12 小時、日線各跑一組。共 144 個帳戶，每個從 10,000 USDT 開始，每筆風險 0.5%。
- 每筆記錄同方向強勢訊號的商品數、資金費率擁擠度與資金成本。這些只記錄，不影響進出場。
- 資金費率來源：Binance 連得到時用 Binance；線上執行的伺服器被 Binance 擋掉，所以改用 OKX，OKX 沒有的商品用 Bitget。EOS、MATIC、MKR 三個商品沒有資料。回測研究用的是 Binance 的資金費率，OKX 的數值相近但不完全相同。

## 檔案

| 檔案 | 用途 |
|---|---|
| `run_pattern.py --once` | 跑一輪：更新行情、偵測形態、記錄進出場 |
| `cloud_fetch.py` | 確保 K 線與資金費率歷史存在（平常由快取提供） |
| `status_md.py` | 產生 STATUS.md、TRADES.md 與 `trades/` 月份頁 |
| `charts.py` | 畫每筆交易的 K 線圖（`charts/` 裡的 SVG） |
| `dashboard.py` | 產生完整圖表頁 dashboard.html |
| `research4.py`、`research7.py` | 形態偵測與出場重播（機器人使用的規則） |
| `logs/pattern_*.json`、`logs/pattern_trades.csv`、`logs/pattern_events.jsonl` | 模擬交易紀錄 |
| `logs/pattern_history_*.csv`、`logs/backtest_negative_combos.csv` | 2018 至今的回測結果（固定，不會自動更新） |
| `.github/workflows/paper-bot.yml` | 排程設定 |

## 停止或暫停

到 Actions 分頁 → `paper-bot` → 右上角選單 → Disable workflow。
