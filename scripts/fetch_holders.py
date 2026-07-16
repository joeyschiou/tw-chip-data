"""
fetch_holders.py — 集保股權分散 → data/holders/{id}.csv(廣度層 universe)
資料源 TaiwanStockHoldingSharesPer(週更)。

抓法(0b 實測):
  * 全市場「單一快照日」單 call 可行(一天回 ~4000 檔);但「日期區間」全市場不可靠
    (窗一寬就回 0)。所以路徑是:
      1) 用 canary(2330)逐檔 call 取得「每週快照日清單」;
      2) 每個快照日做一次「全市場單日」call,切 universe 寫 per-id。
    16 週 ≈ 16 個 call。
  * 全市場單日回空(異常)→ 自動退回該日逐檔。
  * --stock <id>:只抓單檔(逐檔路徑),給 backfill 定向用。

續傳:用 RESUME_CANARY(2317,非 watchlist 大型股,只經 universe 路徑填)已有的快照日當
      coverage marker,跳過已抓過的快照日;每日寫入即持久(中斷不白費)。
節流/守衛:api_data 內建;每抓幾日檢查用量,逼近上限就停(下輪續)。
冪等:write_if_changed(內容沒變不寫,避免上千檔 churn);canary 新鮮 → no-op。

用法:
  python scripts/fetch_holders.py                # nightly/weekly:近 21 天(通常 no-op)
  python scripts/fetch_holders.py --days 130      # 回補約 18 週
  python scripts/fetch_holders.py --start 2021-01-01
  python scripts/fetch_holders.py --stock 2330    # 單檔
需要:FINMIND_TOKEN;data/info.csv + data/daily/*.csv(算 universe)
"""

import os
import argparse
from datetime import date, timedelta
import pandas as pd
import finmind_client as fc

DATASET = "TaiwanStockHoldingSharesPer"
FRESH_DAYS = 6            # canary 在 FRESH_DAYS 天內就算新鮮(週更資料落後幾天)
DATE_CANARY = "2330"     # 取快照日清單用(流動性最高、必有集保資料)
RESUME_CANARY = "2317"   # 續傳 coverage marker(鴻海,大型股、非 watchlist,只經 universe 路徑填)
STOP_RATIO = 0.9         # 用量逼近上限的比例,超過就停下(續傳)
COLS = ["date", "level", "people", "holding_shares", "percent"]


def canary_newest(sid: str) -> str:
    p = f"data/holders/{sid}.csv"
    if not os.path.exists(p):
        return ""
    d = pd.read_csv(p, dtype=str)
    return str(d["date"].max()) if len(d) else ""


def covered_dates() -> set:
    p = f"data/holders/{RESUME_CANARY}.csv"
    if not os.path.exists(p):
        return set()
    return set(pd.read_csv(p, dtype=str)["date"].astype(str))


def snapshot_dates(token: str, start: str) -> list:
    r = fc.api_data(token, DATASET, data_id=DATE_CANARY, start_date=start)
    return sorted(r["date"].astype(str).unique()) if not r.empty else []


def write_slices(raw: pd.DataFrame, keep_ids: set) -> int:
    if raw.empty:
        return 0
    raw = raw.copy()
    raw["stock_id"] = raw["stock_id"].astype(str)
    sliced = raw[raw["stock_id"].isin(keep_ids)].rename(
        columns={"HoldingSharesLevel": "level", "unit": "holding_shares"})
    changed = 0
    for sid, g in sliced.groupby("stock_id"):
        if fc.write_if_changed(f"data/holders/{sid}.csv", g[COLS], keys=["date", "level"]):
            changed += 1
    return changed


def main() -> None:
    ap = argparse.ArgumentParser(description="集保股權分散(全市場 universe,週更)")
    ap.add_argument("--days", type=int, default=21, help="回補近 N 天(預設 21)")
    ap.add_argument("--start", default=None, help="起始日 YYYY-MM-DD(優先於 --days)")
    ap.add_argument("--stock", default=None, help="只抓這一檔(逐檔路徑)")
    ap.add_argument("--force", action="store_true", help="忽略 canary 新鮮度與 coverage,一律抓")
    args = ap.parse_args()

    token = fc.get_token()
    fc.check_token(token)
    os.makedirs("data/holders", exist_ok=True)
    start = args.start or (date.today() - timedelta(days=args.days)).isoformat()

    # 單檔路徑
    if args.stock:
        raw = fc.api_data(token, DATASET, data_id=str(args.stock), start_date=start)
        print(f"✅ 集保單檔 {args.stock}:更新 {write_slices(raw, {str(args.stock)})} 檔")
        return

    # nightly 新鮮度守衛
    if not args.start and not args.force:
        cutoff = (date.today() - timedelta(days=FRESH_DAYS)).isoformat()
        if canary_newest(DATE_CANARY) and canary_newest(DATE_CANARY) >= cutoff:
            print(f"⏭ 集保 canary({DATE_CANARY})最新 {canary_newest(DATE_CANARY)},新鮮,no-op")
            return

    uni = set(fc.load_universe())
    dates = snapshot_dates(token, start)
    done = set() if args.force else covered_dates()
    todo = [d for d in dates if d not in done]
    print(f"集保 universe {len(uni)} 檔;快照日 {len(dates)} 個,待補 {len(todo)} 個({start}起)")
    if not todo:
        print("⏭ 全部快照日已覆蓋,no-op")
        return

    changed_total = 0
    for i, d in enumerate(todo, 1):
        raw = fc.api_data(token, DATASET, start_date=d, end_date=d)   # 全市場單日
        if raw.empty:
            print(f"   ⚠ {d} 全市場回空,退回逐檔")
            for sid in sorted(uni):
                raw2 = fc.api_data(token, DATASET, data_id=sid, start_date=d, end_date=d)
                changed_total += write_slices(raw2, {sid})
        else:
            changed_total += write_slices(raw, uni)
        used, lim = fc.token_usage(token)
        print(f"   [{i}/{len(todo)}] {d}:累計更新 {changed_total} 檔;用量 {used}/{lim}")
        if used and lim and used > lim * STOP_RATIO:
            print(f"   ⏸ 用量逼近上限({used}/{lim}),停下續傳(下輪再跑補完)")
            break

    print(f"✅ 集保完成,更新 {changed_total} 檔")


if __name__ == "__main__":
    main()
