# tw-chip-data — Claude 工作備忘

## 兩層清單(別搞混)
- config/universe.csv：全市場「上市 twse」普通股,機器產生(fetch_universe.py)。只用於日線廣掃。不要手動編輯、不要當追蹤清單。
- config/watchlist.yaml：深追清單。用於日線 + 分點 + 當沖 + 集保 + 流通張數。

## 鐵則
- 使用者新提到 / 要追蹤 / 要回填的任何個股,一律加進 **watchlist**(日線+分點),**永遠不要**加進 universe。
- 加股票前必查證 market(twse/tpex),不要用代號猜(例:6278 是6開頭卻是上市)。用 `python scripts/ensure_watchlist.py --stock <id> --market <twse|tpex>`。
- 資料一律用 pandas 實算,不要肉眼掃 CSV。CSV 一律 utf-8-sig。分點成本計算排除 price=0 列。

## 資料流
- 日線:fetch_daily.py,對 universe ∪ watchlist,append+dedup(--days 控制回填天數,nightly 預設近 30 天)。
- 分點:fetch_branch.py / backfill.py,只對 watchlist。
- 當沖:fetch_daytrade.py,全市場一 call/日,存 watchlist 切片(日更)。
- 集保:fetch_holders.py,週更;流通張數分母:fetch_float.py(發行/鎖倉 proxy/外部流通,週更慢變維度)。
- 基本資料:fetch_info.py(全市場,偶爾刷新)。
- 主控:update.py 先跑核心(calendar/universe/daily/branch,失敗紅燈),再跑補充(info/daytrade/holders/float,失敗只警告);cadence 由各腳本內部 no-op。
- 單檔回填:backfill.py --stock <id> --days N --datasets daily,branch,holders,daytrade,float(預設只 daily,branch)。
- 判讀先讀 data/latest.json。

## 籌碼分母(重要)
- 「佔流通%」的分母在 data/float/{id}.csv 的 free_float_shares(單位:股;張=股/1000)。
- 鎖倉是 proxy(千張大戶,非董監;FinMind 無董監 dataset)。精確董監鎖倉須另接來源。
- 注意/處置 FinMind 沒有 → latest.json.reference.disposition 記為 unavailable,別自建爬蟲(另案)。
- 單位鐵則見 config/schema.md:一律存「股」,永不存「張」。

## Windows 本機
跑腳本前先 set PYTHONUTF8=1(否則 emoji 在 cp950 會 UnicodeEncodeError)。
