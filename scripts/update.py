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
import subprocess
import pandas as pd

SCRIPTS = ["fetch_calendar.py", "fetch_daily.py", "fetch_branch.py"]


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


def dataset_status(csv_glob: str, col: str, latest_day: str) -> dict:
    """
    掃一批 CSV,取每檔 col 欄的最大日期,回 {through, status}。
    through = 這批資料裡最舊的『最後日期』(木桶效應:最落後的那檔決定整體)。
    """
    files = glob.glob(csv_glob)
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


def write_latest() -> None:
    latest_day = last_trading_date()
    now_tpe = pd.Timestamp.now(tz="Asia/Taipei")

    # 讀 watchlist 拿追蹤清單
    import yaml
    with open("config/watchlist.yaml", encoding="utf-8") as f:
        tickers = [t["id"] for t in yaml.safe_load(f)["tickers"]]

    manifest = {
        "generated_at_utc": now_tpe.tz_convert("UTC").isoformat(),
        "generated_at_taipei": now_tpe.strftime("%Y-%m-%d %H:%M:%S"),
        "last_trading_date": latest_day,
        "datasets": {
            "price":  dataset_status("data/daily/*.csv", "close", latest_day),
            "inst":   dataset_status("data/daily/*.csv", "foreign_net_shares", latest_day),
            "margin": dataset_status("data/daily/*.csv", "margin_balance_shares", latest_day),
        },
        "tickers": tickers,
    }

    with open("data/latest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print("\n=== latest.json ===")
    for name, d in manifest["datasets"].items():
        flag = "✅" if d["status"] == "ok" else "⚠"
        print(f"  {flag} {name:8s} 截至 {d['through']}  ({d['status']})")
    print(f"  最新交易日:{latest_day}")


def main() -> None:
    for s in SCRIPTS:
        run_script(s)
    write_latest()
    print("\n✅ 更新完成。data/latest.json 已寫出。")


if __name__ == "__main__":
    main()
