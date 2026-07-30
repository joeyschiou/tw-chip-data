"""
bench_kill_test.py — 機器 vs 大盤的存亡測試(Config B 機械選股是否有真 alpha)

用法:python scripts/bench_kill_test.py        # 從 repo 根目錄執行;唯讀,不碰 state.json / trades.csv
產出:data/screener/bench_kill_result.csv(逐事件)、data/screener/bench_kill_report.md(摘要)

問題:Config B(60日突破 + 營收YoY>=15% + regime gate + N=5 槽)的報酬,是選股 alpha,
      還是只是 regime-on 期間搭市場順風車?

凍結判定矩陣(執行前已鎖定,本程式不得因結果調整):
  T1 通過 = 兩者皆成立
    1. mean(r_i) >= mean(b_i) + 0.5pp
    2. geo(1+r_i) >= geo(1+b_i)
  未通過 → 機器降級為「regime-timing beta」,不得宣稱選股 alpha。

凍結選擇(執行前鎖定,寫入報告):
  - r_i 含 0.4425% 摩擦(回測原樣);b_i 不扣費。
  - 基準 = data/daily 原始收盤價建的全 universe 每日等權報酬指數;缺值/停牌當日剔除該股,不前向填補。
  - 同窗定義 = close(進場日) → close(出場日),共 hold_days=20 個基準日報酬。
    (機器實際是 open(進場日) → close(出場日);兩者差一個進場日盤中,無法完全對齊。
     另計 21 日版 close(進場日-1)→close(出場日) 當敏感度,不用於判定。)
  - 不剔除任何離群事件;仍未出場的事件標記 open 並排除於統計(缺資料,非篩選)。
"""
import os
import sys
import json
import hashlib
import argparse

import numpy as np
import pandas as pd
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import screener_core as sc            # noqa: E402
import screener as scr                # noqa: E402

OUT_CSV = "data/screener/bench_kill_result.csv"
OUT_MD = "data/screener/bench_kill_report.md"
FRICTION = scr.FEES                   # 0.004425(repo 既有回測常數)
BRANCH_ERA = "2021-06-30"
T1_ARITH_BAR = 0.005                  # +0.5pp
# 凍結回測 invariant。原始凍結值 25,003 / 480 出自 2026-07-22 快照;2026-07-30 使用者裁決
# 重新凍結為下列值 —— 因 daily_adj 深補改寫 + delisted 入 universe + market_index 重建
# 使 2015→今整條時間軸的事件集前移(原始 harness 交叉驗證同樣吐 25,578 / 494,非程式差異)。
INV_SIGNALS, INV_SIGNALS_TOL = 25578, 0.01     # 凍結回測 invariant(±1%)
INV_TRADES, INV_TRADES_TOL = 494, 0.02         # 凍結回測 invariant(±2%)


# ─────────────────── 事件集(與 research_backtest.py 同參數重建)───────────────────

def calendar():
    """交易日曆。data/calendar.csv 只含 2024-01-02 起(619 日),回測窗口跨 2015→今,
    故以 data/daily/2330.csv 的日期為準(= 回測 harness 用的同一份日曆);
    兩者在重疊區已驗證集合相同,見報告『日曆偏離』段。"""
    return sorted(pd.read_csv("data/daily/2330.csv", usecols=["date"], dtype=str)["date"].unique())


