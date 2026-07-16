"""
fetch_float.py — 流通張數 free float(整套籌碼分析的分母)→ data/float/{id}.csv(廣度層 universe)

* 單位鐵則(見 config/schema.md):一律存「股」,永不存「張」。張 = 股 / 1000,是呈現層的事。*

發行股數 / 外資持股來源:TaiwanStockShareholding(0b 實測:支援全市場單日 call)。
  - issued_shares          = NumberOfSharesIssued(發行股數,股);
                             缺該股時 fallback 用集保 'total' 級距(== 發行股數,已實測)。
  - foreign_holding_shares = ForeignInvestmentShares(外資持股,股;缺則留空)。

鎖倉(locked)—— 誠實註記:FinMind 沒有「董監持股」dataset(已實測皆 422),
所以 locked 用「集保大戶級距」proxy,不是真正董監鎖倉:
  - big_holder_over1000_shares = 集保 'more than 1,000,001'(>1000 張大戶)
  - big_holder_over400_shares  = 集保 >400,000 股(>400 張)四級距總和
  - locked_shares  = big_holder_over1000_shares(千張大戶當強手/鎖倉 proxy)
  - free_float_shares = issued_shares − locked_shares
  - locked_pct        = locked_shares / issued_shares

時間軸=集保快照日(週),對象=universe(load_universe)。每個快照日做一次「全市場單日」
Shareholding call(切 universe),locked 取自各股當週集保。
續傳:RESUME_CANARY(2317)float 已有的快照日當 coverage marker,跳過已算過的。
冪等:write_if_changed(忽略 fetched_at,內容沒變不寫)。

用法:
  python scripts/fetch_float.py                 # nightly/weekly:補新集保日
  python scripts/fetch_float.py --days 130        # 回補約 18 週(需先有 holders 歷史)
  python scripts/fetch_float.py --stock 2330
需要:FINMIND_TOKEN;data/holders/{id}.csv(先跑 fetch_holders.py)
"""

import os
import argparse
from datetime import date, timedelta
import pandas as pd
import finmind_client as fc

DATASET = "TaiwanStockShareholding"
OVER400_LEVELS = ["400,001-600,000", "600,001-800,000",
                  "800,001-1,000,000", "more than 1,000,001"]
OVER1000_LEVEL = "more than 1,000,001"
DATE_CANARY = "2330"       # 快照日清單來源(holders 檔)
RESUME_CANARY = "2317"     # float 續傳 coverage marker
STOP_RATIO = 0.9
COLS = ["date", "issued_shares", "foreign_holding_shares",
        "big_holder_over400_shares", "big_holder_over1000_shares",
        "locked_shares", "free_float_shares", "locked_pct", "source", "fetched_at"]


def holders_big(stock_id: str) -> dict:
    """holders/{id}.csv → {date: (over400, over1000, total)}(股)。"""
    path = f"data/holders/{stock_id}.csv"
    if not os.path.exists(path):
        return {}
    h = pd.read_csv(path, dtype=str)
    h["holding_shares"] = pd.to_numeric(h["holding_shares"], errors="coerce").fillna(0)
    out = {}
    for d, g in h.groupby("date"):
        over400 = int(g[g["level"].isin(OVER400_LEVELS)]["holding_shares"].sum())
        over1000 = int(g[g["level"] == OVER1000_LEVEL]["holding_shares"].sum())
        total = g[g["level"] == "total"]["holding_shares"].sum()
        out[str(d)] = (over400, over1000, int(total))
    return out


def snapshot_dates(stock_id: str, start: str) -> list:
    p = f"data/holders/{stock_id}.csv"
    if not os.path.exists(p):
        return []
    d = pd.read_csv(p, dtype=str)
    d = d[d["date"] >= start]
    return sorted(d["date"].astype(str).unique())


def covered_dates() -> set:
    p = f"data/float/{RESUME_CANARY}.csv"
    if not os.path.exists(p):
        return set()
    return set(pd.read_csv(p, dtype=str)["date"].astype(str))


