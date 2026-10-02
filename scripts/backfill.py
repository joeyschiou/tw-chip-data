"""
backfill.py — 一次性歷史回填(daily,近一年)
FinMind 免費帳號後:分點/集保/當沖/流通 回填已移除(付費 dataset)。
斷點續傳:已存在日期自動跳過,可安全中斷後重跑。
節流:每次請求間隔 SLEEP 秒,避開 IP 封鎖風險。

用法:python scripts/backfill.py
需要:FINMIND_TOKEN;config/watchlist.yaml

注意:這是一次性腳本,跟每晚的 update.py 分開。跑完 push,之後 Actions 只 append 當天。
"""

import os
import sys
import time
import argparse
import yaml
import requests
import pandas as pd
from datetime import date, timedelta

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
USERINFO_URL = "https://api.web.finmindtrade.com/v2/user_info"

SLEEP = 0.5                      # 每次請求間隔秒數(節流)
BACKFILL_START = (date.today() - timedelta(days=365)).isoformat()   # 近一年


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


def main() -> None:
    ap = argparse.ArgumentParser(description="歷史回填(daily)")
    ap.add_argument("--stock", default=None,
                    help="只回填這一檔代號(省略則回填整個 watchlist)")
    ap.add_argument("--days", type=int, default=365,
                    help="回填近 N 天(預設 365)")
    ap.add_argument("--datasets", default="daily",
                    help="要回填哪些(逗號分隔):daily(免費帳號僅此項)")
    ap.add_argument("--tickers", default=None,
                    help='逗號清單覆蓋 watchlist(定向回補,可含非 watchlist 股),例 "6831,7795"')
    args = ap.parse_args()

    global BACKFILL_START
    BACKFILL_START = (date.today() - timedelta(days=args.days)).isoformat()
    sets = {s.strip() for s in args.datasets.split(",") if s.strip()}
    valid = {"daily"}
    bad = sets - valid
    if bad:
        sys.exit(f"❌ 不支援的 dataset:{bad}(分點/集保/當沖/流通需 FinMind 付費帳號,已移除)。可選:{sorted(valid)}")

    token = get_token()
    check_token(token)

    # 目標代號:--tickers(明確清單,可含非 watchlist)> --stock > watchlist
    if args.tickers:
        tickers = [{"id": s.strip(), "note": "(定向)"} for s in args.tickers.split(",") if s.strip()]
    elif args.stock:
        tickers = [t for t in load_watchlist() if str(t["id"]) == str(args.stock)]
        if not tickers:
            sys.exit(f"❌ watchlist 裡找不到 {args.stock}。請先跑 "
                     f"python scripts/ensure_watchlist.py --stock {args.stock} --market <twse|tpex>。")
    else:
        tickers = load_watchlist()

    print(f"\ndaily 起 {BACKFILL_START}\n")
    for t in tickers:
        sid = str(t["id"])
        print(f"→ {sid} {t.get('note','')}")
        backfill_daily(token, sid)
        time.sleep(SLEEP)
        print()

    print("✅ 全部回填完成。記得 git push,並重跑 update.py --manifest-only 更新 latest.json。")


if __name__ == "__main__":
    main()