def build_signals(cfg):
    """回 (訊號總數, {date: [cand,...]});與 research_backtest.build_signals 同邏輯同參數。"""
    me, floors = cfg["main_engine"], cfg["main_engine"]["production_floors"]
    by_date, total = {}, 0
    for sid in sc.universe_ids():
        d = sc.clean_adj(sid)
        if d is None or len(d) < 88:
            continue
        yoy = sc.yoy_asof(sid, d["date"])
        sigs = sc.breakout_signals(d, yoy, lookback=me["breakout_lookback"], yoy_min=me["yoy_min"],
                                   spacing=me["spacing_days"], i_lo=65, i_hi_off=22)
        if not sigs:
            continue
        total += len(sigs)
        tm = pd.to_numeric(d["Trading_money"], errors="coerce").to_numpy() if "Trading_money" in d.columns else None
        rev = scr.revenue_pub(sid)
        first_bar = d["date"].iloc[0]
        closes = d["close"].to_numpy(dtype=float)
        for i in sigs:
            dt = str(d["date"].iloc[i])
            rs = scr.rank_score(sid, dt, first_bar, rev, cfg)
            close = closes[i]
            liq = float(np.median(tm[max(0, i - 19):i + 1])) if tm is not None else 0.0
            liq_ok = (close >= floors["min_price"]) and (liq >= floors["min_liquidity_20d_median_value"])
            by_date.setdefault(dt, []).append(
                {"sid": sid, "date": dt, "score": rs["score"],
                 "yoy": rs["yoy"] if rs["yoy"] is not None and np.isfinite(rs["yoy"]) else -9,
                 "close": close, "liq_ok": liq_ok})
    return total, by_date


def sim_events(cfg, by_date, cal):
    """N=5 兩階段 sim,逐筆記錄實際入選部位(= research_backtest.sim_trade_count 的同一迴圈,加記錄)。"""
    me = cfg["main_engine"]
    idx = sc.build_market_index()
    regime = dict(zip(idx["date"].astype(str), idx["regime"]))
    cal_idx = {d: i for i, d in enumerate(cal)}
    positions, pending, events = [], [], []
    for t in cal:
        it = cal_idx[t]
        held = {p["sid"] for p in positions}
        for c in pending:
            if c["sid"] in held:
                continue
            positions.append({"sid": c["sid"], "entry_i": it})
            held.add(c["sid"])
            events.append({"sid": c["sid"], "signal_date": c["date"], "entry_i": it,
                           "entry_date": t, "score": c["score"], "yoy": c["yoy"]})
        positions = [p for p in positions if it - p["entry_i"] < me["hold_days"]]
        pending = []
        if regime.get(t, False):
            held = {p["sid"] for p in positions}
            free = me["n_slots"] - len(positions)
            cands = sorted([c for c in by_date.get(t, []) if c["liq_ok"] and c["sid"] not in held],
                           key=lambda c: (-c["score"], -c["yoy"]))
            pending = cands[:max(0, free)]
    return events


# ─────────────────── 基準:全 universe 每日等權報酬指數 ───────────────────

def build_benchmark(cal, price_dir="data/daily", close_col="close"):
    """
    每日報酬 = 當日有效個股 close-to-close 報酬的簡單平均。
    有效 = 該股在 t 與 t-1(日曆上的前一交易日)都有 close>0;缺值/停牌當日剔除該股,不前向填補。
    回 (daily_ret: Series indexed by cal, n_valid: Series)。
    """
    cols = {}
    for sid in sc.universe_ids():
        p = f"{price_dir}/{sid}.csv"
        if not os.path.exists(p):
            continue
        try:
            r = pd.read_csv(p, dtype=str, usecols=["date", close_col])
        except ValueError:
            continue
        c = pd.to_numeric(r[close_col], errors="coerce")
        r = r.assign(_c=c)
        r = r[r["_c"] > 0].drop_duplicates("date", keep="last")
        cols[sid] = r.set_index("date")["_c"]
    M = pd.DataFrame(cols).reindex(cal)              # 缺值留 NaN,不 ffill
    R = M / M.shift(1) - 1                           # t 或 t-1 缺 → NaN → 該股當日自動剔除
    return R.mean(axis=1, skipna=True), R.notna().sum(axis=1)


