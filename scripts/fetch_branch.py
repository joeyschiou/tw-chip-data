"""
fetch_branch.py — 追蹤股的券商分點日彙總(sponsor 會員限定資料)
逐價位原始分點 → groupby 分點 → 日彙總(含實測加權成本帶)。

單次一天:讀 latest.json 的 last_trading_date,抓那一天。
append 進 data/branch/{id}.csv,已存在同日資料則跳過(去重)。

用法:python scripts/fetch_branch.py
需要:FINMIND_TOKEN(sponsor);data/latest.json;config/watchlist.yaml
"""

import os
import sys
import json
import yaml
import requests
import pandas as pd

# 注意:分點是專屬端點,不是通用的 /data
BRANCH_URL = "https://api.finmindtrade.com/api/v4/taiwan_stock_trading_daily_report"
USERINFO_URL = "https://api.web.finmindtrade.com/v2/user_info"

# 官方明列的資料缺漏日 —— 抓到空要分辨是這些日子,不是錯誤
KNOWN_GAPS = {
    "2022-10-31", "2022-11-01", "2022-11-02", "2022-11-03",
    "2023-01-11", "2023-01-12", "2023-01-13", "2023-01-16", "2023-01-17",
}
DATA_START = "2021-06-30"   # 分點資料起點,早於此無資料


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


def target_date() -> str:
    """從 latest.json 拿最新交易日(先跑過 update.py / fetch_calendar)。"""
    with open("data/latest.json", encoding="utf-8") as f:
        return json.load(f)["last_trading_date"]


def load_watchlist() -> list:
    with open("config/watchlist.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)["tickers"]


def fetch_raw_branch(token: str, stock_id: str, date: str) -> pd.DataFrame:
    """抓單檔單日的逐價位分點原始資料。"""
    params = {"data_id": stock_id, "date": date}
    r = requests.get(BRANCH_URL, headers={"Authorization": f"Bearer {token}"},
                     params=params, timeout=60)
    if r.status_code == 402 or r.status_code == 403:
        sys.exit(f"❌ 權限不足(HTTP {r.status_code})。分點需 sponsor 會員,確認 token 對應已付費帳號。")
    if r.status_code != 200:
        print(f"   ⚠ {stock_id} HTTP {r.status_code},略過")
        return pd.DataFrame()
    return pd.DataFrame(r.json().get("data", []))


def aggregate(raw: pd.DataFrame) -> pd.DataFrame:
    """
    逐價位 → 分點日彙總。
    加權成本:price=0 的列(興櫃造市)排除在價格計算外,但股數仍計入總額。
    """
    raw = raw.copy()
    raw["buy"] = pd.to_numeric(raw["buy"], errors="coerce").fillna(0)
    raw["sell"] = pd.to_numeric(raw["sell"], errors="coerce").fillna(0)
    raw["price"] = pd.to_numeric(raw["price"], errors="coerce").fillna(0)

    # 價格有效的列才算加權成本(排除 price=0 的興櫃造市列)
    priced = raw[raw["price"] > 0].copy()
    priced["buy_notional"] = priced["price"] * priced["buy"]
    priced["sell_notional"] = priced["price"] * priced["sell"]

    def agg_one(gid):
        g = raw[raw["securities_trader_id"] == gid]
        gp = priced[priced["securities_trader_id"] == gid]
        buy = g["buy"].sum()
        sell = g["sell"].sum()
        # 加權均價:分母用「有價格的股數」,避免被 price=0 稀釋
        avg_buy = gp["buy_notional"].sum() / gp["buy"].sum() if gp["buy"].sum() > 0 else None
        avg_sell = gp["sell_notional"].sum() / gp["sell"].sum() if gp["sell"].sum() > 0 else None
        return {
            "broker_id": gid,
            "broker_name": g["securities_trader"].iloc[0],
            "buy_shares": int(buy),
            "sell_shares": int(sell),
            "net_shares": int(buy - sell),
            "avg_buy_price": round(avg_buy, 4) if avg_buy is not None else "",
            "avg_sell_price": round(avg_sell, 4) if avg_sell is not None else "",
        }

    out = pd.DataFrame([agg_one(gid) for gid in raw["securities_trader_id"].unique()])
    out["date"] = raw["date"].iloc[0]
    # 欄位順序照 schema.md
    return out[["date", "broker_id", "broker_name", "buy_shares", "sell_shares",
                "net_shares", "avg_buy_price", "avg_sell_price"]]


def append_dedup(path: str, new: pd.DataFrame) -> int:
    """append 進既有 CSV,若該日期已存在則整批跳過(去重)。回傳新增列數。"""
    if os.path.exists(path):
        old = pd.read_csv(path, dtype={"broker_id": str})
        if new["date"].iloc[0] in old["date"].astype(str).values:
            return 0
        combined = pd.concat([old, new], ignore_index=True)
    else:
        combined = new
    combined.to_csv(path, index=False, encoding="utf-8-sig")
    return len(new)


def main() -> None:
    token = get_token()
    check_token(token)
    date = target_date()

    if date < DATA_START:
        sys.exit(f"❌ {date} 早於分點資料起點 {DATA_START}。")

    os.makedirs("data/branch", exist_ok=True)
    tickers = load_watchlist()

    for t in tickers:
        sid = str(t["id"])
        print(f"→ {sid} {t.get('note','')} @ {date}")
        raw = fetch_raw_branch(token, sid, date)

        if raw.empty:
            if date in KNOWN_GAPS:
                print(f"   ℹ {date} 為官方已知缺漏日,非錯誤")
            else:
                print(f"   ⚠ 空資料(可能今日分點未出,晚點重跑)")
            continue

        agg = aggregate(raw)
        path = f"data/branch/{sid}.csv"
        added = append_dedup(path, agg)
        if added == 0:
            print(f"   ⏭ {date} 已存在,跳過")
        else:
            top = agg.nlargest(1, "net_shares").iloc[0]
            print(f"   ✅ {path}:+{added} 分點。當日最大買超 {top['broker_name']} "
                  f"淨 {top['net_shares']:,} 股 @ 均價 {top['avg_buy_price']}")


if __name__ == "__main__":
    main()
