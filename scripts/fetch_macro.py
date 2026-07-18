"""
fetch_macro.py — 市場級總經小表 → data/macro/*.csv(全部全市場單 call,便宜、日/月更)
0b 實測深度:
  business_indicator 2015-01(月);futures_institutional TX 2018-06;
  margin_maintenance 2015-01(日);vix 僅 2026-03 起(intraday,resample 成日)。

用法:python scripts/fetch_macro.py [--only business_indicator,vix,...]
需要:FINMIND_TOKEN
"""
import os
import argparse
import pandas as pd
import finmind_client as fc

OUT = "data/macro"
FUTURES_IDS = ["TX", "MTX"]      # 台指期 + 小台;只留台股期貨(0b 實測 2018 起)


def business_indicator(token):
    d = fc.api_data(token, "TaiwanBusinessIndicator", start_date="2010-01-01")
    if d.empty:
        return "missing"
    return fc.write_if_changed(f"{OUT}/business_indicator.csv", d.sort_values("date"),
                               keys=["date"])


def margin_maintenance(token):
    d = fc.api_data(token, "TaiwanTotalExchangeMarginMaintenance", start_date="2015-01-01")
    if d.empty:
        return "missing"
    return fc.write_if_changed(f"{OUT}/margin_maintenance.csv", d.sort_values("date"),
                               keys=["date"])


def futures_institutional(token):
    frames = []
    for fid in FUTURES_IDS:
        d = fc.api_data(token, "TaiwanFuturesInstitutionalInvestors",
                        data_id=fid, start_date="2015-01-01")
        if not d.empty:
            frames.append(d)
    if not frames:
        return "missing"
    alld = pd.concat(frames, ignore_index=True)
    return fc.write_if_changed(f"{OUT}/futures_institutional.csv", alld,
                               keys=["date", "futures_id", "institutional_investors"])


def vix(token):
    """TaiwanOptionVix 是 intraday(date,time,vix);resample 成日 OHLC。0b:僅 2026-03 起。"""
    d = fc.api_data(token, "TaiwanOptionVix", start_date="2015-01-01")
    if d.empty:
        return "missing"
    d["vix"] = pd.to_numeric(d["vix"], errors="coerce")
    g = d.groupby("date")["vix"].agg(vix_open="first", vix_high="max",
                                     vix_low="min", vix_close="last").reset_index()
    return fc.write_if_changed(f"{OUT}/vix.csv", g.sort_values("date"), keys=["date"])


JOBS = {"business_indicator": business_indicator, "margin_maintenance": margin_maintenance,
        "futures_institutional": futures_institutional, "vix": vix}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="逗號清單,只跑這些")
    args = ap.parse_args()
    token = fc.get_token()
    fc.check_token(token)
    os.makedirs(OUT, exist_ok=True)
    jobs = args.only.split(",") if args.only else list(JOBS)
    for name in jobs:
        r = JOBS[name](token)
        print(f"  {name}: {'寫入' if r is True else ('no-op' if r is False else r)}")
    u, lim = fc.token_usage(token)
    print(f"✅ macro 完成。用量 {u}/{lim}")


if __name__ == "__main__":
    main()
