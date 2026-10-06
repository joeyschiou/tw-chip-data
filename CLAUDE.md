# tw-chip-data — Claude 工作備忘

## 兩層(概念主軸,別搞混)
- **廣度層 universe**=全市場普通股(**上市 twse + 上櫃 tpex**,排興櫃/ETF/DR/特別股)。
  日線覆蓋 universe(集保/流通/月營收/當沖原本也是,免費帳號後已停抓)。
  - `finmind_client.load_universe()`=info.csv 普通股 ∩ 有 daily 檔者(≈2,000);過濾規則見 schema.md。
  - `config/universe.csv`=fetch_universe.py 產的清單,欄位 id,name,**market**(twse/tpex);給日線廣掃來源。
- **重點清單 priority**(config/priority.yaml)=使用者手動維護、最在意的股票:每晚**第一個**抓日線;
  新加入且沒有 daily 檔的自動從 2015 起補完整歷史。
- **深度層 watchlist**(config/watchlist.yaml,上限 100)=優先更新日線的追蹤清單,由篩選器自動增補。
  (原本是分點 branch 追蹤;免費帳號抓不到分點,已停。)

## 鐵則
- 使用者說「重視/優先」的個股 → 加進 **config/priority.yaml**;一般新提到 / 要追蹤的 → 加進 **watchlist**。
  **永遠不要**手動加進 universe(universe 是機器產生的廣度層)。
- 加股票前必查證 market(twse/tpex),不要用代號猜(例:6278 是6開頭卻是上市)。用 `python scripts/ensure_watchlist.py --stock <id> --market <twse|tpex>`。
- 資料一律用 pandas 實算,不要肉眼掃 CSV。CSV 一律 utf-8-sig。分點成本計算排除 price=0 列。

## FinMind 帳號等級(2026-10 起:免費 register,600 call/hr)
- 免費帳號打付費 dataset 回 **HTTP 400 "Your level is register"**;連打幾百個 400 會被 **封 IP(403 ip banned)**,
  後面所有 call 一起死。`finmind_client.api_data` 遇到即 sys.exit,不要寫逐檔狂打的迴圈。
- 「不帶 data_id 的全市場單 call」(當沖/集保/月營收/處置/CB info…)在免費帳號都不能用。
- **已停抓**(實測付費,或 per-id 全 universe 遠超免費額度):分點 branch、當沖、集保、流通(依賴集保)、月營收、
  還原價/借券/質押/停券、CB、news、景氣對策信號、大盤維持率、VIX、處置、產業鏈。data/ 下舊檔保留為歷史(約截至 2026-08-14),
  screener 仍讀得到但**不會再更新**。相關腳本(fetch_branch/daytrade/holders/float/revenue/stockseries/cb/news、
  backtest_branch、runner_backfill)與 weekly-update.yml 已刪除;升回付費時從 git 歷史(commit ee8ce89b 之前)找回。

## 資料流
- 日線:fetch_daily.py,對 universe ∪ watchlist,append+dedup(nightly)。
  - 免費額度一小時只夠 ~180 檔(一檔 3 call):**priority → watchlist 優先**,其餘 universe 依最後日期由舊到新輪轉,
    主排程保留 60 call 給補充腳本;落後超過 --days 的檔自動從最後日期補缺口。開跑前額度已滿就直接收工。
- 基本資料:fetch_info.py;總經:fetch_macro.py(期貨法人);名冊:fetch_regulatory.py(下市);
  借券法規快照:fetch_sbl_snapshot.py(TWSE,非 FinMind)。
- 主控:**daily-update.yml**(每晚 22:00 台北):update.py 跑核心+補充 → screener。
- 接力:**daily-extend.yml**(台北 23:05~隔天 10:05 每小時叫醒):`fetch_daily.py --skip-current --reserve 30`
  用當小時額度續抓 universe(已到最新的 0 call 跳過);單次 run 內自己等下一小時再抓,最多 6 輪
  (GitHub 排程常跳過,不能靠它每小時準時)。不跑 screener。
  - 兩條 workflow 都用 `scripts/commit_push.sh` commit:push 被拒就 rebase 重推(會互相搶推)。
- 回填:`backfill.py --stock <id> --days N`(只剩 daily)。
- 判讀先讀 data/latest.json。

## 策略資料層(見 schema.md 表)
- 仍更新:期貨法人、下市 → nightly(VIX、產業鏈實測付費,已停)。
- 倖存者偏誤:下市股用 TaiwanStockPrice 仍可補歷史(delisting 表當清單);本層未建,已記可行。

## 籌碼分母(重要)
- 「佔流通%」的分母在 data/float/{id}.csv 的 free_float_shares(單位:股;張=股/1000)。
- 鎖倉是 proxy(千張大戶,非董監;FinMind 無董監 dataset)。精確董監鎖倉須另接來源。
- 注意/處置 FinMind 沒有 → latest.json.reference.disposition 記為 unavailable,別自建爬蟲(另案)。
- 單位鐵則見 config/schema.md:一律存「股」,永不存「張」。

## Windows 本機(編碼鐵則,ACP=950)
本機 ANSI codepage 是 950(Big5),UTF-8 檔在預設路徑會被讀成亂碼。

> **`$PROFILE` 救不了你**:`Documents\WindowsPowerShell\Microsoft.PowerShell_profile.ps1` 已設 UTF-8 強制(2026-08-09 建),
> 但 **Claude Code 的 PowerShell 工具是 `-NoProfile`**,完全不吃。實測:同一個檔,一般 shell 讀出「台北」,`-NoProfile` 讀出「?啣?」。
> profile 只保護你手動開的 PowerShell 與排程。**在這裡,下面四條要每次手動遵守。**

四條:
1. **Python**:跑腳本前先 `set PYTHONUTF8=1`;pipe 到別處時再加 `sys.stdout.reconfigure(encoding='utf-8')`,否則 print 中文會被編成 Big5 或 UnicodeEncodeError。
2. **PowerShell 讀檔**:`Get-Content` 預設用 cp950 讀,UTF-8 log 一定亂碼 → **一律加 `-Encoding UTF8`**。寫檔同理:`Out-File`/`Set-Content` 加 `-Encoding utf8`。
3. **PowerShell 輸出**:PS 5.1 的 `[Console]::OutputEncoding` 預設是 cp950,`Write-Output "中文"` 會吐 Big5 位元組給工具層 → 顯示成亂碼。指令開頭先
   `[Console]::OutputEncoding=[Text.Encoding]::UTF8;`,或**乾脆讓 Write-Output 只輸出 ASCII**(中文留在回覆文字裡)。
4. **.cmd / .bat**:cmd.exe 逐位元組解析批次檔,中文 `REM` 註解存成 UTF-8 會被從中間切斷、後半段當命令執行(2026-08-09 幽靈 `ckfill.py` 彈窗即此因)。
   → 批次檔**只用 ASCII 註解**。