def fetch_shareholding_day(token: str, d: str) -> dict:
    """全市場單日 Shareholding → {stock_id: (issued, foreign)}。"""
    raw = fc.api_data(token, DATASET, start_date=d, end_date=d)
    m = {}
    if not raw.empty:
        for _, r in raw.iterrows():
            m[str(r["stock_id"])] = (r.get("NumberOfSharesIssued"), r.get("ForeignInvestmentShares"))
    return m


def build_rows(sid: str, todo: list, big: dict, sh_by_date: dict, now: str) -> pd.DataFrame:
    rows = []
    for d in todo:
        if d not in big:
            continue
        over400, over1000, total = big[d]
        issued, foreign = sh_by_date.get(d, {}).get(sid, (None, None))
        issued = pd.to_numeric(issued, errors="coerce")
        if pd.isna(issued) or issued <= 0:
            issued = total if total > 0 else None      # fallback:集保 total == 發行股數
        foreign = pd.to_numeric(foreign, errors="coerce")
        locked = over1000
        if issued and issued > 0:
            free = int(issued) - int(locked)
            pct = round(locked / int(issued), 6)
            issued_out, free_out = int(issued), free
        else:
            issued_out, free_out, pct = "", "", ""
        rows.append({
            "date": d, "issued_shares": issued_out,
            "foreign_holding_shares": (int(foreign) if pd.notna(foreign) else ""),
            "big_holder_over400_shares": over400, "big_holder_over1000_shares": over1000,
            "locked_shares": locked, "free_float_shares": free_out, "locked_pct": pct,
            "source": "finmind", "fetched_at": now,
        })
    return pd.DataFrame(rows, columns=COLS)


def main() -> None:
    ap = argparse.ArgumentParser(description="流通張數 free float(全市場 universe)")
    ap.add_argument("--days", type=int, default=21, help="回補近 N 天(預設 21)")
    ap.add_argument("--start", default=None, help="起始日 YYYY-MM-DD(優先於 --days)")
    ap.add_argument("--stock", default=None, help="只算這一檔")
    ap.add_argument("--force", action="store_true", help="忽略 coverage,一律重算")
    args = ap.parse_args()

    token = fc.get_token()
    fc.check_token(token)
    os.makedirs("data/float", exist_ok=True)
    now = pd.Timestamp.now(tz="Asia/Taipei").isoformat()
    start = args.start or (date.today() - timedelta(days=args.days)).isoformat()

    ids = [str(args.stock)] if args.stock else fc.load_universe()
    sdates = snapshot_dates(DATE_CANARY, start)
    if not sdates:
        print(f"⚠ 無集保快照日({start}起),先跑 fetch_holders.py")
        return
    done = set() if (args.force or args.stock) else covered_dates()
    todo = [d for d in sdates if d not in done]
    if not todo:
        print("⏭ float 全部快照日已覆蓋,no-op")
        return
    print(f"float universe {len(ids)} 檔;快照日待補 {len(todo)} 個({start}起)")

    # 逐快照日抓全市場 Shareholding(切 universe)
    sh_by_date = {}
    for i, d in enumerate(todo, 1):
        sh_by_date[d] = fetch_shareholding_day(token, d)
        used, lim = fc.token_usage(token)
        print(f"   Shareholding [{i}/{len(todo)}] {d}:{len(sh_by_date[d])} 檔;用量 {used}/{lim}")
        if used and lim and used > lim * STOP_RATIO:
            print(f"   ⏸ 用量逼近上限,停下(已抓的快照日照常算,續傳下輪補完)")
            todo = todo[:i]
            break

    changed = 0
    for sid in ids:
        big = holders_big(sid)
        if not big:
            continue
        df = build_rows(sid, todo, big, sh_by_date, now)
        if df.empty:
            continue
        if fc.write_if_changed(f"data/float/{sid}.csv", df, keys=["date"], volatile=("fetched_at",)):
            changed += 1
    used, lim = fc.token_usage(token)
    print(f"✅ float 完成,更新 {changed} 檔。用量 {used}/{lim}")


if __name__ == "__main__":
    main()
