"""
fetch_revenue.py — 月營收(Block E)→ data/revenue/{id}.csv(廣度層 universe)
資料源 TaiwanStockMonthRevenue(0b 實測:支援全市場單月 call,一次回 ~2300 檔)。

FinMind 欄位:date(公布日=次月1日), revenue, revenue_month, revenue_year, create_time。
  → 實際營收月 revenue_month(YYYY-MM)= f"{revenue_year}-{revenue_month:02d}"。
  → 查詢「公布窗 = 次月」會回「該月營收」:start=2026-05-01/end=2026-05-31 → 2026-04 營收。

產出欄位(yoy/mom/cumulative_yoy 由原始 revenue 自算,分數形式,×100 才是 %):
  revenue_month(YYYY-MM), revenue(元), yoy(對去年同月), mom(對上月),
  cumulative_yoy(今年 YTD 對去年 YTD), source, fetched_at

cadence 月更:守衛「當月 ≥ 11 日且有新月份才抓」,idempotent(write_if_changed)。
路徑:每個「公布窗」做一次全市場單月 call,切 universe。回補 25 個月 ≈ 26 個 call。

用法:
  python scripts/fetch_revenue.py               # nightly:月守衛,只補最新月
  python scripts/fetch_revenue.py --months 25    # 回補 25 個月
  python scripts/fetch_revenue.py --stock 2330
需要:FINMIND_TOKEN;data/info.csv + data/daily/*.csv(算 universe)
"""

import os
import argparse
from datetime import date
import pandas as pd
import finmind_client as fc

DATASET = "TaiwanStockMonthRevenue"
PUBLISH_DAY = 11          # 每月 10 日左右公布上月營收;>=11 視為上月已出
STOP_RATIO = 0.9
COLS = ["revenue_month", "revenue", "yoy", "mom", "cumulative_yoy", "source", "fetched_at"]


def fetch_window(token: str, win: pd.Period) -> pd.DataFrame:
    """全市場單一『公布窗』(=某月)→ 回該月公布的營收(= 前一個月的營收)。"""
    start = win.start_time.date().isoformat()
    end = win.end_time.date().isoformat()
    raw = fc.api_data(token, DATASET, start_date=start, end_date=end)
    if raw.empty:
        return pd.DataFrame()
    raw["stock_id"] = raw["stock_id"].astype(str)
    raw["revenue_month"] = (raw["revenue_year"].astype(int).astype(str) + "-"
                            + raw["revenue_month"].astype(int).astype(str).str.zfill(2))
    return raw[["stock_id", "revenue_month", "revenue"]]


def existing_raw(sid: str) -> pd.DataFrame:
    p = f"data/revenue/{sid}.csv"
    if not os.path.exists(p):
        return pd.DataFrame(columns=["revenue_month", "revenue"])
    d = pd.read_csv(p, dtype=str)
    return d[["revenue_month", "revenue"]]


def compute_derived(df: pd.DataFrame, now: str) -> pd.DataFrame:
    """df:revenue_month, revenue(raw)。回加上 yoy/mom/cumulative_yoy。"""
    df = df.copy()
    df["revenue"] = pd.to_numeric(df["revenue"], errors="coerce")
    df = df.dropna(subset=["revenue"]).drop_duplicates("revenue_month", keep="last")
    df = df.sort_values("revenue_month").reset_index(drop=True)
    rev = dict(zip(df["revenue_month"], df["revenue"]))

    def prev_month(ym, k):
        y, m = map(int, ym.split("-"))
        p = pd.Period(f"{y}-{m:02d}", freq="M") - k
        return f"{p.year}-{p.month:02d}"

    def ratio(a, b):
        return round(a / b - 1, 6) if (b and b > 0 and pd.notna(a)) else ""

    yoys, moms, cums = [], [], []
    for _, r in df.iterrows():
        ym = r["revenue_month"]; y, m = map(int, ym.split("-"))
        moms.append(ratio(r["revenue"], rev.get(prev_month(ym, 1))))
        yoys.append(ratio(r["revenue"], rev.get(prev_month(ym, 12))))
        # 累計 YoY:今年 1..m 對去年 1..m(需兩年同月皆齊全才算)
        cur = [rev.get(f"{y}-{mm:02d}") for mm in range(1, m + 1)]
        prv = [rev.get(f"{y-1}-{mm:02d}") for mm in range(1, m + 1)]
        if all(v is not None for v in cur + prv) and sum(prv) > 0:
            cums.append(round(sum(cur) / sum(prv) - 1, 6))
        else:
            cums.append("")
    df["yoy"], df["mom"], df["cumulative_yoy"] = yoys, moms, cums
    df["revenue"] = df["revenue"].astype("int64")
    df["source"] = "finmind"
    df["fetched_at"] = now
    return df[COLS]


