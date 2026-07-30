"""
regime_only_test.py — 擇時層價值測試(regime gate 本身有沒有價值?)

用法:python scripts/regime_only_test.py     # 從 repo 根目錄執行;唯讀,不碰 state.json / trades.csv
產出:data/screener/regime_only_result.csv(逐日)、regime_only_report.md、regime_only_meta.json

前提:bench_kill_test 已判定 Config B 選股層無 alpha(T1 FAIL,重凍結 25,578 / 494 事件集)。
本測回答:如果「開關 + 買大盤」就能達到機器績效,選股層應整層退役;而 gate 本身又是否勝過什麼都不做。

三個策略(同一區間、同一交易日曆):
  S1  機器      Config B 既有回測輸出(不重算,直接引用凍結數字)
  S2  擇時基準  regime-on 全倉 0050(還原價),regime-off 全數現金
  S3  純 beta   0050 買進持有不動
  S2b 理論上限  開關 + 等權指數本身(不可投資,不參與判定)

凍結判定(執行前鎖定,禁止事後修改):
  J1 選股層去留:S2 幾何 CAGR >= S1 中位數 CAGR − 0.5pp → 選股層退役(平手歸簡單方案)
  J2 擇時層去留:S2 相對 S3 須同時 (a) MDD 改善 >= 5pp 且 (b) 幾何 CAGR >= S3 − 1.0pp
                否則 regime gate 亦無價值,結論退化為「直接持有 0050」

凍結選擇(寫入報告):
  - 費用:S2 每次開關來回 0.1425%×2 + ETF 證交稅 0.1% = 0.385%;S3 只計最初買進 0.1425%;
    S1 維持原回測 0.4425% 不動。現金部位不計利息。
  - 訊號時序:第 d 日的持倉由「前一交易日收盤」的 regime 決定(無前視);
    日報酬 = 0050 還原收盤 close-to-close。
  - regime 訊號直接讀現行 data/screener/market_index.csv 的 regime 欄,不另算、不套 raw 延伸。
  - 錯配 flag:訊號是等權指數、標的是市值加權 0050;此錯配照做,S2b 為其理論上限參考。
"""
import os
import sys
import json
import hashlib

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MKT = "data/screener/market_index.csv"
ETF = "data/daily_adj/0050.csv"
OUT_CSV = "data/screener/regime_only_result.csv"
OUT_MD = "data/screener/regime_only_report.md"
OUT_META = "data/screener/regime_only_meta.json"

COST_SWITCH = 0.001425 * 2 + 0.001      # 0.385% 每次開關來回(手續費雙邊 + ETF 證交稅)
COST_BUY = 0.001425                     # S3 最初買進
TRADING_DAYS = 252

# S1:Config B 既有回測凍結輸出(docs/research/score_v1_report.md §θ 網格 θ*=0 列;
# 來源 data/branch_research/_phase1_theta.json,seed=20260721、nseed=20、多 seed 中位數路徑)。
S1 = {"cagr_med": 11.841, "cagr_min": 1.935, "cagr_max": 16.38,
      "mdd_med": 40.353, "mdd_min": 30.787, "mdd_max": 60.441,
      "trades_total_med": 490.0, "per_trade_mean_pp": 1.613, "win_rate_pct": 47.45,
      "seed": 20260721, "nseed": 20,
      "source": "data/branch_research/_phase1_theta.json (θ*=0);vintage=2026-07-22 事件集"}

J1_MARGIN = 0.5      # pp
J2_MDD_MIN = 5.0     # pp
J2_CAGR_MARGIN = 1.0  # pp


def load_regime():
    d = pd.read_csv(MKT, dtype={"date": str})
    d["regime"] = d["regime"].astype(str).str.lower().isin(("true", "1"))
    d["mkt_ret"] = np.exp(pd.to_numeric(d["mkt_logret"], errors="coerce")) - 1.0   # 等權指數日簡單報酬
    return d[["date", "regime", "mkt_ret"]]


def load_etf():
    d = pd.read_csv(ETF, dtype=str)
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    d = d[d["close"] > 0].drop_duplicates("date", keep="last").sort_values("date")
    return d[["date", "close"]].reset_index(drop=True)


