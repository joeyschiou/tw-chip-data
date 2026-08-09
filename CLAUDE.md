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

## 策略資料層(13 dataset,見 schema.md 表)
- 還原價/借券/質押/停券(per-id universe)+ CB daily/institutional(per-cb)→ **weekly**(每檔 ~2000 call,進 nightly 會爆 6000/hr;fetcher 有 `--wait-quota`)。
- 總經小表(vix/維持率/期貨法人)、慢變全表(處置/下市/產業鏈)、CB info/overview、news(watchlist-8 自增量)→ nightly(便宜)。
- fetcher:fetch_macro / fetch_regulatory / fetch_stockseries(通用逐檔 adj,short,pledge,shortsusp)/ fetch_cb / fetch_news。
- 深度限制(Step 0 實測):VIX 僅 2026-03 起、news 約 2024+;已標 latest.json.strategy_layer.status=shallow。
- 倖存者偏誤:下市股用 TaiwanStockPrice 仍可補歷史(delisting 表當清單);本層未建,已記可行。

## 籌碼分母(重要)
- 「佔流通%」的分母在 data/float/{id}.csv 的 free_float_shares(單位:股;張=股/1000)。
- 鎖倉是 proxy(千張大戶,非董監;FinMind 無董監 dataset)。精確董監鎖倉須另接來源。
- 注意/處置 FinMind 沒有 → latest.json.reference.disposition 記為 unavailable,別自建爬蟲(另案)。
- 單位鐵則見 config/schema.md:一律存「股」,永不存「張」。

## Windows 本機(編碼鐵則,ACP=950)
本機 ANSI codepage 是 950(Big5),UTF-8 檔在預設路徑會被讀成亂碼。四條:
1. **Python**:跑腳本前先 `set PYTHONUTF8=1`;pipe 到別處時再加 `sys.stdout.reconfigure(encoding='utf-8')`,否則 print 中文會被編成 Big5 或 UnicodeEncodeError。
2. **PowerShell 讀檔**:`Get-Content` 預設用 cp950 讀,UTF-8 log 一定亂碼 → **一律加 `-Encoding UTF8`**。寫檔同理:`Out-File`/`Set-Content` 加 `-Encoding utf8`。
3. **PowerShell 輸出**:PS 5.1 的 `[Console]::OutputEncoding` 預設是 cp950,`Write-Output "中文"` 會吐 Big5 位元組給工具層 → 顯示成亂碼。指令開頭先
   `[Console]::OutputEncoding=[Text.Encoding]::UTF8;`,或**乾脆讓 Write-Output 只輸出 ASCII**(中文留在回覆文字裡)。
4. **.cmd / .bat**:cmd.exe 逐位元組解析批次檔,中文 `REM` 註解存成 UTF-8 會被從中間切斷、後半段當命令執行(2026-08-09 幽靈 `ckfill.py` 彈窗即此因)。
   → 批次檔**只用 ASCII 註解**。
