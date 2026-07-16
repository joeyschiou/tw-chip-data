"""
fetch_float.py — 流通張數 free float(整套籌碼分析的分母)→ data/float/{id}.csv

* 單位鐵則(見 config/schema.md):一律存「股」,永不存「張」。張 = 股 / 1000,是呈現層的事。*

發行股數 / 外資持股來源:TaiwanStockShareholding
  - issued_shares          = NumberOfSharesIssued(發行股數,股)
  - foreign_holding_shares = ForeignInvestmentShares(外資持股,股)

鎖倉(locked)—— 誠實註記:FinMind 沒有「董監持股」dataset(已實測 6 個候選名稱皆 404/422),
所以 locked 用「集保大戶級距」當 proxy,不是真正的董監鎖倉:
  - big_holder_over1000_shares = 集保 'more than 1,000,001' 級距(>1000 張大戶)
  - big_holder_over400_shares  = 集保 >400,000 股(>400 張)四個級距總和
  - locked_shares  = big_holder_over1000_shares(預設:千張大戶當強手/鎖倉 proxy)
  - free_float_shares = issued_shares − locked_shares
  - locked_pct        = locked_shares / issued_shares

慢變維度:集保週更、股本罕變。float 以「集保快照日」為時間軸(週),
issued/foreign 用 as-of(<= 該日最近一筆 shareholding)。update.py join 時取 as-of 最近一筆 float。
冪等:只補集保有、float 還沒有的日期;無新日 → no-op。

用法:
  python scripts/fetch_float.py                 # nightly:補新集保日
  python scripts/fetch_float.py --start 2021-01-01   # 回補(需先有 holders 歷史)
需要:FINMIND_TOKEN;data/holders/{id}.csv(先跑 fetch_holders.py)
"""

import os
import argparse
import pandas as pd
import finmind_client as fc

DATASET = "TaiwanStockShareholding"
OVER400_LEVELS = ["400,001-600,000", "600,001-800,000",
                  "800,001-1,000,000", "more than 1,000,001"]
OVER1000_LEVEL = "more than 1,000,001"


def holders_big(stock_id: str) -> pd.DataFrame:
    """從 holders/{id}.csv 算每個集保日的大戶持股(股)。回 date, over400, over1000。"""
    path = f"data/holders/{stock_id}.csv"
    if not os.path.exists(path):
        return pd.DataFrame()
    h = pd.read_csv(path, dtype=str)
    h["holding_shares"] = pd.to_numeric(h["holding_shares"], errors="coerce").fillna(0)
    rows = []
    for d, g in h.groupby("date"):
        over400 = g[g["level"].isin(OVER400_LEVELS)]["holding_shares"].sum()
        over1000 = g[g["level"] == OVER1000_LEVEL]["holding_shares"].sum()
        rows.append({"date": d, "over400": int(over400), "over1000": int(over1000)})
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def existing_dates(stock_id: str) -> set:
    path = f"data/float/{stock_id}.csv"
    if not os.path.exists(path):
        return set()
    return set(pd.read_csv(path, dtype=str)["date"].astype(str).tolist())


def build_for(token: str, stock_id: str, start: str) -> int:
    big = holders_big(stock_id)
    if big.empty:
        print(f"   ⚠ {stock_id} 無 holders,先跑 fetch_holders.py")
        return 0
    have = existing_dates(stock_id)
    todo = big[~big["date"].isin(have)]
    if start:
        todo = todo[todo["date"] >= start]
    if todo.empty:
        print(f"   ⏭ {stock_id} float 無新集保日,no-op")
        return 0

    # 抓涵蓋 todo 期間的 shareholding(往前留 buffer 保證 as-of 對得到)
    sh_start = (pd.to_datetime(todo["date"].min()) - pd.Timedelta(days=20)).date().isoformat()
    sh = fc.api_data(token, DATASET, data_id=stock_id, start_date=sh_start)
    if not sh.empty:
        sh = sh.rename(columns={"NumberOfSharesIssued": "issued_shares",
                                "ForeignInvestmentShares": "foreign_holding_shares"})
        sh = sh[["date", "issued_shares", "foreign_holding_shares"]].copy()
        sh["date"] = pd.to_datetime(sh["date"])
        sh = sh.sort_values("date")
    else:
        sh = pd.DataFrame(columns=["date", "issued_shares", "foreign_holding_shares"])

    t = todo.copy()
    t["date_dt"] = pd.to_datetime(t["date"])
    t = t.sort_values("date_dt")
    if len(sh):
        merged = pd.merge_asof(t, sh.rename(columns={"date": "date_dt"}),
                               on="date_dt", direction="backward")
    else:
        merged = t.assign(issued_shares=pd.NA, foreign_holding_shares=pd.NA)

    def compute(r):
        issued = pd.to_numeric(r.get("issued_shares"), errors="coerce")
        over1000 = r["over1000"]
        locked = over1000
        if pd.notna(issued) and issued > 0:
            free = int(issued) - int(locked)
            pct = round(locked / int(issued), 6)
            issued_out, free_out = int(issued), free
        else:
            issued_out, free_out, pct = "", "", ""
        return pd.Series({
            "date": r["date"],
            "issued_shares": issued_out,
            "foreign_holding_shares": (int(pd.to_numeric(r.get("foreign_holding_shares"),
                                       errors="coerce")) if pd.notna(
                                       pd.to_numeric(r.get("foreign_holding_shares"),
                                       errors="coerce")) else ""),
            "big_holder_over400_shares": int(r["over400"]),
            "big_holder_over1000_shares": int(over1000),
            "locked_shares": int(locked),
            "free_float_shares": free_out,
            "locked_pct": pct,
        })

    out = merged.apply(compute, axis=1)
    out["source"] = "finmind"
    out["fetched_at"] = pd.Timestamp.now(tz="Asia/Taipei").isoformat()
    cols = ["date", "issued_shares", "foreign_holding_shares",
            "big_holder_over400_shares", "big_holder_over1000_shares",
            "locked_shares", "free_float_shares", "locked_pct", "source", "fetched_at"]
    out = out[cols]

    path = f"data/float/{stock_id}.csv"
    merged_all = fc.append_dedup(path, out, keys=["date"])
    print(f"   ✅ {path}:+{len(out)} 筆(共 {len(merged_all)}),"
          f"{merged_all['date'].min()}→{merged_all['date'].max()}")
    return len(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="流通張數 free float(慢變維度)")
    ap.add_argument("--start", default=None, help="只算 >= 此日的集保快照(回補用)")
    ap.add_argument("--stock", default=None, help="只算這一檔(省略=整個 watchlist)")
    args = ap.parse_args()

    token = fc.get_token()
    fc.check_token(token)
    os.makedirs("data/float", exist_ok=True)

    ids = [str(args.stock)] if args.stock else fc.watchlist_ids()
    total = 0
    for sid in ids:
        total += build_for(token, sid, args.start)
    print(f"✅ float 完成,新增 {total} 筆")


if __name__ == "__main__":
    main()
