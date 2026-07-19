"""
screener_selftest.py — 公式不變量守門(與定案回測逐位比對)
只用 data/daily_adj(不含 raw 延伸),重放 2015→今:
  成立訊號總數 必須 = 25,003 ± 5%(i∈[65,n-22),不套 regime/排序/衛生層/5格)
  V1 事件總數  必須 =  8,313 ± 10%(i∈[25,n-22),spacing 10,不套 maintenance<155)
印逐年分佈供比對。過不了 → 實作與定案回測不一致,修實作,禁改參數湊數。

用法:python scripts/screener_selftest.py
"""
import sys
from collections import Counter
import yaml
import screener_core as sc

BREAKOUT_TARGET = 25003
V1_TARGET = 8313


def main():
    cfg = yaml.safe_load(open("config/screener.yaml", encoding="utf-8"))
    me = cfg["main_engine"]
    v1 = cfg["satellites"]["v1_margin_flush"]

    ids = sc.universe_ids()
    bk_years, v1_years = Counter(), Counter()
    bk_total = v1_total = 0
    n_stocks = 0

    for k, sid in enumerate(ids, 1):
        d = sc.clean_adj(sid)
        if d is None or len(d) < 66:
            continue
        n_stocks += 1
        dates = d["date"]
        # 成立訊號
        yoy = sc.yoy_asof(sid, dates)
        for i in sc.breakout_signals(d, yoy, lookback=me["breakout_lookback"],
                                     yoy_min=me["yoy_min"], spacing=me["spacing_days"],
                                     i_lo=65, i_hi_off=22):
            bk_total += 1
            bk_years[str(dates.iloc[i])[:4]] += 1
        # V1 事件
        margin = sc.margin_on_adj(sid, dates)
        for i in sc.v1_events(d, margin, v1, spacing=10, i_lo=25, i_hi_off=22):
            v1_total += 1
            v1_years[str(dates.iloc[i])[:4]] += 1
        if k % 400 == 0:
            print(f"   ...{k}/{len(ids)} 掃描中", flush=True)

    def band(t, pct):
        return t * (1 - pct), t * (1 + pct)

    bk_lo, bk_hi = band(BREAKOUT_TARGET, 0.05)
    v1_lo, v1_hi = band(V1_TARGET, 0.10)
    bk_ok = bk_lo <= bk_total <= bk_hi
    v1_ok = v1_lo <= v1_total <= v1_hi

    print(f"\n掃描 {n_stocks} 檔(清洗後 >=66 列)")
    print("\n=== 成立訊號逐年 ===")
    for y in sorted(bk_years):
        print(f"   {y}: {bk_years[y]}")
    print(f"   TOTAL {bk_total}  target {BREAKOUT_TARGET} ±5% [{bk_lo:.0f},{bk_hi:.0f}]  "
          f"{'PASS ✓' if bk_ok else 'FAIL ✗'}")
    print("\n=== V1 事件逐年 ===")
    for y in sorted(v1_years):
        print(f"   {y}: {v1_years[y]}")
    print(f"   TOTAL {v1_total}  target {V1_TARGET} ±10% [{v1_lo:.0f},{v1_hi:.0f}]  "
          f"{'PASS ✓' if v1_ok else 'FAIL ✗'}")

    sys.exit(0 if (bk_ok and v1_ok) else 1)


if __name__ == "__main__":
    main()
