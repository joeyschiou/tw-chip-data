"""
fetch_daily.py — 追蹤股的日K + 三大法人 + 融資融券
免費層可用。對 watchlist ∪ universe 逐檔抓三個 dataset,合併成 data/daily/{id}.csv。
免費額度一晚只夠 ~180 檔:watchlist 優先,其餘依最後日期由舊到新輪轉(約 2 週輪完一圈)。

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
RESERVE_CALLS = 60          # 用量守衛:留給補充腳本的額度(免費帳號 600/hr)
DEAD_DAYS = 120             # 最後日期早於此天數 → 視為下市/停牌,輪轉排最後、不做缺口回補


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


def fetch_dataset(token: str, dataset: str, stock_id: str, start: str = None) -> pd.DataFrame:
    """打一個 FinMind dataset,回 DataFrame(空的話回空 df,不中斷)。"""
    params = {"dataset": dataset, "data_id": stock_id, "start_date": start or START_DATE}
    r = requests.get(FINMIND_URL, headers={"Authorization": f"Bearer {token}"},
                     params=params, timeout=60)
    if r.status_code != 200:
        print(f"   ⚠ {dataset} HTTP {r.status_code},略過此資料集")
        return pd.DataFrame()
    rows = r.json().get("data", [])
    return pd.DataFrame(rows)


def build_daily(token: str, stock_id: str, start: str = None) -> pd.DataFrame:
    # --- 1. 日K(骨架)---
    price = fetch_dataset(token, "TaiwanStockPrice", stock_id, start)
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
    inst = fetch_dataset(token, "TaiwanStockInstitutionalInvestorsBuySellWide", stock_id, start)
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
    margin = fetch_dataset(token, "TaiwanStockMarginPurchaseShortSale", stock_id, start)
    if not margin.empty:
        # FinMind 這兩欄單位是「張」;schema 鐵則存「股」→ ×1000。Int64 避免 .0。
        m_out = pd.DataFrame({
            "date": margin["date"],
            "margin_balance_shares": (pd.to_numeric(margin["MarginPurchaseTodayBalance"],
                                                    errors="coerce") * 1000).astype("Int64"),
            "short_balance_shares": (pd.to_numeric(margin["ShortSaleTodayBalance"],
                                                   errors="coerce") * 1000).astype("Int64"),
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


def _last_date(path: str) -> str:
    """daily 檔最後一列的 date(第一欄);沒檔/空檔回 ""。只讀檔尾,不載整檔。"""
    if not os.path.exists(path):
        return ""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 4096))
        lines = f.read().decode("utf-8-sig", errors="ignore").strip().splitlines()
    last = lines[-1].split(",")[0] if lines else ""
    return last if last[:2] == "20" else ""


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
    ap.add_argument("--checkpoint", default=None,
                    help="續傳 checkpoint 檔:記錄已完成代號,重跑自動跳過(大回補用)")
    args = ap.parse_args()

    global START_DATE
    START_DATE = args.start if args.start else (date.today() - timedelta(days=args.days)).isoformat()

    token = get_token()
    check_token(token)

    wl_ids = [str(t["id"]) for t in load_watchlist()]
    uni_ids = load_universe_ids()
    # 聯集去重:watchlist 可含不在 universe 的(如上櫃 6831/7795)
    # 免費帳號 600 call/hr、一檔 3 call → 一晚只抓得完 ~180 檔,不夠覆蓋全 universe。
    # 排序:watchlist 先(深度層一定要新)→ universe 依「最後日期」由舊到新輪轉
    # → 無 daily 檔、或最後日期早於 DEAD_DAYS 前(多半已下市/長期停牌)放最後,不每晚浪費額度。
    last = {s: _last_date(f"data/daily/{s}.csv") for s in dict.fromkeys(uni_ids + wl_ids)}
    dead_cut = (date.today() - timedelta(days=DEAD_DAYS)).isoformat()
    wl_set = set(wl_ids)
    rest = [s for s in uni_ids if s not in wl_set]
    rest.sort(key=lambda s: (last[s] < dead_cut, last[s]))
    target_ids = list(dict.fromkeys(wl_ids + rest))
    if args.new_only:
        before = len(target_ids)
        target_ids = [s for s in target_ids if not os.path.exists(f"data/daily/{s}.csv")]
        print(f"--new-only:{before} 檔中 {len(target_ids)} 檔尚無 daily(既有跳過)")
    # checkpoint 續傳:跳過已完成的代號(大回補中途被用量守衛停下時不用從頭再抓)
    if args.checkpoint:
        done = set()
        if os.path.exists(args.checkpoint):
            with open(args.checkpoint, encoding="utf-8") as f:
                done = {line.strip() for line in f if line.strip()}
        before = len(target_ids)
        target_ids = [s for s in target_ids if s not in done]
        print(f"checkpoint({args.checkpoint}):已完成 {len(done)} 檔,{before} → 剩 {len(target_ids)} 檔待抓")
    print(f"日線目標:universe {len(uni_ids)} + watchlist {len(wl_ids)} → 目標 {len(target_ids)} 檔")
    print(f"起始日期 START_DATE = {START_DATE}\n")

    os.makedirs("data/daily", exist_ok=True)

    def _mark_done(sid: str) -> None:
        """記進 checkpoint(含『無資料』的股,否則下輪會一直重試)。"""
        if args.checkpoint:
            with open(args.checkpoint, "a", encoding="utf-8") as f:
                f.write(sid + "\n")

    for i, sid in enumerate(target_ids, 1):
        # 用量守衛:逼近上限就停(續傳靠 --checkpoint / --new-only)
        # 每 10 檔查一次(一檔 3 call,兩次檢查間最多 30 call);免費帳號上限只有 600,
        # 停在 lim - RESERVE_CALLS,留額度給後面的 info/macro 等補充腳本(實測 50 檔一查會衝到 600/600)。
        if i % 10 == 1 and i > 1:
            used, lim = _usage(token)
            if used and lim and used >= lim - RESERVE_CALLS:
                print(f"   ⏸ 用量逼近上限({used}/{lim}),停下續傳"
                      f"(重跑同一道指令即可從 checkpoint 接續)")
                break
        print(f"→ [{i}/{len(target_ids)}] {sid}")
        # 起點取 START_DATE 與既有最後日期較早者:落後超過 --days 的檔也能補齊缺口
        start = min(START_DATE, last[sid]) if last.get(sid, "") >= dead_cut else START_DATE
        df = build_daily(token, sid, start)
        if df.empty:
            _mark_done(sid)
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
        _mark_done(sid)
        time.sleep(0.15)


if __name__ == "__main__":
    main()
