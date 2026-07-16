"""
fetch_daytrade.py — 當沖比 → data/daytrade/{id}.csv(追蹤股切片)
資料源 TaiwanStockDayTrading:一天一次「全市場」呼叫(免 data_id,免費、日更),
抓回整日全市場後切 watchlist 存檔。

欄位:
  date              交易日
  day_trade_volume  當沖成交股數(FinMind Volume,單位「股」)
  day_trade_ratio   當沖成交股數 / 當日總成交股數(近似;總量取自 daily/{id}.csv)

當沖比 = 養魚階段溫度計 + 判斷買盤是否為散戶當沖。
冪等:已存在的日期跳過(append-dedup on date);全部都有 → no-op。

用法:
  python scripts/fetch_daytrade.py                # nightly:自癒近 7 個交易日
  python scripts/fetch_daytrade.py --days 30      # 回補近 30 天
  python scripts/fetch_daytrade.py --start 2024-01-01
需要:FINMIND_TOKEN;data/calendar.csv;data/daily/{id}.csv(算比率用)
"""

import os
import argparse
from datetime import date, timedelta
import pandas as pd
import finmind_client as fc

DATASET = "TaiwanStockDayTrading"


def trading_days(start: str, end: str) -> list:
    cal = pd.read_csv("data/calendar.csv")
    d = cal[(cal["date"] >= start) & (cal["date"] <= end)]["date"].astype(str)
    return sorted(d.tolist())


def last_trading_date() -> str:
    cal = pd.read_csv("data/calendar.csv")
    return str(cal["date"].max())


def total_volume_map(stock_id: str) -> dict:
    """從 daily/{id}.csv 取 date → 當日總成交股數,用來算當沖比分母。"""
    path = f"data/daily/{stock_id}.csv"
    if not os.path.exists(path):
        return {}
    d = pd.read_csv(path, dtype=str)
    if "volume_shares" not in d.columns:
        return {}
    out = {}
    for _, r in d.iterrows():
        try:
            out[str(r["date"])] = float(r["volume_shares"])
        except (ValueError, TypeError):
            pass
    return out


def existing_dates(stock_id: str) -> set:
    path = f"data/daytrade/{stock_id}.csv"
    if not os.path.exists(path):
        return set()
    return set(pd.read_csv(path, dtype=str)["date"].astype(str).tolist())


CANARY = "2330"      # 覆蓋 marker(流動性最高、每日必有當沖)


def main() -> None:
    ap = argparse.ArgumentParser(description="當沖比(全市場一 call/日,universe scope)")
    ap.add_argument("--days", type=int, default=7, help="回補近 N 天(預設 7,nightly 自癒)")
    ap.add_argument("--start", default=None, help="起始日 YYYY-MM-DD(優先於 --days)")
    ap.add_argument("--stock", default=None, help="只抓這一檔(省略=整個 universe)")
    ap.add_argument("--force", action="store_true", help="忽略 canary 覆蓋,窗內每日都抓(universe 擴充回補用)")
    args = ap.parse_args()

    token = fc.get_token()
    fc.check_token(token)
    os.makedirs("data/daytrade", exist_ok=True)

    ids = [str(args.stock)] if args.stock else fc.load_universe()
    idset = set(ids)
    end = last_trading_date()
    start = args.start or (date.today() - timedelta(days=args.days)).isoformat()
    days = trading_days(start, end)
    if not days:
        print(f"⚠ {start}~{end} 無交易日,結束")
        return

    # 覆蓋判斷:用 canary(2330,每日必有當沖)已抓過的日子;--force 則窗內全抓(填 universe 新增股)。
    covered = set() if args.force else (existing_dates(CANARY) if not args.stock else existing_dates(str(args.stock)))
    todo = [d for d in days if d not in covered]
    if not todo:
        print(f"⏭ 當沖近 {len(days)} 交易日已抓過,no-op")
        return
    print(f"當沖 universe {len(ids)} 檔;待補 {len(todo)} 個交易日({start}~{end})")

    # 逐日全市場一 call,收集 universe 切片
    collected = {}
    for i, d in enumerate(todo, 1):
        raw = fc.api_data(token, DATASET, start_date=d, end_date=d, throttle=0.3)
        if raw.empty:
            print(f"   ⚠ {d} 無當沖資料,略過")
            continue
        raw["stock_id"] = raw["stock_id"].astype(str)
        for _, r in raw[raw["stock_id"].isin(idset)].iterrows():
            collected.setdefault(str(r["stock_id"]), []).append({"date": d, "day_trade_volume": r["Volume"]})
        if i % 10 == 0 or i == len(todo):
            print(f"   {i}/{len(todo)} … {d}")

    # 逐檔算比率 + write_if_changed(免上千檔 churn)
    changed = 0
    for sid, rows in collected.items():
        df = pd.DataFrame(rows)
        vol_map = total_volume_map(sid)
        df["day_trade_volume"] = pd.to_numeric(df["day_trade_volume"], errors="coerce")

        def ratio(row):
            tv = vol_map.get(row["date"])
            if tv and tv > 0 and pd.notna(row["day_trade_volume"]):
                return round(row["day_trade_volume"] / tv, 6)
            return ""     # 缺總量 → 留空(left-join 誠實,不填 0)

        df["day_trade_ratio"] = df.apply(ratio, axis=1)
        df = df[["date", "day_trade_volume", "day_trade_ratio"]]
        if fc.write_if_changed(f"data/daytrade/{sid}.csv", df, keys=["date"]):
            changed += 1

    used, lim = fc.token_usage(token)
    print(f"✅ 當沖完成,更新 {changed} 檔。用量 {used}/{lim}")


if __name__ == "__main__":
    main()
