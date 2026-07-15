# tw-chip-data — 資料規格書

這是本 repo 所有資料檔的權威定義。腳本產出、下游判讀都以此為準。
**單位鐵則:一律存「股」,永不存「張」。欄名帶單位是唯一防呆。**(1 張 = 1,000 股;張是呈現層的事)

---

## 檔案地圖

```
config/
  watchlist.yaml          你手動維護的追蹤股清單
  schema.md               本檔
data/
  latest.json             清單檔 — 每次判讀第一個讀這個
  calendar.csv            交易日曆(含 seq 序號,用於營業日計數)
  universe.csv            股本/流通(算「佔流通%」的分母)
  daily/{id}.csv          追蹤股:日K + 三大法人 + 融資券
  branch/{id}.csv         追蹤股:分點日彙總
# 全市場逐價位分點 → 進 GitHub Releases,不進 repo(單日 ~22MB)
```

`daily/`、`branch/`、`latest.json`、`calendar.csv`、`universe.csv` 全部由 fetch 腳本自動生成,**不要手動建**。

---

## latest.json — 最重要的檔

每次判讀的**第一個動作**就是讀它,兩秒內知道每個資料集截到哪天,把快取陷阱變成一次便宜的檢查。

```json
{
  "generated_at_utc": "2026-07-14T11:31:02Z",
  "generated_at_taipei": "2026-07-14 19:31:02",
  "last_trading_date": "2026-07-14",
  "datasets": {
    "price":  {"through": "2026-07-14", "status": "ok"},
    "branch": {"through": "2026-07-14", "status": "ok"},
    "inst":   {"through": "2026-07-14", "status": "ok"},
    "margin": {"through": "2026-07-11", "status": "lagging"}
  },
  "tickers": ["6831", "7795", "2330", "2059"]
}
```

`status`: `ok` 截到最新交易日 / `lagging` 落後 / `missing` 該抓沒抓到。

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

## universe.csv

| 欄位 | 說明 |
|---|---|
| `id` | 代號 |
| `name` | |
| `shares_outstanding` | 發行股數(**股**)|
| `locked_pct` | 鎖倉%(董監/策略,可空)|
| `float_shares` | 外部流通 = 發行 ×(1−鎖倉);< 3,000 萬股(3萬張)= 微流通 |

---

## ETL 驗收 assert(腳本結尾必跑,不過就讓 Actions 紅燈)

```python
assert df.date.is_monotonic_increasing and df.date.is_unique
assert (df.close.between(df.low, df.high)).all()                 # OHLC 內部一致
assert df.date.isin(cal.query("is_trading_day").date).all()      # 無幽靈交易日
```

第三個 assert 是價格序列完整性閘門的機器版:序列有洞當場炸出,而非等到判讀時才發現。
