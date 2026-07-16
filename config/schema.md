# tw-chip-data — 資料規格書

這是本 repo 所有資料檔的權威定義。腳本產出、下游判讀都以此為準。
**單位鐵則:一律存「股」,永不存「張」。欄名帶單位是唯一防呆。**(1 張 = 1,000 股;張是呈現層的事)

---

## 檔案地圖

```
config/
  watchlist.yaml          你手動維護的追蹤股清單(深追:日線 + 分點 + 集保/流通/當沖)
  universe.csv            全市場上市普通股清單(機器產生,只給日線廣掃;非追蹤清單)
  broker_tags.csv         券商分點分類參考表(手動維護 scaffold)
  schema.md               本檔
data/
  latest.json             清單檔 — 每次判讀第一個讀這個
  calendar.csv            交易日曆(含 seq 序號,用於營業日計數)
  info.csv                全市場個股基本資料(名稱/產業/上市櫃)
  daily/{id}.csv          追蹤股:日K + 三大法人 + 融資券
  branch/{id}.csv         追蹤股:分點日彙總
  daytrade/{id}.csv       廣度層 universe:當沖量 + 當沖比(日更)
  holders/{id}.csv        廣度層 universe:集保股權分散級距(週更)
  float/{id}.csv          廣度層 universe:流通張數分母 — 發行/鎖倉 proxy/外部流通(週更)
  revenue/{id}.csv        廣度層 universe:月營收 + yoy/mom/累計 yoy(月更)
# 全市場逐價位分點 → 進 GitHub Releases,不進 repo(單日 ~22MB)
```

`data/` 下所有檔與 `config/universe.csv` 全部由 fetch 腳本自動生成,**不要手動建**。
`config/broker_tags.csv` 是手動維護的 scaffold(唯一例外)。

---

## latest.json — 最重要的檔

每次判讀的**第一個動作**就是讀它,兩秒內知道每個資料集截到哪天,把快取陷阱變成一次便宜的檢查。

```json
{
  "generated_at_utc": "2026-07-16T01:41:58Z",
  "generated_at_taipei": "2026-07-16 09:41:58",
  "last_trading_date": "2026-07-15",
  "datasets": {
    "price":    {"through": "2026-07-15", "status": "ok"},
    "inst":     {"through": "2026-07-15", "status": "ok"},
    "margin":   {"through": "2026-07-15", "status": "ok"},
    "daytrade": {"through": "2026-07-15", "status": "ok", "cadence": "daily"},
    "holders":  {"through": "2026-07-09", "status": "ok", "cadence": "weekly"},
    "float":    {"through": "2026-07-09", "status": "ok", "cadence": "weekly",
                 "note": "locked=千張大戶 proxy(非董監)"}
  },
  "reference": {
    "info":        {"count": 2743, "status": "ok"},
    "disposition": {"status": "unavailable", "note": "FinMind 無 注意/處置 dataset"}
  },
  "universe": {"count": 1208, "daily_files": 1133, "current": 1080},
  "tickers": ["6831", "7795", "2330", "2059", "6278", "2327", "6510", "7769"]
}
```

`status`: `ok` 截到最新交易日 / `lagging` 落後 / `missing` 該抓沒抓到。
- 核心(price/inst/margin)用 **watchlist canary**(min:最落後那檔決定整體)。
- 補充(daytrade/holders/float)用 watchlist **max**(有抓到多新),週更資料容許 `cadence` 落後(tol 10 天)。
- `reference.disposition` 記錄 **已知缺口**:FinMind 無 注意/處置 dataset(已實測),下游此層需另接 TWSE/櫃買來源。

---

## daily/{id}.csv

| 欄位 | 型別 | 說明 |
|---|---|---|
| `date` | ISO `YYYY-MM-DD` | **絕不用民國年**;ETL 就把 `1150709` 轉掉 |
| `open` `high` `low` `close` | float | |
| `volume_shares` | int | **股** |
| `value_twd` | int | 成交金額 |
| `transactions` | int | 筆數 |
| `limit_up` `limit_down` | bool | ETL 算好,不讓下游猜 |
| `foreign_net_shares` | int | 外資淨,**股** |
| `trust_net_shares` | int | 投信淨 |
| `dealer_net_shares` | int | 自營淨 |
| `margin_balance_shares` | int | 融資餘額 |
| `short_balance_shares` | int | 融券餘額 |
| `daytrade_ratio` | float | 當沖比(拿不到留空) |
| `source` | str | `finmind` / `twse` / `tpex` |
| `fetched_at` | ISO datetime | |

