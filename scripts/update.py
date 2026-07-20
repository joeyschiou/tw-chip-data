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

# 核心資料(價量/法人/融資/分點)—— 失敗就紅燈中止,不要靜默半套。
SCRIPTS = ["fetch_calendar.py", "fetch_universe.py", "fetch_daily.py", "fetch_branch.py"]

# 補充資料(基本資料/當沖/集保/流通/月營收)—— 各自 idempotent,依 cadence 內部 no-op。
# holders/float/revenue 走全市場 universe、週更/月更(nightly 大多 no-op);weekly-update.yml 另有主排。
# 失敗「不」中止核心管線(避免補充打嗝拖垮整晚),改用 latest.json 的 status 誠實反映落後。
# 順序:info→daytrade(需 daily 算比率)→holders→float(需 holders)→revenue。
EXTRA_SCRIPTS = ["fetch_info.py", "fetch_daytrade.py", "fetch_holders.py",
                 "fetch_float.py", "fetch_revenue.py",
                 # 策略資料層(便宜/增量的進 nightly;重 per-id 的 adj/short/pledge/shortsusp +
                 # CB daily/institutional 走 weekly-update.yml,不進這裡):
                 "fetch_macro.py",        # vix/維持率/期貨法人(日);景氣(月 no-op)
                 "fetch_regulatory.py",   # 處置/下市/產業鏈(全表 idempotent no-op)
                 "fetch_cb.py",           # 預設 info,overview(便宜)
                 "fetch_news.py"]         # watchlist-8,自增量(從已存最新日續)


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


def freshness_status(files: list, latest_day: str, tol_days: int = 0) -> dict:
    """
    補充資料的 through/status:through = 這批 watchlist 檔的『最新』日期(取 max)。
    daytrade 部分冷門股本就沒有每日當沖資料、集保/流通週更本就落後幾天,
    所以用 max(有抓到多新)而非 min(木桶),並容許 tol_days 的 cadence 落後。
    """
    files = [f for f in files if os.path.exists(f)]
    if not files:
        return {"through": None, "status": "missing"}
    dates = []
    for f in files:
        try:
            d = pd.read_csv(f, usecols=["date"], dtype=str)
        except Exception:
            continue
        if len(d):
            dates.append(str(d["date"].max()))
    if not dates:
        return {"through": None, "status": "missing"}
    through = max(dates)
    cutoff = (pd.to_datetime(latest_day) - pd.Timedelta(days=tol_days)).date().isoformat()
    return {"through": through, "status": "ok" if through >= cutoff else "lagging"}


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


def slice_coverage(subdir: str) -> int:
    """該 universe 資料集實際覆蓋幾檔(data/{subdir}/*.csv 檔數)。"""
    return len(glob.glob(f"data/{subdir}/*.csv"))


def revenue_status(files: list) -> dict:
    """月營收 through=watchlist 最新 revenue_month;status 對比「當月守衛」的期望月。"""
    files = [f for f in files if os.path.exists(f)]
    months, earliest = [], None
    for f in files:
        try:
            d = pd.read_csv(f, usecols=["revenue_month"], dtype=str)
        except Exception:
            continue
        if len(d):
            months.append(str(d["revenue_month"].max()))
            mn = str(d["revenue_month"].min())
            if earliest is None or mn < earliest:
                earliest = mn
    if not months:
        return {"through": None, "status": "missing"}
    through = max(months)
    from datetime import date as _date
    t = _date.today()
    cur = pd.Period(f"{t.year}-{t.month:02d}", freq="M")
    exp_p = (cur - 1) if t.day >= 11 else (cur - 2)     # 當月 >=11 日才有上月營收
    exp = f"{exp_p.year}-{exp_p.month:02d}"
    return {"through": through, "earliest": earliest,
            "status": "ok" if through >= exp else "lagging"}


def _through(path, col="date"):
    if not os.path.exists(path):
        return None
    try:
        d = pd.read_csv(path, usecols=[col], dtype=str)
        return str(d[col].max()) if len(d) else None
    except Exception:
        return None


