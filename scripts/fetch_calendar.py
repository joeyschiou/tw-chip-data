"""
fetch_calendar.py — 台股交易日曆
管線的第一支腳本:打通 FinMind → 驗 token → 寫 CSV 整條路。

產出:data/calendar.csv  欄位:date, seq, is_trading_day
用法:python scripts/fetch_calendar.py
需要環境變數:FINMIND_TOKEN
"""

import os
import sys
import requests
import pandas as pd

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
USERINFO_URL = "https://api.web.finmindtrade.com/v2/user_info"

# 日曆抓多長:從這天到今天。第一次先抓近兩年就夠打通管線。
START_DATE = "2024-01-01"


def get_token() -> str:
    """從環境變數拿 token,沒有就明確報錯停止(不靜默)。"""
    token = os.environ.get("FINMIND_TOKEN")
    if not token:
        sys.exit("❌ 找不到環境變數 FINMIND_TOKEN。本機測試請先 setx 或在同一個 shell 設定。")
    return token


def check_token(token: str) -> None:
    """開頭先驗 token 有效 + 額度還在。無效直接停,避免後面靜默抓空。"""
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(USERINFO_URL, headers=headers, timeout=30)
    if resp.status_code != 200:
        sys.exit(f"❌ token 驗證失敗(HTTP {resp.status_code})。token 可能失效,去官網重取。")
    info = resp.json()
    used = info.get("user_count", "?")
    limit = info.get("api_request_limit", "?")
    print(f"✅ token 有效。本小時已用 {used}/{limit} 次。")


def fetch_calendar(token: str) -> pd.DataFrame:
    """
    用 TaiwanStockPrice 的 2330(台積電)日期序列當交易日曆的骨架。
    台積電幾乎每個交易日都有成交,是最可靠的『哪天有開市』來源。
    """
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "dataset": "TaiwanStockPrice",
        "data_id": "2330",
        "start_date": START_DATE,
    }
    resp = requests.get(FINMIND_URL, headers=headers, params=params, timeout=60)
    if resp.status_code != 200:
        sys.exit(f"❌ 抓日曆失敗(HTTP {resp.status_code}):{resp.text[:200]}")

    payload = resp.json()
    rows = payload.get("data", [])
    if not rows:
        sys.exit(f"❌ 回傳空資料。FinMind 訊息:{payload.get('msg', '(無)')}")

    df = pd.DataFrame(rows)
    # 只留日期,去重、排序,建交易日曆
    cal = (
        df[["date"]]
        .drop_duplicates()
        .sort_values("date")
        .reset_index(drop=True)
    )
    cal["seq"] = range(1, len(cal) + 1)      # 交易日序號:免心算營業日
    cal["is_trading_day"] = True             # 這張表只收有開市的日子
    return cal


def main() -> None:
    token = get_token()
    check_token(token)

    cal = fetch_calendar(token)

    # 驗收 assert:序列必須遞增且不重複(schema.md 的完整性閘門機器版)
    assert cal.date.is_monotonic_increasing, "日期沒有遞增,資料有問題"
    assert cal.date.is_unique, "日期有重複,資料有問題"

    os.makedirs("data", exist_ok=True)
    out_path = "data/calendar.csv"
    cal.to_csv(out_path, index=False, encoding="utf-8")

    print(f"✅ 寫出 {out_path}:{len(cal)} 個交易日")
    print(f"   範圍 {cal.date.iloc[0]} → {cal.date.iloc[-1]}")


if __name__ == "__main__":
    main()