def window_compound(daily: pd.Series, i0: int, i1: int) -> float:
    """複利 daily.iloc[i0..i1](含兩端);任一日 NaN 視為 0(全市場皆無報酬的日子不存在,僅防呆)。"""
    if i0 > i1 or i0 < 0 or i1 >= len(daily):
        return np.nan
    seg = daily.iloc[i0:i1 + 1].fillna(0.0).to_numpy(dtype=float)
    return float(np.prod(1.0 + seg) - 1.0)


# ─────────────────── 統計 ───────────────────

def geo_mean(x: np.ndarray) -> float:
    """幾何平均 (1+x) − 1;任一 (1+x)<=0 回 NaN(本測不剔除離群,故直接暴露)。"""
    v = 1.0 + np.asarray(x, dtype=float)
    if np.any(v <= 0):
        return np.nan
    return float(np.exp(np.mean(np.log(v))) - 1.0)


def stat_block(r, b):
    a = r - b
    return {"n": int(len(a)),
            "r_mean": float(np.mean(r)), "b_mean": float(np.mean(b)),
            "alpha_mean": float(np.mean(a)), "alpha_median": float(np.median(a)),
            "alpha_p5": float(np.percentile(a, 5)), "alpha_std": float(np.std(a, ddof=1)) if len(a) > 1 else np.nan,
            "win_rate": float(np.mean(a > 0)),
            "geo_r": geo_mean(r), "geo_b": geo_mean(b)}


def pct(x, nd=2):
    return "—" if x is None or not np.isfinite(x) else f"{x * 100:+.{nd}f}%"