def run_switch(ret, pos, cost_switch):
    """
    pos[i]=第 i 日是否在場(已含時序位移)。
    回 (equity_curve, n_switch, cost_drag, gross_curve):
      cost_drag = 1 − Π(1−c) 的複利拖累(不是每次費用的算術加總);
      gross_curve = 同一開關序列但零成本(用來把「gate 本身」與「開關成本」拆開)。
    """
    eq, gross, prev, curve, gcurve, mult = 1.0, 1.0, False, [], [], 1.0
    n_sw = 0
    for r, p in zip(ret, pos):
        if p != prev:                      # 開關發生在該日進場/出場,費用一次計入
            eq *= (1.0 - cost_switch)
            mult *= (1.0 - cost_switch)
            n_sw += 1
            prev = p
        if p:
            eq *= (1.0 + r)
            gross *= (1.0 + r)
        curve.append(eq)
        gcurve.append(gross)
    return np.array(curve), n_sw, (1.0 - mult), np.array(gcurve)


def mdd(curve):
    peak = np.maximum.accumulate(curve)
    return float(np.max((peak - curve) / peak) * 100)


def cagr(curve, years):
    if curve[-1] <= 0 or years <= 0:
        return np.nan
    return float((curve[-1] ** (1.0 / years) - 1.0) * 100)


def stats(curve, ret_used, years, n_sw, cost, in_mkt):
    c = cagr(curve, years)
    m = mdd(curve)
    vol = float(np.std(ret_used, ddof=1) * np.sqrt(TRADING_DAYS) * 100)
    return {"cagr": c, "mdd": m, "vol": vol,
            "calmar": (c / m if m > 0 else np.nan),
            "in_market": in_mkt * 100, "switches": n_sw, "cost_drag": cost * 100,
            "final": float(curve[-1])}


def annual(dates, curve):
    """每年報酬 = 年末 equity / 前一年末 equity − 1。"""
    s = pd.Series(curve, index=pd.Index(dates, name="date"))
    yr = pd.Series(list(dates)).str[:4].to_numpy()
    out, prev = {}, 1.0
    for y in sorted(set(yr)):
        last = s.to_numpy()[yr == y][-1]
        out[y] = last / prev - 1.0
        prev = last
    return out


def fmt(x, nd=2, unit="%"):
    return "—" if x is None or not np.isfinite(x) else f"{x:+.{nd}f}{unit}"