def newest_month(sid: str) -> str:
    d = existing_raw(sid)
    return str(d["revenue_month"].max()) if len(d) else ""


def main() -> None:
    ap = argparse.ArgumentParser(description="月營收(全市場 universe,月更)")
    ap.add_argument("--months", type=int, default=None, help="回補近 N 個營收月(不給=nightly 月守衛)")
    ap.add_argument("--stock", default=None, help="只算這一檔")
    ap.add_argument("--force", action="store_true", help="忽略月守衛,一律抓")
    args = ap.parse_args()

    token = fc.get_token()
    fc.check_token(token)
    os.makedirs("data/revenue", exist_ok=True)
    now = pd.Timestamp.now(tz="Asia/Taipei").isoformat()
    today = date.today()
    cur_win = pd.Period(f"{today.year}-{today.month:02d}", freq="M")

    ids = [str(args.stock)] if args.stock else fc.load_universe()

    # 決定要抓的公布窗
    if args.months:
        windows = [cur_win - i for i in range(0, args.months + 1)]
    else:
        # nightly 月守衛:當月 >=11 日才有上月營收;canary 已有最新月 → no-op
        if not args.force and today.day < PUBLISH_DAY:
            print(f"⏭ 今天 {today}(< {PUBLISH_DAY} 日),上月營收未公布,no-op")
            return
        latest_rev_month = f"{(cur_win - 1).year}-{(cur_win - 1).month:02d}"
        canary = args.stock or "2330"
        if not args.force and newest_month(canary) >= latest_rev_month:
            print(f"⏭ 月營收 canary({canary})已有 {latest_rev_month},no-op")
            return
        windows = [cur_win]      # 只抓當月公布窗(= 上月營收)

    # 逐窗全市場抓
    fetched = {}   # stock_id -> list[(revenue_month, revenue)]
    for i, w in enumerate(windows, 1):
        raw = fetch_window(token, w)
        if not raw.empty:
            keep = raw[raw["stock_id"].isin(set(ids))] if not args.stock else raw[raw["stock_id"] == str(args.stock)]
            for _, r in keep.iterrows():
                fetched.setdefault(str(r["stock_id"]), []).append((r["revenue_month"], r["revenue"]))
        used, lim = fc.token_usage(token)
        print(f"   公布窗 [{i}/{len(windows)}] {w}:{0 if raw.empty else len(raw)} 檔;用量 {used}/{lim}")
        if used and lim and used > lim * STOP_RATIO:
            print("   ⏸ 用量逼近上限,停下續傳")
            break

    changed = 0
    for sid in ids:
        new = pd.DataFrame(fetched.get(sid, []), columns=["revenue_month", "revenue"])
        merged = pd.concat([existing_raw(sid), new], ignore_index=True)
        if merged.empty:
            continue
        out = compute_derived(merged, now)
        if fc.write_if_changed(f"data/revenue/{sid}.csv", out, keys=["revenue_month"], volatile=("fetched_at",)):
            changed += 1
    used, lim = fc.token_usage(token)
    print(f"✅ 月營收完成,更新 {changed} 檔。用量 {used}/{lim}")


if __name__ == "__main__":
    main()
