# tw-chip-data — Claude 工作備忘

## 兩層(概念主軸,別搞混)
- **廣度層 universe**=全市場普通股(**上市 twse + 上櫃 tpex**,排興櫃/ETF/DR/特別股)。
  日線、集保(holders)、流通(float)、月營收(revenue)、當沖(daytrade)都覆蓋 universe。
  - `finmind_client.load_universe()`=info.csv 普通股 ∩ 有 daily 檔者(≈2,000);過濾規則見 schema.md。
  - `config/universe.csv`=fetch_universe.py 產的清單,欄位 id,name,**market**(twse/tpex);給日線廣掃來源。
- **深度層 watchlist**(config/watchlist.yaml,上限 100)=分點(branch)追蹤,之後由篩選器自動增補。

## 鐵則
- 使用者新提到 / 要追蹤的個股 → 加進 **watchlist**(分點),**永遠不要**手動加進 universe(universe 是機器產生的廣度層)。
- 加股票前必查證 market(twse/tpex),不要用代號猜(例:6278 是6開頭卻是上市)。用 `python scripts/ensure_watchlist.py --stock <id> --market <twse|tpex>`。
- 資料一律用 pandas 實算,不要肉眼掃 CSV。CSV 一律 utf-8-sig。分點成本計算排除 price=0 列。

## 資料流
- 日線:fetch_daily.py,對 universe ∪ watchlist,append+dedup(nightly)。
- 分點:fetch_branch.py / backfill.py,**只對 watchlist**(0d:全市場分點需 storage_objects,未實作)。
- 集保/流通/月營收:fetch_holders / fetch_float / fetch_revenue,**全市場 universe**,全市場單 call(單日/單月)切片。
  - holders/float 週更;revenue 月更(當月>=11日守衛)。都 idempotent(write_if_changed,內容沒變不寫)。
- 當沖:fetch_daytrade.py,全市場一 call/日,存 **universe** 切片(日更)。基本資料:fetch_info.py。
- 主控:
  - **daily-update.yml**(每晚 22:00 台北):update.py 跑核心+補充;週更/月更資料多半 no-op。
  - **weekly-update.yml**(台北週六 07:00 + 週二 07:00 保險):holders→float→revenue。
- 回填:
  - `backfill.py --datasets daily,branch,holders,daytrade,float`(預設 daily,branch)。
  - 集保/流通 universe 回填:`fetch_holders.py --days N` / `fetch_float.py --days N`(全市場)。
  - 月營收回填:`fetch_revenue.py --months 25`。
  - **定向分點回補(農場能力)**:`backfill.py --tickers "6831,7795" --lookback-days 60 --datasets branch`。
- 判讀先讀 data/latest.json。

## 籌碼分母(重要)
- 「佔流通%」的分母在 data/float/{id}.csv 的 free_float_shares(單位:股;張=股/1000)。
- 鎖倉是 proxy(千張大戶,非董監;FinMind 無董監 dataset)。精確董監鎖倉須另接來源。
- 注意/處置 FinMind 沒有 → latest.json.reference.disposition 記為 unavailable,別自建爬蟲(另案)。
- 單位鐵則見 config/schema.md:一律存「股」,永不存「張」。

## Windows 本機
跑腳本前先 set PYTHONUTF8=1(否則 emoji 在 cp950 會 UnicodeEncodeError)。