## branch/{id}.csv

| 欄位 | 說明 |
|---|---|
| `date` | |
| `broker_id` | **主鍵**;券商改名代碼不變 |
| `broker_name` | 顯示用 |
| `buy_shares` `sell_shares` | **總額,不是淨額**(淨額不可逆,對敲比需要買賣兩腿) |
| `net_shares` | 衍生欄,先算好 |
| `avg_buy_price` `avg_sell_price` | 逐價位加權 → 成本帶來源 |

## calendar.csv

| 欄位 | 說明 |
|---|---|
| `date` | ISO |
| `seq` | 交易日序號 → 「處置 10 營業日後」= seq+10,免心算 |
| `is_trading_day` | bool |

## config/universe.csv

全市場「上市 twse + 上櫃 tpex」普通股清單,`fetch_universe.py` 機器產生。**廣度層來源,不是追蹤清單。**
過濾(0b 實測 type:twse=上市 / tpex=上櫃 / emerging=興櫃):保留 twse+tpex,**排興櫃**;
`is_common`(4 位非 0 開頭,排 00xx ETF/特別股/權證)、排 91xx TDR、排 industry 含 ETF。

| 欄位 | 說明 |
|---|---|
| `id` | 代號 |
| `name` | 股名 |
| `market` | `twse`(上市)/ `tpex`(上櫃)—— 下游判交易所、必要時帶 market 參數 |

> 註:分母(流通張數)已獨立成 `data/float/{id}.csv`(見下),不再塞進 universe.csv。

## float/{id}.csv — 流通張數分母(整套籌碼判讀的分母)

慢變維度,週更(跟集保同拍)。issued/foreign 用 as-of(<= 該集保日最近一筆 shareholding)。
下游 join 時取「as-of 最近一筆 float」。**單位一律「股」**(張 = 股 / 1000)。

| 欄位 | 說明 |
|---|---|
| `date` | 集保快照日 |
| `issued_shares` | 發行股數(FinMind `TaiwanStockShareholding.NumberOfSharesIssued`)|
| `foreign_holding_shares` | 外資持股(`ForeignInvestmentShares`)|
| `big_holder_over400_shares` | 集保 >400,000 股(>400 張)四級距總和 |
| `big_holder_over1000_shares` | 集保 `more than 1,000,001`(>1000 張大戶)|
| `locked_shares` | **鎖倉 proxy = `big_holder_over1000_shares`**(見下誠實註記)|
| `free_float_shares` | 外部流通 = `issued_shares − locked_shares` |
| `locked_pct` | `locked_shares / issued_shares` |
| `source` `fetched_at` | |

**流通張數算式**:`free_float_lots = free_float_shares / 1000`;
`佔流通% = 分點累積淨買股數 / free_float_shares`(股對股,或張對張,同單位即可)。

**鎖倉 proxy 的誠實註記(重要)**:FinMind **沒有**董監持股 dataset
(已實測 `TaiwanStockBoardMemberShareholding`/`ManagerInsiders`/`InsiderHolding`/
`DirectorSupervisorShareholding` 等 6 個候選名稱皆 HTTP 422 不存在)。
因此 `locked_shares` 用「集保千張大戶持股」當 proxy,**不是真正的董監鎖倉**:
- 千張大戶會進出,不是全鎖;對 2330 這種官股/ADR 大量持有者會高估鎖倉(→ 低估 free float)。
- 需要精確董監鎖倉時,`free_float` 只能當近似上界看待,別當精確分母。
- 想放寬/收緊 proxy:改用 `big_holder_over400_shares` 當 locked 即可(欄位已備)。

## holders/{id}.csv — 集保股權分散(週更)

| 欄位 | 說明 |
|---|---|
| `date` | 集保結算日(週更)|
| `level` | 持股級距(FinMind 原字串,例 `400,001-600,000`、`more than 1,000,001`、`total`)|
| `people` | 該級距人數 |
| `holding_shares` | 該級距持股「股數」(`total` 級距 == 發行股數)|
| `percent` | 該級距佔比 % |

## daytrade/{id}.csv — 當沖(日更)

