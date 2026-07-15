"""
update.py — 每日主控:串接所有 fetch 腳本,產出 data/latest.json 清單檔。
判讀時第一個讀 latest.json,兩秒知道每個資料集截到哪天。

用法:python scripts/update.py
需要:FINMIND_TOKEN;先存在 fetch_calendar.py / fetch_daily.py
"""

import os
import sys
import json
import glob
import argparse
import subprocess
import pandas as pd

SCRIPTS = ["fetch_calendar.py", "fetch_universe.py", "fetch_daily.py", "fetch_branch.py"]


def run_script(name: str) -> None:
    """跑一支子腳本,失敗就中斷整個更新(寧可紅燈也不要靜默半套)。"""
    path = os.path.join("scripts", name)
    print(f"\n=== 執行 {name} ===")
    result = subprocess.run([sys.executable, path])
    if result.returncode != 0:
        sys.exit(f"❌ {name} 失敗(exit {result.returncode}),中止更新。")


def last_trading_date() -> str:
    """calendar 的最後一個交易日,當作『最新』的基準。"""
    cal = pd.read_csv("data/calendar.csv")
    return str(cal["date"].max())


def dataset_status(files: list, col: str, latest_day: str) -> dict:
    """
    掃一批 CSV,取每檔 col 欄的最大日期,回 {through, status}。
    through = 這批資料裡最舊的『最後日期』(木桶效應:最落後的那檔決定整體)。
    files 只傳 watchlist 的 daily 檔當 canary,避免被上千檔冷門股拖累。
    """
    files = [f for f in files if os.path.exists(f)]
    if not files:
        return {"through": None, "status": "missing"}
    last_dates = []
    for f in files:
        df = pd.read_csv(f)
        sub = df[df[col].notna()] if col in df.columns else df
        if len(sub):
            last_dates.append(str(sub["date"].max()))
    if not last_dates:
        return {"through": None, "status": "missing"}
    through = min(last_dates)                    # 最落後的那檔
    status = "ok" if through >= latest_day else "lagging"
    return {"through": through, "status": status}


def universe_report(latest_day: str) -> dict:
    """
    universe 廣掃概況:
      count       = config/universe.csv 檔數
      daily_files = data/daily/*.csv 實際檔數
      current     = 有多少 daily 檔的最大日期 >= last_trading_date
                    (用 >= 而非 ==:price 常在 calendar 更新前就有當日資料,
                     資料領先行事曆時 == 會誤判成 0)
    """
    count = 0
    if os.path.exists("config/universe.csv"):
        count = len(pd.read_csv("config/universe.csv", dtype=str))
    daily_files = glob.glob("data/daily/*.csv")
    current = 0
    for f in daily_files:
        try:
            d = pd.read_csv(f, usecols=["date"], dtype=str)
        except Exception:
            continue
        if len(d) and str(d["date"].max()) >= latest_day:
            current += 1
    return {"count": count, "daily_files": len(daily_files), "current": current}


def write_latest() -> None:
    latest_day = last_trading_date()
    now_tpe = pd.Timestamp.now(tz="Asia/Taipei")

    # 讀 watchlist 拿追蹤清單
    import yaml
    with open("config/watchlist.yaml", encoding="utf-8") as f:
        tickers = [t["id"] for t in yaml.safe_load(f)["tickers"]]

    # canary 只掃 watchlist 的 daily 檔(避免被上千檔冷門股的落後日期拖累)
    wl_files = [f"data/daily/{sid}.csv" for sid in tickers]

    manifest = {
        "generated_at_utc": now_tpe.tz_convert("UTC").isoformat(),
        "generated_at_taipei": now_tpe.strftime("%Y-%m-%d %H:%M:%S"),
        "last_trading_date": latest_day,
        "datasets": {
            "price":  dataset_status(wl_files, "close", latest_day),
            "inst":   dataset_status(wl_files, "foreign_net_shares", latest_day),
            "margin": dataset_status(wl_files, "margin_balance_shares", latest_day),
        },
        "universe": universe_report(latest_day),
        "tickers": tickers,
    }

    with open("data/latest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print("\n=== latest.json ===")
    for name, d in manifest["datasets"].items():
        flag = "✅" if d["status"] == "ok" else "⚠"
        print(f"  {flag} {name:8s} 截至 {d['through']}  ({d['status']})  [watchlist canary]")
    u = manifest["universe"]
    print(f"  universe:{u['count']} 檔清單 / {u['daily_files']} 個 daily 檔 / {u['current']} 檔已到最新")
    print(f"  最新交易日:{latest_day}")


def main() -> None:
    ap = argparse.ArgumentParser(description="每日主控 / 清單刷新")
    ap.add_argument("--manifest-only", action="store_true",
                    help="只刷新 latest.json,跳過 fetch 腳本(不碰 FinMind)")
    args = ap.parse_args()

    if not args.manifest_only:
        for s in SCRIPTS:
            run_script(s)
    write_latest()
    print("\n✅ 更新完成。data/latest.json 已寫出。")


if __name__ == "__main__":
    main()
