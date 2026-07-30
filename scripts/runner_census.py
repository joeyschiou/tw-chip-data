"""
runner_census.py — 連板事件盤點(Phase 0,不呼叫任何 API)

用法:python scripts/runner_census.py      # 從 repo 根目錄執行;純讀 data/daily/*.csv
產出:data/screener/runner_census.csv(逐事件)、data/screener/runner_census_report.md

目的:為 tw-chip-flow-audit 技能的 E 模式回測估算樣本量與分點回補成本。
**只用 repo 現有資料,不呼叫 FinMind,不回補任何分點。**

事件定義(凍結,本程式不得為了樣本量調整):
  - 連續 >=2 個交易日收漲停 = runner 確立;事件日 = 第 2 根漲停日
  - 漲停價 = 前收 × 1.10 後依 TWSE tick 表**無條件捨去**到檔位
    (<10:0.01 / 10-50:0.05 / 50-100:0.1 / 100-500:0.5 / 500-1000:1 / >1000:5)
  - 排除:各檔前 5 個交易日(上市初期無漲跌幅限制)、ETF、總資料不足 60 日者
  - 同一檔 30 個交易日內多次觸發只算第一次
  - 處置股(分盤交易)漲幅限制不變,仍照 10% 算

**v2 修正(2026-07-30)**:原定義寫「四捨五入到檔位」,經 v1 全母體(2,071 檔逐日)驗證為錯:
  - 四捨五入命中 35,220 個漲停日,其中 **74 筆漲幅 >10%** —— 10% 限制下不可能成立;
  - 無條件捨去命中 62,396 個漲停日,**0 筆 >10%**。
  TWSE 漲停價 = 不超過前收×1.1 的**最大**檔位價(捨去)。四捨五入向上時門檻高於真實漲停價,
  系統性漏認約一半的漲停日。v2 起 limit_up() 預設 floor,主結果採 floor;round 版留存對照。
"""
import os
import sys
import json
import math
import hashlib

import numpy as np
import pandas as pd

DAILY = "data/daily"
OUT_CSV = "data/screener/runner_census.csv"
OUT_MD = "data/screener/runner_census_report.md"
OUT_META = "data/screener/runner_census_meta.json"

MIN_ROWS = 60           # 資料不足 60 日者排除
SKIP_FIRST = 5          # 各檔前 5 個交易日排除(上市初期無漲跌幅限制)
DEDUP_TDAYS = 30        # 同一檔 30 個交易日內只算第一次
BRANCH_ERA = "2021-07-01"
WINDOWS = (30, 60, 120)         # 事件日前 N 個交易日(v2:成本試算改分桶,窗口收斂為 3 種)
POST = 20                       # 事件後 20 個交易日
QUOTA_PER_HR = 6000


def tick_of(p: float) -> float:
    if p < 10:    return 0.01
    if p < 50:    return 0.05
    if p < 100:   return 0.1
    if p < 500:   return 0.5
    if p < 1000:  return 1.0
    return 5.0


def limit_up(prev: float, mode: str = "floor") -> float:
    """漲停價。mode=floor 為 TWSE 實務(不超過前收×1.1 的最大檔位價);round 僅保留作 v1 對照。"""
    raw = prev * 1.10
    t = tick_of(raw)
    n = raw / t
    k = round(n) if mode == "round" else math.floor(n + 1e-9)
    return round(k * t, 2)


def markets():
    m = {}
    if os.path.exists("config/universe.csv"):
        u = pd.read_csv("config/universe.csv", dtype=str)
        m.update(dict(zip(u["id"], u["market"])))
    if os.path.exists("data/info.csv"):
        i = pd.read_csv("data/info.csv", dtype=str)
        for sid, t, nm in zip(i["stock_id"], i["type"], i["name"]):
            m.setdefault(sid, t)
    return m


def names():
    if not os.path.exists("data/info.csv"):
        return {}
    i = pd.read_csv("data/info.csv", dtype=str)
    return dict(zip(i["stock_id"], i["name"]))


def is_etf(sid: str, info_type: dict) -> bool:
    return info_type.get(sid) == "ETF" or sid.startswith("00")


def branch_dates(sid: str):
    """該檔已存在的分點 (date) 集合;data/branch(watchlist)∪ data/branch_research(研究回補)。"""
    out = set()
    for d in ("data/branch", "data/branch_research"):
        p = f"{d}/{sid}.csv"
        if os.path.exists(p):
            try:
                out |= set(pd.read_csv(p, dtype=str, usecols=["date"])["date"].unique())
            except Exception:
                pass
    return out