| 欄位 | 說明 |
|---|---|
| `date` | 交易日 |
| `day_trade_volume` | 當沖成交股數(FinMind `TaiwanStockDayTrading.Volume`,**股**)|
| `day_trade_ratio` | `day_trade_volume / 當日總成交股數`(近似;總量取自 daily/{id}.csv;缺總量留空)|

> 冷門股本就沒有每日當沖資料,缺列屬正常(left-join 誠實,不補 0)。

## revenue/{id}.csv — 月營收(Block E,月更)

| 欄位 | 說明 |
|---|---|
| `revenue_month` | 營收月 `YYYY-MM`(FinMind revenue_year+revenue_month;公布日=次月)|
| `revenue` | 當月營收(元)|
| `yoy` | 對去年同月成長率(分數;×100 = %;基期需 12 個月前)|
| `mom` | 對上月成長率(分數)|
| `cumulative_yoy` | 今年 YTD 對去年 YTD 成長率(兩年同月皆齊全且去年 YTD>0 才算)|
| `source` `fetched_at` | |

> 資料源 `TaiwanStockMonthRevenue`(全市場單月 call)。yoy/mom/累計皆由原始 revenue 自算。
> cadence 月更:當月 ≥ 11 日且有新月份才抓(上月營收約每月 10 日公布)。

## 廣度層 universe 定義(load_universe)

`holders / float / revenue / daytrade` 的對象是**廣度層 universe**,由 `finmind_client.load_universe()` 決定:
**全市場「普通股(twse+tpex)」∩「已有 data/daily/*.csv 的代號」**(≈2,000)。普通股過濾(排 ETF/DR/受益憑證/特別股/興櫃):
- `stock_id` 為 4 位、非 0 開頭(排 00xx ETF、字母尾綴特別股如 2881A、6 位權證)
- 排 91xx TDR
- 排 `info.csv` industry 含 ETF

> 與 `config/universe.csv`(fetch_universe.py 產的「上市 twse 清單」,只給日線廣掃)是**不同**概念:
> 前者是「我們有日線、要算籌碼分母」的股集(≈1,100),後者是日線廣掃的來源清單。
> 分點(branch)維持 **watchlist-only**(深度層,見 0d)。

## info.csv — 全市場個股基本資料

| 欄位 | 說明 |
|---|---|
| `stock_id` | 代號 |
| `name` | 股名 |
| `industry` | 產業別(FinMind `industry_category`)|
| `type` | `twse`=上市 / `tpex`=上櫃 |

## config/broker_tags.csv — 券商分點分類參考表(手動維護 scaffold)

`broker_key` 對得上 `branch/{id}.csv` 的 `broker_id`。欄位:`broker_key, broker_name, tag, note`;
`tag` 建議值 `隔日沖 / 外資 / 大戶具名 / 其他`。**含 `#` 註解列,讀取用 `pandas.read_csv(comment='#')`。**
隔日沖名單需人工維護,勿臆造。

## 已知缺口 — 注意/處置

FinMind **無** 注意股/處置股 dataset(已實測)。本管線保持 **FinMind-only**,
`latest.json.reference.disposition.status = "unavailable"` 記錄此缺口;
下游若需此層,須另接 TWSE / 櫃買公告來源(另案,非本管線)。

## 分點 branch 範圍(0d 實測結論)

分點維持 **watchlist-only(深度層)**,不是全市場。實測:
- `taiwan_stock_trading_daily_report` endpoint **強制要 data_id**(省略回 HTTP 400),
  無法用「單 call 抓全市場分點」。
- 全市場分點需 FinMind `storage_objects` 批次下載(S3,另一機制),**現有程式碼完全沒實作**;
  `watchlist.yaml` 的 `fetch_full_market_branch: true` 旗標目前是 **no-op**(fetch_branch.py 不讀它)。
- 定向回補用 `backfill.py --tickers "a,b" --lookback-days N`(篩選器讓股進農場後呼叫;自動觸發另案)。

---

## ETL 驗收 assert(腳本結尾必跑,不過就讓 Actions 紅燈)

```python
assert df.date.is_monotonic_increasing and df.date.is_unique
assert (df.close.between(df.low, df.high)).all()                 # OHLC 內部一致
assert df.date.isin(cal.query("is_trading_day").date).all()      # 無幽靈交易日
```

第三個 assert 是價格序列完整性閘門的機器版:序列有洞當場炸出,而非等到判讀時才發現。
