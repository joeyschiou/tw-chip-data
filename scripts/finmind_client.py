"""
finmind_client.py — 新 fetcher 共用的 FinMind 存取層。
沿用既有慣例(get_token / check_token / watchlist),外加:
  - api_data():帶節流 + rate-limit 感知的重試(不硬打;超限降速重試)。
  - FinMind 的 rate limit 會回 HTTP 402 且 msg 含 "limit";真正權限不足也是 402。
    用 msg 內容區分:含 limit → 退避重試;否則 → 視為權限不足直接停。

既有的 fetch_calendar/daily/branch 維持自帶版本不動;此模組只給新 fetcher 用。
"""

import os
import sys
import time
import yaml
import requests
import pandas as pd

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
USERINFO_URL = "https://api.web.finmindtrade.com/v2/user_info"


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


def watchlist_ids() -> list:
    return [str(t["id"]) for t in load_watchlist()]


def api_data(token: str, dataset: str, throttle: float = 0.3,
             max_retries: int = 5, **params) -> pd.DataFrame:
    """
    打 FinMind /data,回 DataFrame。
    - 402 + msg 含 'limit' / 'upper' → rate limit,指數退避後重試(最多 max_retries)。
    - 402 其他 / 403 → 權限不足,直接 sys.exit(分點/財報等 sponsor 資料)。
    - 5xx / 429 → 退避重試。
    - 其他非 200 → 印警告回空 df(不中斷整條管線)。
    每次成功呼叫後 sleep(throttle) 節流。
    """
    q = {"dataset": dataset}
    q.update({k: v for k, v in params.items() if v is not None})
    headers = {"Authorization": f"Bearer {token}"}

    for attempt in range(max_retries):
        r = requests.get(FINMIND_URL, headers=headers, params=q, timeout=90)
        code = r.status_code
        try:
            msg = (r.json() or {}).get("msg", "") or ""
        except Exception:
            msg = ""
        low = msg.lower()

        if code == 402 and ("limit" in low or "upper" in low):
            wait = min(120, 5 * (2 ** attempt))
            print(f"   ⏳ rate limit(402:{msg[:60]}),{wait}s 後重試 "
                  f"({attempt + 1}/{max_retries})")
            time.sleep(wait)
            continue
        if code in (402, 403):
            sys.exit(f"❌ 權限不足(HTTP {code}:{msg})。dataset={dataset} 需 sponsor。")
        if code == 429 or code >= 500:
            wait = min(120, 5 * (2 ** attempt))
            print(f"   ⏳ HTTP {code},{wait}s 後重試 ({attempt + 1}/{max_retries})")
            time.sleep(wait)
            continue

        time.sleep(throttle)
        if code != 200:
            print(f"   ⚠ {dataset} HTTP {code}({msg[:60]}),略過")
            return pd.DataFrame()
        return pd.DataFrame(r.json().get("data", []))

    print(f"   ⚠ {dataset} 重試 {max_retries} 次仍失敗,略過")
    return pd.DataFrame()


def append_dedup(path: str, new: pd.DataFrame, keys: list) -> pd.DataFrame:
    """append 進既有 CSV,依 keys 去重(keep last),排序後寫回。回傳合併後的 df。"""
    if new is None or new.empty:
        return pd.DataFrame()
    if os.path.exists(path):
        old = pd.read_csv(path, dtype=str)
        combined = pd.concat([old, new.astype(str)], ignore_index=True)
    else:
        combined = new.astype(str)
    combined = combined.drop_duplicates(subset=keys, keep="last").sort_values(keys)
    combined.to_csv(path, index=False, encoding="utf-8-sig")
    return combined