def main():
    mkt = markets()
    nm = names()
    info_type = {}
    if os.path.exists("data/info.csv"):
        i = pd.read_csv("data/info.csv", dtype=str)
        info_type = dict(zip(i["stock_id"], i["industry"]))
    wl_ids, res_ids = set(), set()
    if os.path.isdir("data/branch"):
        wl_ids = {f[:-4] for f in os.listdir("data/branch") if f.endswith(".csv")}
    if os.path.isdir("data/branch_research"):
        res_ids = {f[:-4] for f in os.listdir("data/branch_research")
                   if f.endswith(".csv") and not f.startswith("_")}

    files = sorted(f for f in os.listdir(DAILY) if f.endswith(".csv"))
    events, skipped = [], {"etf": 0, "short": 0, "nofile": 0}
    diag = {"round_days": 0, "floor_days": 0, "round_illegal": 0, "floor_illegal": 0, "round_only": 0}
    stock_dates = {}          # sid -> 該檔交易日 list(供回補窗口用)

    for f in files:
        sid = f[:-4]
        if is_etf(sid, info_type):
            skipped["etf"] += 1
            continue
        try:
            d = pd.read_csv(f"{DAILY}/{f}", dtype=str, usecols=["date", "close", "value_twd"])
        except Exception:
            skipped["nofile"] += 1
            continue
        d["c"] = pd.to_numeric(d["close"], errors="coerce")
        d["v"] = pd.to_numeric(d["value_twd"], errors="coerce")
        d = d[d["c"] > 0].drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
        if len(d) < MIN_ROWS:
            skipped["short"] += 1
            continue
        c = d["c"].to_numpy(dtype=float)
        v = d["v"].to_numpy(dtype=float)
        dates = d["date"].tolist()
        stock_dates[sid] = dates

        lim_r = np.zeros(len(c), dtype=bool)
        lim_f = np.zeros(len(c), dtype=bool)
        for i in range(1, len(c)):
            x = round(c[i], 2)
            lim_r[i] = abs(x - limit_up(c[i - 1], "round")) < 1e-6
            lim_f[i] = abs(x - limit_up(c[i - 1], "floor")) < 1e-6
            if lim_r[i]:
                diag["round_days"] += 1
                if x / c[i - 1] > 1.1000001:
                    diag["round_illegal"] += 1        # 漲幅 >10%,10% 限制下不可能(除權息除外)
            if lim_f[i]:
                diag["floor_days"] += 1
                if x / c[i - 1] > 1.1000001:
                    diag["floor_illegal"] += 1
            if lim_r[i] and not lim_f[i]:
                diag["round_only"] += 1               # 收在 floor 檔位之上 = 超過真實漲停價

        for mode, lim in (("round", lim_r), ("floor", lim_f)):
            last = -10 ** 9
            for i in range(max(SKIP_FIRST, 2), len(c)):
                if not (lim[i] and lim[i - 1]):
                    continue
                if lim[i - 2]:                      # 只取連板的第 2 根
                    continue
                if i - last < DEDUP_TDAYS:
                    continue
                last = i
                streak = 2
                j = i + 1
                while j < len(c) and lim[j]:
                    streak += 1; j += 1
                events.append({
                    "sid": sid, "name": nm.get(sid, ""), "market": mkt.get(sid, "?"),
                    "mode": mode, "event_date": dates[i], "first_limit_date": dates[i - 1],
                    "prev_close": round(c[i - 2], 4) if i >= 2 else np.nan,
                    "close": round(c[i], 4), "limit_price": limit_up(c[i - 1], mode),
                    "streak_len": streak,
                    "value_20d_median": float(np.nanmedian(v[max(0, i - 19):i + 1])),
                    "branch_era": dates[i] >= BRANCH_ERA,
                    "has_branch_watchlist": sid in wl_ids,
                    "has_branch_research": sid in res_ids,
                    "year": dates[i][:4], "event_idx": i})

    ev = pd.DataFrame(events)
    if ev.empty:
        print("❌ 沒有偵測到任何事件"); return 1

    main_ev = ev[ev["mode"] == "floor"].copy()      # v2 主結果(TWSE 實務捨去)
    alt_ev = ev[ev["mode"] == "round"].copy()       # v1 四捨五入版,留存對照

    # ── 成交金額分桶(股本 proxy;info.csv 無股本欄位)──
    def bucket(x):
        if not np.isfinite(x):      return "unknown"
        if x < 3e7:                 return "A <30M"
        if x < 1e8:                 return "B 30M-100M"
        if x < 5e8:                 return "C 100M-500M"
        if x < 2e9:                 return "D 500M-2B"
        return "E >=2B"
    for df in (main_ev, alt_ev):
        df["value_bucket"] = df["value_20d_median"].map(bucket)

    main_ev.drop(columns=["mode"]).to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    # ── 分點回補成本試算(branch-era 事件;v2 改為按成交金額分層)──
    era = main_ev[main_ev["branch_era"]].copy()
    era["grp"] = np.where(era["value_bucket"] == "A <30M", "A", "B-E")
    bd_cache = {}
    cost = {}
    for grp, g in [("A(<30M)", era[era["grp"] == "A"]),
                   ("B-E(>=30M)", era[era["grp"] == "B-E"]),
                   ("ALL", era)]:
        for W in WINDOWS:
            need = set()
            for sid, i in zip(g["sid"], g["event_idx"]):
                ds = stock_dates[sid]
                i = int(i)
                for k in range(max(0, i - W), min(len(ds), i + POST + 1)):
                    need.add((sid, ds[k]))
            sids = {s for s, _ in need}
            for s in sids:
                if s not in bd_cache:
                    bd_cache[s] = branch_dates(s)
            have = {(s, dt) for s in sids for dt in bd_cache[s]}
            todo = need - have
            cost[f"{grp}|{W}"] = {"group": grp, "window": f"D-{W} ~ D+{POST}",
                                  "events": int(len(g)), "distinct_stocks": len(sids),
                                  "stock_days_total": len(need), "already_have": len(need & have),
                                  "todo_calls": len(todo), "hours_at_quota": len(todo) / QUOTA_PER_HR}

    param_hash = hashlib.sha256(
        f"v2|streak>=2|tick=floor|skipfirst={SKIP_FIRST}|minrows={MIN_ROWS}|dedup={DEDUP_TDAYS}"
        f"|era={BRANCH_ERA}|win={WINDOWS}|post={POST}|cost_split=A/B-E@30M".encode()).hexdigest()[:16]

    # ── 報告 ──
    L = ["# runner_census v2 — 連板事件盤點(Phase 0,零 API)\n"]
    L.append("> **v2 修正(2026-07-30)**:漲停檔位捨入方向由「四捨五入」改為 TWSE 實務的"
             "**無條件捨去**。修正依據:v1 全母體驗證顯示四捨五入命中的 35,220 個漲停日中有 "
             "**74 筆漲幅 >10%**(10% 限制下不可能),捨去版 62,396 日則為 **0 筆**。"
             "主結果自本版起採 floor,round 版留存對照。\n")
    L.append(f"- 掃描 {len(files)} 個 data/daily 檔;排除 ETF {skipped['etf']}、"
             f"資料不足 {MIN_ROWS} 日 {skipped['short']}、讀取失敗 {skipped['nofile']}")
    L.append(f"- 納入分析 {len(stock_dates)} 檔;參數 hash `{param_hash}`")
    L.append(f"- 資料期間:{min(min(v) for v in stock_dates.values())} → "
             f"{max(max(v) for v in stock_dates.values())}\n")

    L.append("## 1. 事件總數")
    L.append("| 定義 | 全樣本期 | branch-era(>=2021-07-01) | 佔比 |")
    L.append("|---|---|---|---|")
    for lab, df in (("**v2 主結果(無條件捨去)**", main_ev), ("v1 對照(四捨五入,已知錯誤)", alt_ev)):
        n, ne = len(df), int(df["branch_era"].sum())
        L.append(f"| {lab} | {n} | {ne} | {ne/n*100:.1f}% |")
    L.append("")
    L.append(f"> 修正使事件數 {len(alt_ev)} → {len(main_ev)}({(len(main_ev)-len(alt_ev))/max(len(alt_ev),1)*100:+.1f}%)。"
             "以下所有統計均以 v2(捨去)為準。\n")
    L.append("### 捨入方向的母體證據(全 2,071 檔逐日)")
    L.append("| 判定規則 | 命中「收盤 == 漲停價」的日數 | 其中漲幅 >10%(10% 限制下不可能) |")
    L.append("|---|---|---|")
    L.append(f"| 四捨五入(v1,已棄用) | {diag['round_days']:,} | {diag['round_illegal']:,} |")
    L.append(f"| **無條件捨去(v2 採用)** | {diag['floor_days']:,} | {diag['floor_illegal']:,} |")
    L.append("")
    L.append(f"- 捨去規則多認出 **{diag['floor_days'] - diag['round_days']:,}** 個漲停日,且無一日漲幅超過 10%。")
    L.append(f"- 反向只有 **{diag['round_only']:,}** 日「命中四捨五入但不命中捨去」——"
             "這些收盤價高於捨去檔位,在 10% 限制下不可能是漲停(多為除權息日前收基準不同所致)。\n")

    L.append("## 2. 分年度 × 上市/上櫃")
    piv = main_ev.pivot_table(index="year", columns="market", values="sid",
                              aggfunc="count", fill_value=0)
    cols = list(piv.columns)
    L.append("| 年 | " + " | ".join(cols) + " | 合計 |")
    L.append("|---|" + "---|" * (len(cols) + 1))
    for y, row in piv.iterrows():
        L.append(f"| {y} | " + " | ".join(str(int(row[c])) for c in cols) + f" | {int(row.sum())} |")
    L.append("| **合計** | " + " | ".join(str(int(piv[c].sum())) for c in cols)
             + f" | **{len(main_ev)}** |")
    L.append("")

    L.append("## 3. 事件股規模分布(20 日均成交金額分桶)")
    L.append("> data/info.csv 無股本/股數欄位,流通市值 proxy 不可得 → 依指示改用 20 日均成交金額分桶。")
    L.append("")
    L.append("| 桶 | 全樣本 | branch-era | 全樣本占比 |")
    L.append("|---|---|---|---|")
    for b in ("A <30M", "B 30M-100M", "C 100M-500M", "D 500M-2B", "E >=2B", "unknown"):
        n = int((main_ev["value_bucket"] == b).sum())
        ne = int(((main_ev["value_bucket"] == b) & main_ev["branch_era"]).sum())
        if n:
            L.append(f"| {b} | {n} | {ne} | {n/len(main_ev)*100:.1f}% |")
    L.append("")
    L.append(f"- 連板長度分布:" + ",".join(
        f"{k} 板 {int(v)} 次" for k, v in main_ev["streak_len"].value_counts().sort_index().items()))
    L.append("")

    L.append("## 4. 分點回補成本試算(branch-era 事件,按成交金額分層)")
    L.append(f"- 對象:{len(era)} 筆 branch-era 事件,涉及 {era['sid'].nunique()} 檔股票")
    L.append(f"- 分層:**A = 20 日均成交金額 <30M**(過不了生產 floors 的流動性門檻)、"
             f"**B–E = >=30M**(可執行);1 stock-day = 1 API call;配額 {QUOTA_PER_HR} req/hr")
    L.append("")
    L.append("| 分層 | 事件數 | 窗口 | 涉及股票 | stock-day(去重疊) | 已有分點 | 待抓 = API calls | 執行時數 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for grp in ("A(<30M)", "B-E(>=30M)", "ALL"):
        for W in WINDOWS:
            c = cost[f"{grp}|{W}"]
            bold = "**" if grp == "B-E(>=30M)" else ""
            L.append(f"| {bold}{grp}{bold} | {c['events']} | {c['window']} | {c['distinct_stocks']} "
                     f"| {c['stock_days_total']:,} | {c['already_have']:,} "
                     f"| {bold}{c['todo_calls']:,}{bold} | {bold}{c['hours_at_quota']:.1f} hr{bold} |")
    L.append("")
    L.append("> stock-day 已在同一檔內跨事件去重疊。「已有分點」= data/branch(watchlist)∪ "
             "data/branch_research(研究回補)實際已存在的 (檔,日)。"
             "A 與 B–E 的 stock-day 可能有少量重疊(同一檔在不同事件落入不同桶),"
             "故兩層相加會略大於 ALL 列。\n")

    L.append("## 5. 已有分點資料的事件(免回補)")
    n_wl = int(era["has_branch_watchlist"].sum())
    n_res = int(era["has_branch_research"].sum())
    n_any = int((era["has_branch_watchlist"] | era["has_branch_research"]).sum())
    L.append(f"- branch-era 事件 {len(era)} 筆中:")
    L.append(f"  - 股票已在 **watchlist**(data/branch)有分點檔:**{n_wl}** 筆({n_wl/max(len(era),1)*100:.1f}%)")
    L.append(f"  - 股票在 data/branch_research 有分點檔:{n_res} 筆({n_res/max(len(era),1)*100:.1f}%)")
    L.append(f"  - 兩者任一:{n_any} 筆({n_any/max(len(era),1)*100:.1f}%)")
    L.append("- 注意:有檔案 ≠ 該事件窗口日期都齊。第 4 節的「已有分點」是逐 (檔,日) 比對的實數,"
             "才是真正免抓的量;本節是「檔案層級」的粗覆蓋率。\n")

    L.append("## 6. B–E 桶(>=30M,可執行標的)事件輪廓")
    be = main_ev[main_ev["value_bucket"] != "A <30M"]
    be_era = be[be["branch_era"]]
    L.append(f"- 全樣本 {len(be)} 筆(佔全部 {len(be)/len(main_ev)*100:.1f}%);"
             f"branch-era {len(be_era)} 筆(佔 branch-era {len(be_era)/max(len(era),1)*100:.1f}%)")
    L.append("")
    L.append("**連板長度分布**")
    L.append("| 連板數 | 全樣本 | branch-era | 全樣本占比 |")
    L.append("|---|---|---|---|")
    for k in sorted(be["streak_len"].unique()):
        n = int((be["streak_len"] == k).sum())
        ne = int((be_era["streak_len"] == k).sum())
        L.append(f"| {k} 板 | {n} | {ne} | {n/len(be)*100:.1f}% |")
    L.append("")
    L.append("**年度分布(上市/上櫃)**")
    pb = be.pivot_table(index="year", columns="market", values="sid", aggfunc="count", fill_value=0)
    bcols = list(pb.columns)
    L.append("| 年 | " + " | ".join(bcols) + " | 合計 |")
    L.append("|---|" + "---|" * (len(bcols) + 1))
    for y, row in pb.iterrows():
        L.append(f"| {y} | " + " | ".join(str(int(row[c])) for c in bcols) + f" | {int(row.sum())} |")
    L.append("| **合計** | " + " | ".join(str(int(pb[c].sum())) for c in bcols) + f" | **{len(be)}** |")
    L.append("")

    L.append("## 凍結定義與已知偏差")
    L.append(f"1. 連續 >=2 日收漲停,事件日 = 第 2 根;同檔 {DEDUP_TDAYS} **個交易日**內只算第一次"
             "(『30 日』以交易日解讀,與 repo 其他 spacing 慣例一致)。")
    L.append(f"2. 排除各檔前 {SKIP_FIRST} 個交易日(上市初期無漲跌幅限制)。注意:2015-01-05 之前上市的股票,"
             "其檔案首日不是上市日,此排除對它們無實質作用,僅對期間內新上市股生效。")
    L.append(f"3. 排除 ETF(info.csv industry=ETF 或代號 00 開頭)與總資料不足 {MIN_ROWS} 日者。")
    L.append("4. 處置股(分盤交易)漲幅限制不變,照 10% 算 —— repo 無處置名單(latest.json 記為 unavailable),"
             "**無法辨識也未辨識**處置期間;若日後接上處置來源可回頭核對。")
    L.append("5. 漲停判定用 data/daily 原始收盤價(非還原價),方向正確;除權息日的 close 與前收不同基準,"
             "可能產生極少數偽漲停/漏漲停,未做校正。")
    L.append("6. **倖存者偏誤**:data/daily 只含現存 universe ∪ watchlist,已下市股不在內,事件數為低估。")
    L.append("")

    open(OUT_MD, "w", encoding="utf-8").write("\n".join(L))
    json.dump({"version": 2, "tick_mode": "floor", "corrected_on": "2026-07-30",
               "param_hash": param_hash, "files_scanned": len(files), "stocks_analyzed": len(stock_dates),
               "skipped": skipped, "events_main_floor": len(main_ev),
               "events_alt_round_v1": len(alt_ev), "events_branch_era": len(era), "tick_diag": diag,
               "events_BE_all": int(len(be)), "events_BE_branch_era": int(len(be_era)),
               "cost": {str(k): v for k, v in cost.items()},
               "already_watchlist": n_wl, "already_research": n_res, "already_any": n_any},
              open(OUT_META, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print("\n".join(L))
    print(f"\n✅ {OUT_CSV}({len(main_ev)} 列) / {OUT_MD} / {OUT_META}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
