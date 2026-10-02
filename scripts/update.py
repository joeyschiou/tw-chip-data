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

# 核心資料(價量/法人/融資)—— 失敗就紅燈中止,不要靜默半套。
SCRIPTS = ["fetch_calendar.py", "fetch_universe.py", "fetch_daily.py"]

# FinMind 免費帳號(register,600 call/hr)後已停抓(實測 400 "Your level is register"
# 或全市場單 call 需付費;per-id 全 universe 又遠超額度):
#   分點 branch、當沖 daytrade、集保 holders、流通 float(依賴 holders)、月營收 revenue、
#   還原價/借券/質押/停券(fetch_stockseries)、CB(fetch_cb)、news、
#   景氣/維持率(fetch_macro)、處置(fetch_regulatory)。
# data/ 下這些目錄保留為歷史(screener 等仍可讀),但不再更新。

# 補充資料 —— 各自 idempotent;失敗「不」中止核心管線,改用 latest.json 的 status 反映落後。
EXTRA_SCRIPTS = ["fetch_info.py",
                 "fetch_macro.py",        # vix/期貨法人(日)
                 "fetch_regulatory.py",   # 下市/產業鏈(全表 idempotent no-op)
                 # 借券/放空法規快照:**前瞻累積**。歷史標借費率買不到
                 # (TWSE 各路徑 404、OpenAPI 僅當日股數、FinMind 無 dataset),
                 # 所以從今天開始自己長。單日 2 個 call,極便宜,idempotent。
                 "fetch_sbl_snapshot.py"]


def run_script(name: str) -> None:
    """跑一支子腳本,失敗就中斷整個更新(寧可紅燈也不要靜默半套)。"""
    path = os.path.join("scripts", name)
    print(f"\n=== 執行 {name} ===")
    result = subprocess.run([sys.executable, path])
    if result.returncode != 0:
        sys.exit(f"❌ {name} 失敗(exit {result.returncode}),中止更新。")


def run_script_soft(name: str) -> bool:
    """跑一支補充腳本,失敗只警告不中止(落後會反映在 latest.json status)。"""
    path = os.path.join("scripts", name)
    print(f"\n=== 執行 {name}(補充)===")
    result = subprocess.run([sys.executable, path])
    if result.returncode != 0:
        print(f"⚠ {name} 失敗(exit {result.returncode}),補充資料略過,不中止核心管線。")
        return False
    return True


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
    earliest = None
    for f in daily_files:
        try:
            d = pd.read_csv(f, usecols=["date"], dtype=str)
        except Exception:
            continue
        if not len(d):
            continue
        if str(d["date"].max()) >= latest_day:
            current += 1
        mn = str(d["date"].min())
        if earliest is None or mn < earliest:
            earliest = mn      # 全 universe 最早日期(= 回補深度下限;個股仍以上市日為準)
    return {"count": count, "daily_files": len(daily_files), "current": current,
            "earliest": earliest}


def _through(path, col="date"):
    if not os.path.exists(path):
        return None
    try:
        d = pd.read_csv(path, usecols=[col], dtype=str)
        return str(d[col].max()) if len(d) else None
    except Exception:
        return None


def strategy_layer_status() -> dict:
    """策略資料層各 dataset 的 coverage/through/availability(0b 實測限制一併記錄)。
    免費帳號後停抓的(還原價/借券/質押/停券/景氣/維持率/處置/CB/news)已移除,舊檔留在 data/ 當歷史。"""
    def cov(sub):
        return len(glob.glob(f"data/{sub}/*.csv"))
    return {
        # 免費帳號仍可抓(nightly)
        "futures_institutional": {"cadence": "daily", "through": _through("data/macro/futures_institutional.csv"),
                                  "note": "期貨三大法人 TX/MTX 2018+"},
        "vix":              {"cadence": "daily", "through": _through("data/macro/vix.csv"),
                             "status": "shallow", "note": "台指VIX 僅 2026-03 起(FinMind 深度限制)"},
        "delisting":        {"cadence": "daily", "through": _through("data/delisting.csv"),
                             "note": "下市櫃 2001+;實測 TaiwanStockPrice 仍可抓下市股歷史→倖存者偏誤可修"},
        "industry_chain":   {"cadence": "daily", "coverage": len(pd.read_csv("data/industry_chain.csv", dtype=str))
                             if os.path.exists("data/industry_chain.csv") else 0, "note": "產業鏈快照"},
    }


def write_latest() -> None:
    latest_day = last_trading_date()
    now_tpe = pd.Timestamp.now(tz="Asia/Taipei")

    # 讀 watchlist 拿追蹤清單
    import yaml
    with open("config/watchlist.yaml", encoding="utf-8") as f:
        tickers = [t["id"] for t in yaml.safe_load(f)["tickers"]]

    # canary 只掃 watchlist 的 daily 檔(避免被上千檔冷門股的落後日期拖累)
    wl_files = [f"data/daily/{sid}.csv" for sid in tickers]

    info_count = len(pd.read_csv("data/info.csv", dtype=str)) if os.path.exists("data/info.csv") else 0
    uni = universe_report(latest_day)      # 內含 daily 的 earliest / coverage

    manifest = {
        "generated_at_utc": now_tpe.tz_convert("UTC").isoformat(),
        "generated_at_taipei": now_tpe.strftime("%Y-%m-%d %H:%M:%S"),
        "last_trading_date": latest_day,
        "datasets": {
            # 核心(watchlist canary:min = 最落後的那檔)。
            # 分點/當沖/集保/流通/月營收:FinMind 免費帳號抓不到,已停抓、不列入。
            # price 另附 earliest/coverage:回補深度(全 universe 最早日期)與覆蓋檔數
            "price":  {**dataset_status(wl_files, "close", latest_day),
                       "earliest": uni["earliest"], "coverage": uni["daily_files"],
                       "scope": "universe"},
            "inst":   dataset_status(wl_files, "foreign_net_shares", latest_day),
            "margin": dataset_status(wl_files, "margin_balance_shares", latest_day),
        },
        "reference": {
            "info": {"count": info_count, "status": "ok" if info_count else "missing"},
            # 已知缺口:FinMind 無 注意/處置 dataset(已實測),下游此層需另接來源。
            "disposition": {"status": "unavailable",
                            "note": "FinMind 無 注意/處置 dataset;下游需另接 TWSE/櫃買來源,本管線保持 FinMind-only"},
        },
        "universe": uni,
        "strategy_layer": strategy_layer_status(),
        "tickers": tickers,
    }

    with open("data/latest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print("\n=== latest.json ===")
    for name, d in manifest["datasets"].items():
        flag = "✅" if d["status"] == "ok" else "⚠"
        cad = f"  [{d['cadence']}]" if "cadence" in d else "  [watchlist canary]"
        print(f"  {flag} {name:8s} 截至 {d['through']}  ({d['status']}){cad}")
    r = manifest["reference"]
    print(f"  info:{r['info']['count']} 檔  |  注意處置:{r['disposition']['status']}(FinMind 缺,記為缺口)")
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
            run_script(s)          # 核心:失敗即中止
        for s in EXTRA_SCRIPTS:
            run_script_soft(s)     # 補充:失敗只警告,cadence 由各腳本內部 no-op
    write_latest()
    print("\n✅ 更新完成。data/latest.json 已寫出。")


if __name__ == "__main__":
    main()
