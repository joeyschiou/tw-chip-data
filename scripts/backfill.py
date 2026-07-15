"""
backfill.py — 一次性歷史回填(daily + 分點,近一年)
斷點續傳:已存在日期自動跳過,可安全中斷後重跑。
節流:每次請求間隔 SLEEP 秒,避開 IP 封鎖風險。

用法:python scripts/backfill.py
需要:FINMIND_TOKEN(sponsor);config/watchlist.yaml;data/calendar.csv

注意:這是一次性腳本,跟每晚的 update.py 分開。跑完 push,之後 Actions 只 append 當天。
"""

import os
import sys
import time
import yaml
import requests
import pandas as pd
from datetime import date, timedelta

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
BRANCH_URL = "https://api.finmindtrade.com/api/v4/taiwan_stock_trading_daily_report"
USERINFO_URL = "https://api.web.finmindtrade.com/v2/user_info"

SLEEP = 0.5                      # 每次請求間隔秒數(節流)
BACKFILL_START = (date.today() - timedelta(days=365)).isoformat()   # 近一年
BRANCH_DATA_START = "2021-06-30"
KNOWN_GAPS = {
    "2022-10-31", "2022-11-01", "2022-11-02", "2022-11-03",
    "2023-01-11", "2023-01-12", "2023-01-13", "2023-01-16", "2023-01-17",
}


def get_token() -> str:
    token = os.environ.get("FINMIND_TOKEN")
    if not token:
        sys.exit("❌ 找不到環境變數 FINMIND_TOKEN。")
    return token


def check_token(token: str) -> None:
    r = requests.get(USERINFO_URL, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if r.status_code != 200:
        sys.exit(f"❌ token 驗證失敗(HTTP {r.status_code})。")
    info = r.json()
    print(f"✅ token 有效。本小時已用 {info.get('user_count','?')}/{info.get('api_request_limit','?')} 次。")


def load_watchlist() -> list:
    with open("config/watchlist.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)["tickers"]


def trading_days(start: str) -> list:
    """從 calendar 取 start 之後的所有交易日。"""
    cal = pd.read_csv("data/calendar.csv")
    return sorted(cal[cal["date"] >= start]["date"].astype(str).tolist())


# ---------- daily 回填(區間,一次一檔) ----------

def backfill_daily(token: str, stock_id: str) -> None:
    from importlib import import_module
    # 直接重用 fetch_daily 的邏輯:改 START_DATE 後呼叫 build_daily
    import fetch_daily
    fetch_daily.START_DATE = BACKFILL_START
    df = fetch_daily.build_daily(token, stock_id)
    if df.empty:
        print(f"   ⚠ {stock_id} daily 無資料")
        return
    path = f"data/daily/{stock_id}.csv"
    # 合併既有(去重,保留較長的歷史)
    if os.path.exists(path):
        old = pd.read_csv(path, dtype=str)
        df = pd.concat([old, df.astype(str)], ignore_index=True)
        df = df.drop_duplicates(subset="date", keep="last").sort_values("date")
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"   ✅ daily {path}:{len(df)} 筆,{df.date.iloc[0]}→{df.date.iloc[-1]}")


# ---------- 分點回填(逐日) ----------

def fetch_raw_branch(token: str, stock_id: str, d: str) -> pd.DataFrame:
    r = requests.get(BRANCH_URL, headers={"Authorization": f"Bearer {token}"},
                     params={"data_id": stock_id, "date": d}, timeout=60)
    if r.status_code in (402, 403):
        sys.exit(f"❌ 權限不足(HTTP {r.status_code})。分點需 sponsor。")
    if r.status_code != 200:
        return pd.DataFrame()
    return pd.DataFrame(r.json().get("data", []))


def aggregate(raw: pd.DataFrame) -> pd.DataFrame:
    raw = raw.copy()
    for c in ("buy", "sell", "price"):
        raw[c] = pd.to_numeric(raw[c], errors="coerce").fillna(0)
    priced = raw[raw["price"] > 0].copy()
    priced["bn"] = priced["price"] * priced["buy"]
    priced["sn"] = priced["price"] * priced["sell"]
    rows = []
    for gid in raw["securities_trader_id"].unique():
        g = raw[raw["securities_trader_id"] == gid]
        gp = priced[priced["securities_trader_id"] == gid]
        buy, sell = g["buy"].sum(), g["sell"].sum()
        abp = gp["bn"].sum() / gp["buy"].sum() if gp["buy"].sum() > 0 else None
        asp = gp["sn"].sum() / gp["sell"].sum() if gp["sell"].sum() > 0 else None
        rows.append({
            "date": raw["date"].iloc[0], "broker_id": gid,
            "broker_name": g["securities_trader"].iloc[0],
            "buy_shares": int(buy), "sell_shares": int(sell),
            "net_shares": int(buy - sell),
            "avg_buy_price": round(abp, 4) if abp is not None else "",
            "avg_sell_price": round(asp, 4) if asp is not None else "",
        })
    return pd.DataFrame(rows)[["date", "broker_id", "broker_name", "buy_shares",
                               "sell_shares", "net_shares", "avg_buy_price", "avg_sell_price"]]


def existing_branch_dates(path: str) -> set:
    if not os.path.exists(path):
        return set()
    return set(pd.read_csv(path, dtype=str)["date"].tolist())


def backfill_branch(token: str, stock_id: str, days: list) -> None:
    path = f"data/branch/{stock_id}.csv"
    done = existing_branch_dates(path)
    todo = [d for d in days if d >= BRANCH_DATA_START and d not in done and d not in KNOWN_GAPS]
    if not todo:
        print(f"   ⏭ {stock_id} 分點已完整,無需回填")
        return
    print(f"   分點待補 {len(todo)} 天(已有 {len(done)} 天)")

    collected = []
    for i, d in enumerate(todo, 1):
        raw = fetch_raw_branch(token, stock_id, d)
        if not raw.empty:
            collected.append(aggregate(raw))
        if i % 20 == 0 or i == len(todo):
            print(f"      {i}/{len(todo)} … {d}")
        time.sleep(SLEEP)
        # 每 40 天存一次檔(斷點保護:中途斷了已抓的不白費)
        if collected and (i % 40 == 0 or i == len(todo)):
            _flush_branch(path, collected)
            collected = []
    print(f"   ✅ 分點 {stock_id} 回填完成")


def _flush_branch(path: str, batches: list) -> None:
    new = pd.concat(batches, ignore_index=True)
    if os.path.exists(path):
        old = pd.read_csv(path, dtype=str)
        new = pd.concat([old, new.astype(str)], ignore_index=True)
        new = new.drop_duplicates(subset=["date", "broker_id"], keep="last")
    new = new.sort_values(["date", "broker_id"])
    new.to_csv(path, index=False, encoding="utf-8-sig")


def main() -> None:
    token = get_token()
    check_token(token)
    tickers = load_watchlist()
    days = trading_days(BACKFILL_START)
    print(f"\n回填範圍:{BACKFILL_START} → 今天,共 {len(days)} 個交易日")
    print(f"預估分點請求:約 {len(tickers) * len(days)} 次(扣除已抓的會更少)\n")

    for t in tickers:
        sid = str(t["id"])
        print(f"→ {sid} {t.get('note','')}")
        backfill_daily(token, sid)
        backfill_branch(token, sid, days)
        print()

    print("✅ 全部回填完成。記得 git push,並重跑 update.py 更新 latest.json。")


if __name__ == "__main__":
    main()