def strategy_layer_status() -> dict:
    """策略資料層各 dataset 的 coverage/through/availability(0b 實測限制一併記錄)。"""
    def cov(sub):
        return len(glob.glob(f"data/{sub}/*.csv"))
    return {
        # per-id universe(還原價/借券/質押/停券)
        "daily_adj":        {"cadence": "weekly", "scope": "universe", "coverage": cov("daily_adj"),
                             "through": _through("data/daily_adj/2330.csv"), "note": "還原股價 2015+"},
        "short":            {"cadence": "weekly", "scope": "universe", "coverage": cov("short"),
                             "through": _through("data/short/2330.csv"), "note": "融券+借券餘額 2015+"},
        "pledge":           {"cadence": "weekly", "scope": "universe", "coverage": cov("pledge"),
                             "through": _through("data/pledge/2330.csv"), "note": "借貸擔保品 2015+"},
        "short_suspension": {"cadence": "weekly", "scope": "universe", "coverage": cov("short_suspension"),
                             "note": "停券/回補日 2015+"},
        # 市場級小表
        "business_indicator": {"cadence": "monthly", "through": _through("data/macro/business_indicator.csv"),
                               "note": "景氣對策信號 2010+"},
        "margin_maintenance": {"cadence": "daily", "through": _through("data/macro/margin_maintenance.csv"),
                               "note": "大盤融資維持率 2015+"},
        "futures_institutional": {"cadence": "daily", "through": _through("data/macro/futures_institutional.csv"),
                                  "note": "期貨三大法人 TX/MTX 2018+"},
        "vix":              {"cadence": "daily", "through": _through("data/macro/vix.csv"),
                             "status": "shallow", "note": "台指VIX 僅 2026-03 起(FinMind 深度限制)"},
        "disposition":      {"cadence": "daily", "through": _through("data/regulatory/disposition.csv"),
                             "note": "處置股 2005+"},
        "delisting":        {"cadence": "daily", "through": _through("data/delisting.csv"),
                             "note": "下市櫃 2001+;實測 TaiwanStockPrice 仍可抓下市股歷史→倖存者偏誤可修"},
        "industry_chain":   {"cadence": "daily", "coverage": len(pd.read_csv("data/industry_chain.csv", dtype=str))
                             if os.path.exists("data/industry_chain.csv") else 0, "note": "產業鏈快照"},
        # CB
        "cb_info":          {"cadence": "weekly", "coverage": len(pd.read_csv("data/cb/info.csv", dtype=str))
                             if os.path.exists("data/cb/info.csv") else 0, "note": "1800 CB;stock_id=cb_id[:4]"},
        "cb_daily":         {"cadence": "weekly", "coverage": cov("cb/daily")},
        "cb_institutional": {"cadence": "weekly", "coverage": cov("cb/institutional")},
        "news":             {"cadence": "daily", "scope": "watchlist", "coverage": cov("news"),
                             "status": "shallow", "note": "僅 watchlist-8;FinMind 深度約 2024+(2021 無)"},
    }


def branch_status(tickers, latest_day) -> dict:
    """
    分點 branch through = watchlist 各檔 max(date) 取 min(木桶)。
    status:through==最新交易日→ok;落後 1~3 個交易日→lagging;更久或無檔→missing。
    """
    maxes = []
    for sid in tickers:
        p = f"data/branch/{sid}.csv"
        if not os.path.exists(p):
            return {"through": None, "status": "missing", "gap_tdays": 999,
                    "cadence": "daily", "scope": "watchlist"}
        d = pd.read_csv(p, usecols=["date"], dtype=str)
        if not len(d):
            return {"through": None, "status": "missing", "gap_tdays": 999,
                    "cadence": "daily", "scope": "watchlist"}
        maxes.append(str(d["date"].max()))
    through = min(maxes)
    if through >= latest_day:
        status, gap = "ok", 0
    else:
        cal = pd.read_csv("data/calendar.csv", dtype=str)
        gap = len(cal[(cal["is_trading_day"].str.lower() == "true")
                      & (cal["date"] > through) & (cal["date"] <= latest_day)])
        status = "lagging" if 1 <= gap <= 3 else "missing"
    return {"through": through, "status": status, "gap_tdays": gap,
            "cadence": "daily", "scope": "watchlist"}


def write_latest() -> None:
    latest_day = last_trading_date()
    now_tpe = pd.Timestamp.now(tz="Asia/Taipei")

    # 讀 watchlist 拿追蹤清單
    import yaml
    with open("config/watchlist.yaml", encoding="utf-8") as f:
        tickers = [t["id"] for t in yaml.safe_load(f)["tickers"]]

    # canary 只掃 watchlist 的 daily 檔(避免被上千檔冷門股的落後日期拖累)
    wl_files = [f"data/daily/{sid}.csv" for sid in tickers]
    daytrade_files = [f"data/daytrade/{sid}.csv" for sid in tickers]
    holders_files = [f"data/holders/{sid}.csv" for sid in tickers]
    float_files = [f"data/float/{sid}.csv" for sid in tickers]

    info_count = len(pd.read_csv("data/info.csv", dtype=str)) if os.path.exists("data/info.csv") else 0
    uni = universe_report(latest_day)      # 內含 daily 的 earliest / coverage

    manifest = {
        "generated_at_utc": now_tpe.tz_convert("UTC").isoformat(),
        "generated_at_taipei": now_tpe.strftime("%Y-%m-%d %H:%M:%S"),
        "last_trading_date": latest_day,
        "datasets": {
            # 核心(watchlist canary:min = 最落後的那檔)
            # price 另附 earliest/coverage:回補深度(全 universe 最早日期)與覆蓋檔數
            "price":  {**dataset_status(wl_files, "close", latest_day),
                       "earliest": uni["earliest"], "coverage": uni["daily_files"],
                       "scope": "universe"},
            "inst":   dataset_status(wl_files, "foreign_net_shares", latest_day),
            "margin": dataset_status(wl_files, "margin_balance_shares", latest_day),
            "branch": branch_status(tickers, latest_day),
            # 補充(watchlist canary 定 through;coverage=universe 實際覆蓋檔數)
            "daytrade": {**freshness_status(daytrade_files, latest_day, tol_days=0),
                         "cadence": "daily", "scope": "universe",
                         "coverage": slice_coverage("daytrade")},
            "holders":  {**freshness_status(holders_files, latest_day, tol_days=10),
                         "cadence": "weekly", "scope": "universe",
                         "coverage": slice_coverage("holders")},
            "float":    {**freshness_status(float_files, latest_day, tol_days=10),
                         "cadence": "weekly", "scope": "universe",
                         "coverage": slice_coverage("float"),
                         "note": "locked=千張大戶 proxy(非董監)"},
            "revenue":  {**revenue_status([f"data/revenue/{sid}.csv" for sid in tickers]),
                         "cadence": "monthly", "scope": "universe",
                         "coverage": slice_coverage("revenue")},
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
