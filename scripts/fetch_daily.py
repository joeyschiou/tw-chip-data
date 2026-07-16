"""
fetch_daily.py — 追蹤股的日K + 三大法人 + 融資融券
免費層可用。讀 config/watchlist.yaml,逐檔抓三個 dataset,合併成 data/daily/{id}.csv。

用法:python scripts/fetch_daily.py
需要:FINMIND_TOKEN 環境變數;config/watchlist.yaml
"""

import os
import sys
import io
import time
import argparse
import yaml
import requests
import pandas as pd
from datetime import date, timedelta

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
USERINFO_URL = "https://api.web.finmindtrade.com/v2/user_info"
START_DATE = "2024-01-01"   # 第一次抓近兩年;歷史回填是步驟 7 的事


def get_token() -> str:
    token = os.environ.get("FINMIND_TOKEN")
    if not token:
        sys.exit("❌ 找不到環境變數 FINMIND_TOKEN。")
    return token


def check_token(token: str) -> None:
    r = requests.get(USERINFO_URL, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if r.status_code != 200:
        sys.exit(f"❌ token 驗證失敗(HTTP {r.status_code})。去官網重取。")
    info = r.json()
    print(f"✅ token 有效。本小時已用 {info.get('user_count','?')}/{info.get('api_request_limit','?')} 次。")


def load_watchlist() -> list:
    with open("config/watchlist.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg["tickers"]


def load_universe_ids() -> list:
    """讀 config/universe.csv 回 id 清單(檔案不存在則回 [])。"""
    path = "config/universe.csv"
    if not os.path.exists(path):
        return []
    uni = pd.read_csv(path, dtype=str)
    return uni["id"].astype(str).tolist()


def fetch_dataset(token: str, dataset: str, stock_id: str) -> pd.DataFrame:
    """打一個 FinMind dataset,回 DataFrame(空的話回空 df,不中斷)。"""
    params = {"dataset": dataset, "data_id": stock_id, "start_date": START_DATE}
    r = requests.get(FINMIND_URL, headers={"Authorization": f"Bearer {token}"},
                     params=params, timeout=60)
    if r.status_code != 200:
        print(f"   ⚠ {dataset} HTTP {r.status_code},略過此資料集")
        return pd.DataFrame()
    rows = r.json().get("data", [])
    return pd.DataFrame(rows)


def build_daily(token: str, stock_id: str) -> pd.DataFrame:
    # --- 1. 日K(骨架)---
    price = fetch_dataset(token, "TaiwanStockPrice", stock_id)
    if price.empty:
        print(f"   ⚠ {stock_id} 無日K資料,跳過")
        return pd.DataFrame()

    df = pd.DataFrame({
        "date": price["date"],
        "open": price["open"],
        "high": price["max"],           # FinMind 欄名是 max/min
        "low": price["min"],
        "close": price["close"],
        "volume_shares": price["Trading_Volume"],
        "value_twd": price["Trading_money"],
        "transactions": price["Trading_turnover"],
    })
    # 漲跌停:用 spread 判(近似;精確需漲跌停價,之後可加)
    df["limit_up"] = ""
    df["limit_down"] = ""

    # --- 2. 三大法人(寬表)---
    inst = fetch_dataset(token, "TaiwanStockInstitutionalInvestorsBuySellWide", stock_id)
    if not inst.empty:
        inst_out = pd.DataFrame({"date": inst["date"]})
        inst_out["foreign_net_shares"] = inst["Foreign_Investor_buy"] - inst["Foreign_Investor_sell"]
        inst_out["trust_net_shares"] = inst["Investment_Trust_buy"] - inst["Investment_Trust_sell"]
        # 自營合併新制兩欄(文件明示:2014-12 起拆 self/Hedging)
        dealer_buy = inst.get("Dealer_self_buy", 0) + inst.get("Dealer_Hedging_buy", 0)
        dealer_sell = inst.get("Dealer_self_sell", 0) + inst.get("Dealer_Hedging_sell", 0)
        inst_out["dealer_net_shares"] = dealer_buy - dealer_sell
        df = df.merge(inst_out, on="date", how="left")   # left join:沒資料留空,不填 0
    else:
        df["foreign_net_shares"] = ""
        df["trust_net_shares"] = ""
        df["dealer_net_shares"] = ""

    # --- 3. 融資融券(取今日餘額,存量)---
    margin = fetch_dataset(token, "TaiwanStockMarginPurchaseShortSale", stock_id)
    if not margin.empty:
        m_out = pd.DataFrame({
            "date": margin["date"],
            "margin_balance_shares": margin["MarginPurchaseTodayBalance"],
            "short_balance_shares": margin["ShortSaleTodayBalance"],
        })
        df = df.merge(m_out, on="date", how="left")
    else:
        df["margin_balance_shares"] = ""
        df["short_balance_shares"] = ""

    df["daytrade_ratio"] = ""     # 免費層無;之後可補
    df["source"] = "finmind"
    df["fetched_at"] = pd.Timestamp.now(tz="Asia/Taipei").isoformat()

    df = df.sort_values("date").reset_index(drop=True)
    return df


def _usage(token: str) -> tuple:
    try:
        r = requests.get(USERINFO_URL, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        if r.status_code == 200:
            j = r.json()
            return j.get("user_count"), j.get("api_request_limit")
    except Exception:
        pass
    return None, None


def main() -> None:
    ap = argparse.ArgumentParser(description="日線廣掃(universe ∪ watchlist)")
    ap.add_argument("--days", type=int, default=30, help="回抓近 N 天(預設 30)")
    ap.add_argument("--start", default=None,
                    help="直接指定起始日期 YYYY-MM-DD(優先於 --days)")
    ap.add_argument("--new-only", action="store_true",
                    help="只抓還沒有 daily 檔的代號(回補新增 delta 用,既有跳過 0 call)")
    args = ap.parse_args()

    global START_DATE
    START_DATE = args.start if args.start else (date.today() - timedelta(days=args.days)).isoformat()

    token = get_token()
    check_token(token)

    wl_ids = [str(t["id"]) for t in load_watchlist()]
    uni_ids = load_universe_ids()
    # 聯集去重(保留順序):universe 先,watchlist 補上不在 universe 的(如上櫃 6831/7795)
    target_ids = list(dict.fromkeys(uni_ids + wl_ids))
    if args.new_only:
        before = len(target_ids)
        target_ids = [s for s in target_ids if not os.path.exists(f"data/daily/{s}.csv")]
        print(f"--new-only:{before} 檔中 {len(target_ids)} 檔尚無 daily(既有跳過)")
    print(f"日線目標:universe {len(uni_ids)} + watchlist {len(wl_ids)} → 目標 {len(target_ids)} 檔")
    print(f"起始日期 START_DATE = {START_DATE}\n")

    os.makedirs("data/daily", exist_ok=True)
    for i, sid in enumerate(target_ids, 1):
        # 用量守衛:逼近上限就停(--new-only 可續傳:已完成的有檔,下輪跳過)
        if i % 200 == 1 and i > 1:
            used, lim = _usage(token)
            if used and lim and used > lim * 0.9:
                print(f"   ⏸ 用量逼近上限({used}/{lim}),停下續傳(下輪 --new-only 補完)")
                break
        print(f"→ [{i}/{len(target_ids)}] {sid}")
        df = build_daily(token, sid)
        if df.empty:
            time.sleep(0.15)
            continue
        # 驗收:OHLC 內部一致、日期唯一遞增
        assert df.date.is_unique, f"{sid} 日期重複"
        bad = df[~df["close"].astype(float).between(df["low"].astype(float), df["high"].astype(float))]
        if len(bad):
            print(f"   ⚠ {sid} 有 {len(bad)} 筆 close 不在 low~high 內,請檢查")
        out = f"data/daily/{sid}.csv"
        # append + 去重(取代原本直接覆寫;與 backfill.py 的 backfill_daily 同模式)
        if os.path.exists(out):
            old = pd.read_csv(out, dtype=str)
            df = pd.concat([old, df.astype(str)], ignore_index=True).drop_duplicates("date", keep="last").sort_values("date")
        df.to_csv(out, index=False, encoding="utf-8-sig")
        print(f"   ✅ {out}:{len(df)} 筆,{df.date.iloc[0]}→{df.date.iloc[-1]}")
        time.sleep(0.15)


if __name__ == "__main__":
    main()