def main():
    reg, etf = load_regime(), load_etf()
    m = etf.merge(reg, on="date", how="inner").sort_values("date").reset_index(drop=True)
    if m.empty:
        print("❌ 無共同交易日"); return 1

    # 時序:第 d 日持倉由前一交易日收盤的 regime 決定(無前視)。第一日無前訊號 → 空手。
    m["pos"] = m["regime"].shift(1).fillna(False).astype(bool)
    m["r_etf"] = m["close"] / m["close"].shift(1) - 1.0
    m = m.iloc[1:].reset_index(drop=True)                # 丟掉沒有前一日報酬的首列
    m["r_etf"] = m["r_etf"].fillna(0.0)
    m["r_ew"] = m["mkt_ret"].fillna(0.0)

    dates = m["date"].tolist()
    years = (pd.Timestamp(dates[-1]) - pd.Timestamp(dates[0])).days / 365.25
    r_etf, r_ew, pos = m["r_etf"].to_numpy(), m["r_ew"].to_numpy(), m["pos"].to_numpy()

    # S2:開關 0050
    c2, sw2, cost2, g2 = run_switch(r_etf, pos, COST_SWITCH)
    # S3:買進持有 0050(僅最初買進費)
    c3 = (1.0 - COST_BUY) * np.cumprod(1.0 + r_etf)
    # S2b:開關 + 等權指數(不可投資,不參與判定)
    c2b, sw2b, cost2b, g2b = run_switch(r_ew, pos, COST_SWITCH)

    in_mkt = float(np.mean(pos))
    S2 = stats(c2, r_etf[pos], years, sw2, cost2, in_mkt)
    S3 = stats(c3, r_etf, years, 1, COST_BUY, 1.0)
    S2b = stats(c2b, r_ew[pos], years, sw2b, cost2b, in_mkt)
    # 診斷用(不參與判定,費用假設未變):同一開關序列的零成本版本
    S2_gross = stats(g2, r_etf[pos], years, sw2, 0.0, in_mkt)
    S2b_gross = stats(g2b, r_ew[pos], years, sw2b, 0.0, in_mkt)

    # ── 凍結判定 ──
    j1 = S2["cagr"] >= S1["cagr_med"] - J1_MARGIN
    mdd_gain = S3["mdd"] - S2["mdd"]
    j2a = mdd_gain >= J2_MDD_MIN
    j2b = S2["cagr"] >= S3["cagr"] - J2_CAGR_MARGIN
    j2 = j2a and j2b

    # ── regime-off 期間大盤報酬分布 ──
    off, on = ~pos, pos
    def dist(mask, r):
        x = r[mask]
        return {"n": int(mask.sum()),
                "mean_pp": float(np.mean(x) * 100) if len(x) else np.nan,
                "median_pp": float(np.median(x) * 100) if len(x) else np.nan,
                "pos_pct": float(np.mean(x > 0) * 100) if len(x) else np.nan,
                "compound": float(np.prod(1 + x) - 1) * 100 if len(x) else np.nan,
                "worst_pp": float(np.min(x) * 100) if len(x) else np.nan,
                "best_pp": float(np.max(x) * 100) if len(x) else np.nan}
    d_off_etf, d_on_etf = dist(off, r_etf), dist(on, r_etf)
    d_off_ew, d_on_ew = dist(off, r_ew), dist(on, r_ew)

    # ── 逐日落地 ──
    out = pd.DataFrame({"date": dates, "regime_prev": pos, "r_0050": r_etf, "r_ew_index": r_ew,
                        "equity_S2": c2, "equity_S3": c3, "equity_S2b": c2b})
    out.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    a2, a3, a2b = annual(dates, c2), annual(dates, c3), annual(dates, c2b)

    param_hash = hashlib.sha256(
        open(MKT, "rb").read() + open(ETF, "rb").read()
        + f"|switch={COST_SWITCH}|buy={COST_BUY}|J1={J1_MARGIN}|J2={J2_MDD_MIN},{J2_CAGR_MARGIN}"
          f"|S1cagr={S1['cagr_med']}".encode()).hexdigest()[:16]

    # ── 報告 ──
    L = ["# regime_only_test — 擇時層價值測試\n"]
    L.append("## 判定(凍結矩陣,先於統計)\n")
    L.append(f"### J1 選股層去留:**{'選股層退役' if j1 else '選股層保留'}**")
    L.append(f"- S2 幾何 CAGR {S2['cagr']:.2f}% vs S1 中位數 CAGR {S1['cagr_med']:.2f}% − {J1_MARGIN}pp "
             f"= {S1['cagr_med'] - J1_MARGIN:.2f}% → {'✅ 成立' if j1 else '❌ 不成立'}")
    L.append(f"- 判讀:{'「開關 + 買 0050」已達到或超過機器績效,選股層不值得保留。' if j1 else '機器績效超出簡單方案 0.5pp 以上,選股層暫留。'}\n")
    L.append(f"### J2 擇時層去留:**{'regime gate 有價值' if j2 else 'regime gate 無價值 → 結論退化為「直接持有 0050」'}**")
    L.append(f"- (a) MDD 改善 {mdd_gain:.2f}pp(S3 {S3['mdd']:.2f}% → S2 {S2['mdd']:.2f}%),"
             f"門檻 >= {J2_MDD_MIN}pp → {'✅' if j2a else '❌'}")
    L.append(f"- (b) 幾何 CAGR {S2['cagr']:.2f}% vs S3 {S3['cagr']:.2f}% − {J2_CAGR_MARGIN}pp "
             f"= {S3['cagr'] - J2_CAGR_MARGIN:.2f}% → {'✅' if j2b else '❌'}")
    L.append(f"- 兩者{'皆成立' if j2 else '未同時成立'} → **{'保留 gate' if j2 else 'gate 亦無價值'}**\n")

    L.append("## 區間與資料")
    L.append(f"- 共同區間:**{dates[0]} → {dates[-1]}**({len(dates)} 交易日,{years:.2f} 年)")
    L.append(f"- 交易日曆 = data/daily_adj/0050.csv ∩ data/screener/market_index.csv 的交易日")
    L.append(f"- regime 開關日序列直接取自 market_index.csv 的 regime 欄(未重算、未套 raw 延伸)")
    L.append(f"- 參數 hash:`{param_hash}`\n")

    L.append("## 三策略並列")
    L.append("| 指標 | S1 機器(凍結引用) | S2 開關 0050 | S3 買進持有 0050 | S2b 開關+等權(不可投資) |")
    L.append("|---|---|---|---|---|")
    L.append(f"| 幾何 CAGR | **{S1['cagr_med']:.2f}%**(min {S1['cagr_min']:.2f} / max {S1['cagr_max']:.2f}) "
             f"| **{S2['cagr']:.2f}%** | **{S3['cagr']:.2f}%** | {S2b['cagr']:.2f}% |")
    L.append(f"| MDD | {S1['mdd_med']:.2f}%(min {S1['mdd_min']:.2f} / max {S1['mdd_max']:.2f}) "
             f"| {S2['mdd']:.2f}% | {S3['mdd']:.2f}% | {S2b['mdd']:.2f}% |")
    L.append(f"| 年化波動 | — | {S2['vol']:.2f}% | {S3['vol']:.2f}% | {S2b['vol']:.2f}% |")
    L.append(f"| Calmar | {S1['cagr_med'] / S1['mdd_med']:.3f} | {S2['calmar']:.3f} | {S3['calmar']:.3f} "
             f"| {S2b['calmar']:.3f} |")
    L.append(f"| 在場時間比例 | — | {S2['in_market']:.1f}% | 100.0% | {S2b['in_market']:.1f}% |")
    L.append(f"| 開關/交易次數 | {S1['trades_total_med']:.0f} 筆進場 | {S2['switches']} 次開關 | 1 次買進 "
             f"| {S2b['switches']} 次開關 |")
    L.append(f"| 總費用拖累(複利) | 每筆 0.4425%(內含) | −{S2['cost_drag']:.2f}% | −{S3['cost_drag']:.2f}% "
             f"| −{S2b['cost_drag']:.2f}% |")
    L.append(f"| 期末淨值(起點1) | — | {S2['final']:.3f} | {S3['final']:.3f} | {S2b['final']:.3f} |")
    L.append("")

    L.append("## 診斷:gate 本身 vs 開關成本(不參與判定,費用假設未變)")
    L.append("| 版本 | CAGR | 相對 S3 |")
    L.append("|---|---|---|")
    L.append(f"| S3 買進持有 0050 | {S3['cagr']:.2f}% | — |")
    L.append(f"| S2 零成本(只有 gate 的擇時效果) | {S2_gross['cagr']:.2f}% | "
             f"{S2_gross['cagr'] - S3['cagr']:+.2f}pp |")
    L.append(f"| S2 含 {COST_SWITCH*100:.3f}% 開關成本(判定用) | {S2['cagr']:.2f}% | "
             f"{S2['cagr'] - S3['cagr']:+.2f}pp |")
    L.append(f"| S2b 零成本(開關+等權) | {S2b_gross['cagr']:.2f}% | {S2b_gross['cagr'] - S3['cagr']:+.2f}pp |")
    L.append("")
    L.append(f"- gate 本身(零成本)就已經比買進持有少 **{S3['cagr'] - S2_gross['cagr']:.2f}pp/年**;"
             f"開關成本再吃掉 **{S2_gross['cagr'] - S2['cagr']:.2f}pp/年**。"
             "兩者都是負貢獻,不是「好訊號被成本拖垮」。\n")

    L.append("## 分年度報酬")
    L.append("| 年 | S2 開關0050 | S3 持有0050 | S2−S3 | S2b 開關+等權 | S1 機器 |")
    L.append("|---|---|---|---|---|---|")
    for y in sorted(a2):
        L.append(f"| {y} | {fmt(a2[y]*100)} | {fmt(a3[y]*100)} | {fmt((a2[y]-a3[y])*100, unit='pp')} "
                 f"| {fmt(a2b[y]*100)} | 無逐年輸出 |")
    L.append("")
    L.append("> **S2 與 S1 的逐年差額無法產出。** 既有回測(`_phase1_theta.json`)只落地了 20 seed 的"
             "CAGR/MDD 的 min/med/max 與每筆統計,**沒有落地 equity 曲線或逐年報酬**;要產出逐年差額"
             "必須重跑 `phase1_theta.sim_portfolio` 的 20 seed 路徑,那屬於『重算』,本測依指示不做。\n")

    L.append("## regime-off 期間躲掉了什麼")
    L.append("| 期間 | 日數 | 0050 日均 | 0050 中位 | 上漲日占比 | 複利報酬 | 最差日 | 最佳日 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for lab, d in (("regime-OFF(空手)", d_off_etf), ("regime-ON(在場)", d_on_etf)):
        L.append(f"| {lab} | {d['n']} | {fmt(d['mean_pp'], 3, 'pp')} | {fmt(d['median_pp'], 3, 'pp')} "
                 f"| {d['pos_pct']:.1f}% | {fmt(d['compound'])} | {fmt(d['worst_pp'], 2, 'pp')} "
                 f"| {fmt(d['best_pp'], 2, 'pp')} |")
    L.append("")
    L.append("同一切分套在等權指數(訊號自己的標的)上:")
    L.append("| 期間 | 日數 | 等權日均 | 等權中位 | 上漲日占比 | 複利報酬 |")
    L.append("|---|---|---|---|---|---|")
    for lab, d in (("regime-OFF", d_off_ew), ("regime-ON", d_on_ew)):
        L.append(f"| {lab} | {d['n']} | {fmt(d['mean_pp'], 3, 'pp')} | {fmt(d['median_pp'], 3, 'pp')} "
                 f"| {d['pos_pct']:.1f}% | {fmt(d['compound'])} |")
    L.append("")

    L.append("## 凍結選擇與 flag")
    L.append(f"1. **費用**:S2 每次開關來回 {COST_SWITCH*100:.3f}%(手續費 0.1425%×2 + ETF 證交稅 0.1%);"
             f"S3 僅最初買進 {COST_BUY*100:.4f}%;S1 維持原回測 0.4425%。現金部位不計利息。")
    L.append("2. **訊號時序**:第 d 日持倉由前一交易日收盤的 regime 決定(無前視);日報酬為 0050 還原收盤 close-to-close。")
    L.append("3. **⚠️ 訊號/標的錯配**:regime 訊號建在**等權**市場指數上,S2 的投資標的卻是**市值加權**的 0050。"
             "此錯配依指示照做;S2b(開關 + 等權指數本身)為其理論上限,標的不可投資,**不參與判定**。")
    L.append(f"4. **S1 為凍結引用,非本次重算**:來源 {S1['source']},seed={S1['seed']}、nseed={S1['nseed']}。"
             "注意其 vintage 是 2026-07-22 事件集(480 筆),與本輪重凍結的 494 筆事件集不同 —— "
             "S1 的 CAGR/MDD 未隨重凍結更新,因為既有 harness 沒有落地路徑輸出。此為 J1 比較的已知不確定性。")
    L.append("5. **缺價/停牌**:照 bench_kill_test 慣例,close<=0 或缺列的日子直接不成為交易日(不前向填補);"
             "兩序列取交集日曆。")
    L.append("")

    open(OUT_MD, "w", encoding="utf-8").write("\n".join(L))
    json.dump({"param_hash": param_hash, "start": dates[0], "end": dates[-1], "days": len(dates),
               "years": years, "S1": S1, "S2": S2, "S3": S3, "S2b": S2b,
               "S2_gross_no_cost": S2_gross, "S2b_gross_no_cost": S2b_gross,
               "J1_retire_stockpicking": bool(j1), "J2_gate_valuable": bool(j2),
               "J2a_mdd_gain_pp": mdd_gain, "J2b_cagr_ok": bool(j2b),
               "off_dist_etf": d_off_etf, "on_dist_etf": d_on_etf,
               "off_dist_ew": d_off_ew, "on_dist_ew": d_on_ew},
              open(OUT_META, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print("\n".join(L))
    print(f"\n✅ {OUT_CSV} / {OUT_MD} / {OUT_META}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
