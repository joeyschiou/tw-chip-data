"""
fetch_holders.py — 集保股權分散 → data/holders/{id}.csv(追蹤股)
資料源 TaiwanStockHoldingSharesPer(週更):各持股級距的人數與股數。
餵 fetch_float.py 的鎖倉 proxy(大戶級距),本身也是大戶增減訊號。

欄位:
  date            集保結算日(週更)
  level           持股級距(FinMind 原字串,例 '400,001-600,000'、'more than 1,000,001'、'total')
  people          該級距人數
  holding_shares  該級距持股「股數」(FinMind unit;total 級距 == 發行股數)
  percent         該級距佔比 %

冪等:append-dedup on (date, level);已有近一週資料 → 該檔 no-op。

用法:
  python scripts/fetch_holders.py               # nightly:近 30 天(週更,通常 no-op 或補一筆)
  python scripts/fetch_holders.py --days 400     # 回補
  python scripts/fetch_holders.py --start 2021-01-01
需要:FINMIND_TOKEN;data/calendar.csv
"""

import os
import argparse
from datetime import date, timedelta
import pandas as pd
import finmind_client as fc

DATASET = "TaiwanStockHoldingSharesPer"
FRESH_DAYS = 7      # 已有 <= FRESH_DAYS 天前的資料就算新鮮(週更資料約落後一週)


def last_trading_date() -> str:
    cal = pd.read_csv("data/calendar.csv")
    return str(cal["date"].max())


def newest_date(stock_id: str) -> str:
    path = f"data/holders/{stock_id}.csv"
    if not os.path.exists(path):
        return ""
    d = pd.read_csv(path, dtype=str)
    return str(d["date"].max()) if len(d) else ""


def main() -> None:
    ap = argparse.ArgumentParser(description="集保股權分散(週更)")
    ap.add_argument("--days", type=int, default=30, help="回補近 N 天(預設 30)")
    ap.add_argument("--start", default=None, help="起始日 YYYY-MM-DD(優先於 --days)")
    ap.add_argument("--force", action="store_true", help="忽略新鮮度,一律重抓")
    ap.add_argument("--stock", default=None, help="只抓這一檔(省略=整個 watchlist)")
    args = ap.parse_args()

    token = fc.get_token()
    fc.check_token(token)
    os.makedirs("data/holders", exist_ok=True)

    ids = [str(args.stock)] if args.stock else fc.watchlist_ids()
    start = args.start or (date.today() - timedelta(days=args.days)).isoformat()
    ltd = last_trading_date()
    fresh_cutoff = (date.fromisoformat(ltd) - timedelta(days=FRESH_DAYS)).isoformat()

    updated = 0
    for sid in ids:
        if not args.force and newest_date(sid) and newest_date(sid) >= fresh_cutoff:
            print(f"⏭ {sid} 集保已新鮮(最新 {newest_date(sid)}),no-op")
            continue
        raw = fc.api_data(token, DATASET, data_id=sid, start_date=start)
        if raw.empty:
            print(f"   ⚠ {sid} 集保無資料")
            continue
        df = raw.rename(columns={"HoldingSharesLevel": "level", "unit": "holding_shares"})
        df = df[["date", "level", "people", "holding_shares", "percent"]]
        path = f"data/holders/{sid}.csv"
        merged = fc.append_dedup(path, df, keys=["date", "level"])
        updated += 1
        print(f"   ✅ {path}:{len(merged)} 筆 / {merged['date'].nunique()} 週,"
              f"{merged['date'].min()}→{merged['date'].max()}")

    print(f"✅ 集保完成,更新 {updated} 檔")


if __name__ == "__main__":
    main()
