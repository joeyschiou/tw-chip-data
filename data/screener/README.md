# data/screener/ — 篩選機產出

由 `scripts/screener.py` 每晚產生。

## 檔案
- `latest.md` / `report_YYYY-MM-DD.md` — 訊號報告(zh-TW)。
- `market_index.csv` — 等權市場指數快取(date, mkt_logret, index, ma120, regime)。
- `state.json` — **模型組合狀態(純合法 JSON,可手動編輯)**。
- `trades.csv` — 已平倉交易(含毛/淨報酬,淨=毛−0.4425%)。

## state.json 格式(說明放這裡,不放進 JSON 檔內以維持純合法 JSON)
```
{
  "last_run_date": "YYYY-MM-DD",           // 最後跑的資料日
  "positions": [                            // 目前持倉
    {"id","name","entry_date","entry_price","exit_due","rank_score","signal_date"}
  ],
  "pending":   [                            // 明日開盤待買(依排序分)
    {"id","name","rank_score","signal_date"}
  ],
  "farm_queue": ["id", ...]                 // 農場待回補 60 日分點的代號佇列
}
```
空狀態即上述四鍵、值為 null/空陣列(仍是合法 JSON)。手動編輯後請確保 `python -c "import json;json.load(open('data/screener/state.json',encoding='utf-8-sig'))"` 通過。