def pp(x, nd=2):
    return "—" if x is None or not np.isfinite(x) else f"{x * 100:+.{nd}f}pp"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-sensitivity", action="store_true", help="略過 adj 基準/21日窗敏感度(較快)")
    args = ap.parse_args()

    cfg = yaml.safe_load(open("config/screener.yaml", encoding="utf-8"))
    yaml_raw = open("config/screener.yaml", "rb").read()
    param_hash = hashlib.sha256(
        yaml_raw + f"|friction={FRICTION}|hold={cfg['main_engine']['hold_days']}"
                   f"|n={cfg['main_engine']['n_slots']}|bench=daily_close_ew|win=E..X".encode()
    ).hexdigest()[:16]

    cal = calendar()
    print(f"日曆 {cal[0]} → {cal[-1]}({len(cal)} 交易日)")
    print("建訊號中(全 universe)…")
    total, by_date = build_signals(cfg)
    print(f"訊號總數 = {total}")
    print("跑 N=5 sim…")
    events = sim_events(cfg, by_date, cal)
    print(f"進場事件數 = {len(events)}")

    # ── 凍結回測一致性閘門 ──
    sig_dev = abs(total - INV_SIGNALS) / INV_SIGNALS
    trd_dev = abs(len(events) - INV_TRADES) / INV_TRADES
    sig_ok, trd_ok = sig_dev <= INV_SIGNALS_TOL, trd_dev <= INV_TRADES_TOL
    halt = not (sig_ok and trd_ok)
    print(f"[invariant] 訊號 {total} vs {INV_SIGNALS}(偏離 {sig_dev*100:.2f}%,容忍 ±{INV_SIGNALS_TOL*100:.0f}%)"
          f" → {'PASS' if sig_ok else 'FAIL'}")
    print(f"[invariant] 交易 {len(events)} vs {INV_TRADES}(偏離 {trd_dev*100:.2f}%,容忍 ±{INV_TRADES_TOL*100:.0f}%)"
          f" → {'PASS' if trd_ok else 'FAIL'}")
    if halt:
        print("⛔ HALT:重建事件清單與凍結回測 invariant 不一致 → 不下 T1 判定,證據照常落地供人工裁決。")

    # ── 逐事件報酬 ──
    print("算基準指數(data/daily 原始收盤)…")
    bench, bench_n = build_benchmark(cal)
    bench_alt = None
    if not args.no_sensitivity:
        print("算敏感度基準(data/daily_adj 還原收盤)…")
        bench_alt, _ = build_benchmark(cal, price_dir="data/daily_adj", close_col="close")

    hold = cfg["main_engine"]["hold_days"]
    px = {}
    rows = []
    for e in events:
        sid, ei = e["sid"], e["entry_i"]
        if sid not in px:
            d = sc.clean_adj(sid)
            px[sid] = (None if d is None else
                       d.drop_duplicates("date", keep="last").set_index("date")[["open", "close"]])
        p = px[sid]
        xi = ei + hold
        ed = cal[ei]
        xd = cal[xi] if xi < len(cal) else None
        o = float(p["open"].get(ed)) if p is not None and ed in p.index else np.nan
        c = float(p["close"].get(xd)) if (p is not None and xd is not None and xd in p.index) else np.nan
        r = (c / o - 1.0 - FRICTION) if (np.isfinite(o) and np.isfinite(c) and o > 0) else np.nan
        b = window_compound(bench, ei + 1, xi) if xd is not None else np.nan
        b21 = window_compound(bench, ei, xi) if xd is not None else np.nan
        badj = (window_compound(bench_alt, ei + 1, xi) if (bench_alt is not None and xd is not None) else np.nan)
        rows.append({"sid": sid, "signal_date": e["signal_date"], "entry_date": ed,
                     "exit_date": xd or "", "hold_days": hold,
                     "entry_open": o, "exit_close": c,
                     "r_i": r, "b_i": b, "alpha_i": r - b,
                     "b_i_21d": b21, "alpha_i_21d": r - b21,
                     "b_i_adj": badj, "alpha_i_adj": r - badj,
                     "bench_n_entry": int(bench_n.iloc[ei]) if ei < len(bench_n) else 0,
                     "score": e["score"], "yoy": e["yoy"],
                     "year": ed[:4], "branch_era": ed >= BRANCH_ERA,
                     "status": "closed" if np.isfinite(r) and xd else ("open" if xd is None else "missing_price")})
    df = pd.DataFrame(rows)
    os.makedirs("data/screener", exist_ok=True)
    df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(f"✅ 逐事件落地 {OUT_CSV}({len(df)} 列)")

    ok = df[(df["status"] == "closed") & df["b_i"].notna()].copy()
    dropped = len(df) - len(ok)
    r, b = ok["r_i"].to_numpy(), ok["b_i"].to_numpy()
    S = stat_block(r, b)

    # T1 判定
    c1 = S["alpha_mean"] >= T1_ARITH_BAR
    c2 = (np.isfinite(S["geo_r"]) and np.isfinite(S["geo_b"]) and S["geo_r"] >= S["geo_b"])
    verdict = c1 and c2

    # 切片
    years = []
    for y, g in ok.groupby("year"):
        years.append((y, stat_block(g["r_i"].to_numpy(), g["b_i"].to_numpy())))
    eras = []
    for lab, g in (("branch-era(>=2021-06-30)", ok[ok["branch_era"]]),
                   ("pre-branch-era", ok[~ok["branch_era"]])):
        if len(g):
            eras.append((lab, stat_block(g["r_i"].to_numpy(), g["b_i"].to_numpy())))

    # alpha vs b_i 對稱性
    med_b = float(np.median(b))
    hi, lo = ok[ok["b_i"] >= med_b], ok[ok["b_i"] < med_b]
    sym = {"med_b": med_b,
           "hi": stat_block(hi["r_i"].to_numpy(), hi["b_i"].to_numpy()),
           "lo": stat_block(lo["r_i"].to_numpy(), lo["b_i"].to_numpy()),
           "corr": float(np.corrcoef(b, ok["alpha_i"].to_numpy())[0, 1]),
           "beta": float(np.polyfit(b, ok["r_i"].to_numpy(), 1)[0])}
    terc = []
    if len(ok) >= 30:
        q = ok["b_i"].quantile([1 / 3, 2 / 3]).to_list()
        for lab, g in (("低基準 T1", ok[ok["b_i"] <= q[0]]),
                       ("中基準 T2", ok[(ok["b_i"] > q[0]) & (ok["b_i"] <= q[1])]),
                       ("高基準 T3", ok[ok["b_i"] > q[1]])):
            terc.append((lab, stat_block(g["r_i"].to_numpy(), g["b_i"].to_numpy())))

    # 敏感度(不用於判定)
    sens = {}
    for key, bcol in (("21日窗 close(E-1)->close(X)", "b_i_21d"), ("還原價基準 daily_adj", "b_i_adj")):
        g = ok[ok[bcol].notna()]
        if len(g):
            sens[key] = stat_block(g["r_i"].to_numpy(), g[bcol].to_numpy())

    # ── 報告 ──
    L = []
    L.append("# bench_kill_test — 機器 vs 大盤存亡測試\n")
    L.append(f"- 產生時間資料截止:{cal[-1]}(日曆 {cal[0]} → {cal[-1]},{len(cal)} 交易日)")
    L.append(f"- 參數 hash:`{param_hash}`(config/screener.yaml + friction + hold + N + 基準定義)")
    L.append(f"- 訊號總數:{total}(凍結 invariant {INV_SIGNALS} ±{INV_SIGNALS_TOL*100:.0f}% → "
             f"{'PASS' if sig_ok else 'FAIL'},偏離 {sig_dev*100:.2f}%)")
    L.append(f"- 進場事件數:{len(events)}(凍結 invariant {INV_TRADES} ±{INV_TRADES_TOL*100:.0f}% → "
             f"{'PASS' if trd_ok else 'FAIL'},偏離 {trd_dev*100:.2f}%)")
    L.append(f"- 納入統計:{len(ok)} 筆(排除 {dropped} 筆:仍未出場/缺價,非篩選)\n")

    L.append("## 判定")
    if halt:
        L.append("**⛔ HALT — 重建事件清單與凍結回測 invariant 不一致,T1 判定不生效。**")
        L.append("下列統計為證據,不構成結論;需人工裁決是否重新凍結 invariant 後再判。\n")
    L.append(f"- 條件1 每事件算術平均 alpha >= +0.50pp:{pp(S['alpha_mean'])} → "
             f"{'✅ 成立' if c1 else '❌ 不成立'}")
    L.append(f"- 條件2 幾何平均 (1+r) >= (1+b):{pct(S['geo_r'])} vs {pct(S['geo_b'])} → "
             f"{'✅ 成立' if c2 else '❌ 不成立'}")
    if not halt:
        L.append(f"\n**T1 {'通過 — 機器具備超越同窗大盤的選股 alpha。' if verdict else '未通過 — 機器降級為「regime-timing beta」,不得宣稱選股 alpha。'}**\n")
    else:
        L.append("")

    L.append("## 總表")
    L.append("| 指標 | 值 |")
    L.append("|---|---|")
    L.append(f"| 事件數 n | {S['n']} |")
    L.append(f"| 每事件平均報酬 r(含 {FRICTION*100:.4f}% 費) | {pct(S['r_mean'])} |")
    L.append(f"| 同窗基準平均 b(不扣費) | {pct(S['b_mean'])} |")
    L.append(f"| alpha 算術平均 | {pp(S['alpha_mean'])} |")
    L.append(f"| alpha 中位數 | {pp(S['alpha_median'])} |")
    L.append(f"| alpha P5 | {pp(S['alpha_p5'])} |")
    L.append(f"| alpha 標準差 | {pp(S['alpha_std'])} |")
    L.append(f"| 勝率(alpha>0) | {S['win_rate']*100:.1f}% |")
    L.append(f"| 幾何平均 (1+r) | {pct(S['geo_r'])} |")
    L.append(f"| 幾何平均 (1+b) | {pct(S['geo_b'])} |")
    L.append("")

    def slice_table(title, items):
        L.append(f"## {title}")
        L.append("| 切片 | n | r 平均 | b 平均 | alpha 平均 | alpha 中位 | 勝率 | geo r | geo b |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for lab, s in items:
            L.append(f"| {lab} | {s['n']} | {pct(s['r_mean'])} | {pct(s['b_mean'])} | {pp(s['alpha_mean'])} "
                     f"| {pp(s['alpha_median'])} | {s['win_rate']*100:.1f}% | {pct(s['geo_r'])} | {pct(s['geo_b'])} |")
        L.append("")

    slice_table("分年度", years)
    slice_table("branch-era 切片", eras)
    if terc:
        slice_table("alpha vs 基準報酬(三分位;檢驗「只在大漲時看似有效」)", terc)
    L.append(f"- 基準中位數 b = {pct(sym['med_b'])};高基準組 alpha 平均 {pp(sym['hi']['alpha_mean'])}"
             f"(n={sym['hi']['n']}),低基準組 {pp(sym['lo']['alpha_mean'])}(n={sym['lo']['n']})")
    L.append(f"- corr(b_i, alpha_i) = {sym['corr']:+.3f};r 對 b 的斜率(beta)= {sym['beta']:+.3f}\n")

    if sens:
        slice_table("敏感度(凍結選擇之外的替代口徑,不用於判定)",
                    [(k, v) for k, v in sens.items()])

    L.append("## 凍結選擇聲明")
    L.append(f"1. **r_i 含 {FRICTION*100:.4f}% 摩擦,b_i 不扣費**。此為執行前凍結選擇,對機器不利,未因結果調整。")
    L.append("2. **基準 = data/daily 原始收盤價的全 universe 等權指數**,缺值/停牌當日剔除該股、不前向填補。")
    L.append("3. **同窗 = close(進場日) → close(出場日)**,20 個基準日報酬,與 hold_days 對齊。"
             "機器實際窗為 open(進場日)→close(出場日),差一個進場日盤中;21 日版列為敏感度。")
    L.append("4. **不剔除離群事件**;仍未出場/缺價的事件標記後排除,屬缺資料而非篩選,數量已列出。")
    L.append("5. **日曆偏離**:data/calendar.csv 僅覆蓋 2024-01-02 起(619 日),不足以涵蓋 2015→今的回測窗,"
             "故採 data/daily/2330.csv 日期(= 回測 harness 同一份日曆);兩者在重疊區集合完全相同,已驗證。")
    L.append("")
    L.append("## 已知偏差(方向已標,未做修正)")
    L.append("- **r_i 用還原價、b_i 用原始價** → r 含股利、b 不含。台股年均現金殖利率約 3–4%,"
             "20 交易日窗約 0.25–0.35pp。此偏差**高估 alpha**,方向對機器有利。"
             "『還原價基準』敏感度列於上表,可讀出此項的量級。")
    L.append("- **基準成分取自 config/universe.csv(現存普通股)** → 含倖存者偏誤,已下市股不在基準內,"
             "**高估基準**、對機器不利,與上一項方向相反。")
    L.append(f"- **b_i 不扣費** → 低估基準的可實現報酬,**高估 alpha**(單邊往返約 {FRICTION*100:.2f}%)。")
    L.append("")

    open(OUT_MD, "w", encoding="utf-8").write("\n".join(L))
    print(f"✅ 摘要落地 {OUT_MD}")
    print("\n".join(L[L.index("## 判定"):]))

    json.dump({"param_hash": param_hash, "signals": total, "events": len(events),
               "analyzed": len(ok), "dropped": dropped, "halt": halt,
               "alpha_mean": S["alpha_mean"], "geo_r": S["geo_r"], "geo_b": S["geo_b"],
               "verdict": (None if halt else bool(verdict))},
              open("data/screener/bench_kill_meta.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    return 2 if halt else 0


if __name__ == "__main__":
    sys.exit(main())
