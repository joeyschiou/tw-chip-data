# tw-chip-data — Claude 工作備忘

## 兩層清單(別搞混)
- config/universe.csv：全市場「上市 twse」普通股,機器產生(fetch_universe.py)。只用於日線廣掃。不要手動編輯、不要當追蹤清單。
- config/watchlist.yaml：深追清單。用於日線 + 分點。

## 鐵則
- 使用者新提到 / 要追蹤 / 要回填的任何個股,一律加進 **watchlist**(日線+分點),**永遠不要**加進 universe。
- 加股票前必查證 market(twse/tpex),不要用代號猜(例:6278 是6開頭卻是上市)。用 `python scripts/ensure_watchlist.py --stock <id> --market <twse|tpex>`。
- 資料一律用 pandas 實算,不要肉眼掃 CSV。CSV 一律 utf-8-sig。分點成本計算排除 price=0 列。

## 資料流
- 日線:fetch_daily.py,對 universe ∪ watchlist,append+dedup(--days 控制回填天數,nightly 預設近 30 天)。
- 分點:fetch_branch.py / backfill.py,只對 watchlist。
- 單檔回填:backfill.py --stock <id> --days N(分點+日線)。全市場日線:fetch_daily.py --days N。
- 判讀先讀 data/latest.json。

## Windows 本機
跑腳本前先 set PYTHONUTF8=1(否則 emoji 在 cp950 會 UnicodeEncodeError)。
