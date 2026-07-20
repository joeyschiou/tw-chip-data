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
    """
    目標交易日 = calendar 中 is_trading_day 且 <= 今天(Asia/Taipei)的最大日期。
    改讀 calendar.csv(同一輪 fetch_calendar 剛更新過),不讀 latest.json——
    latest.json 是 update.py「跑完所有 fetch 才寫」,fetch_branch 執行當下讀到的是上一晚的,
    會導致每晚都慢一個交易日。
    """
    cal = pd.read_csv("data/calendar.csv", dtype=str)
    today = pd.Timestamp.now(tz="Asia/Taipei").strftime("%Y-%m-%d")
    td = cal[(cal["is_trading_day"].str.lower() == "true") & (cal["date"] <= today)]["date"]
    if td.empty:
        sys.exit("❌ calendar 無 <= 今天的交易日。")
    return str(td.max())


def _trading_days(cal, after: str, upto: str) -> list:
    m = (cal["is_trading_day"].str.lower() == "true") & (cal["date"] > after) & (cal["date"] <= upto)
    return sorted(cal[m]["date"].astype(str).tolist())


def _branch_max(path: str) -> str:
    if not os.path.exists(path):
        return ""      # 沒檔 → 從最早算,靠下面 cap 5 限制
    d = pd.read_csv(path, dtype=str)
    return str(d["date"].max()) if len(d) else ""


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
    target = target_date()
    if target < DATA_START:
        sys.exit(f"❌ {target} 早於分點資料起點 {DATA_START}。")

    cal = pd.read_csv("data/calendar.csv", dtype=str)
    os.makedirs("data/branch", exist_ok=True)
    tickers = load_watchlist()
    total = 0

    # gap-fill:每檔補「CSV 最大日期」到「目標日」之間所有缺的交易日(上限 5,跳 KNOWN_GAPS),
    # 逐日抓、逐日 append_dedup → 缺一天會自動自癒,不用手動回補。
    for t in tickers:
        sid = str(t["id"])
        path = f"data/branch/{sid}.csv"
        cmax = _branch_max(path)
        days = [d for d in _trading_days(cal, cmax, target)
                if d >= DATA_START and d not in KNOWN_GAPS][-5:]
        if not days:
            print(f"→ {sid} {t.get('note','')} 已最新(至 {cmax or '無檔'})")
            continue
        print(f"→ {sid} {t.get('note','')}:待補 {days}")
        for d in days:
            raw = fetch_raw_branch(token, sid, d)
            if raw.empty:
                if d == target:
                    # 目標日分點批次偶爾延到隔日早上:警告、不 append、不紅燈(落後由 latest.json status 反映)
                    print(f"   ⚠ 目標日 {d} 空(分點批次可能延到隔日早上),不 append")
                else:
                    print(f"   ⚠ {d} 空資料(非 KNOWN_GAPS),略過")
                continue
            added = append_dedup(path, aggregate(raw))
            total += added
            print(f"   ✅ {d}:+{added} 分點" if added else f"   ⏭ {d} 已存在")

    print(f"✅ 分點 gap-fill 完成,共補 {total} 分點列(目標日 {target})。")


if __name__ == "__main__":
    main()
