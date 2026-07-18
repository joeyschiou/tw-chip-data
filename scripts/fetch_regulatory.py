"""
fetch_regulatory.py — 監理/名冊小表(全市場單表)
  data/regulatory/disposition.csv       處置股歷史(0b:2010 起;分段抓避免 range cap)
  data/delisting.csv                     下市櫃表(0b:2001 起)
  data/industry_chain.csv                產業鏈(0b:目前快照 2025+)
停券/融券回補(short_suspension)是 per-id → 交給 fetch_stockseries.py(全市場長 range 無效)。

用法:python scripts/fetch_regulatory.py
需要:FINMIND_TOKEN
"""
import os
import pandas as pd
import finmind_client as fc


def disposition(token):
    # 0b:單 call 會被 range 截斷,分段(每 3 年)抓再合併
    frames = []
    for s, e in [("2005-01-01", "2011-12-31"), ("2012-01-01", "2017-12-31"),
                 ("2018-01-01", "2022-12-31"), ("2023-01-01", None)]:
        d = fc.api_data(token, "TaiwanStockDispositionSecuritiesPeriod",
                        start_date=s, end_date=e)
        if not d.empty:
            frames.append(d)
    if not frames:
        return "missing"
    alld = pd.concat(frames, ignore_index=True)
    os.makedirs("data/regulatory", exist_ok=True)
    changed = fc.write_if_changed("data/regulatory/disposition.csv", alld,
                                  keys=["stock_id", "period_start", "period_end"])
    print(f"     disposition: {len(alld.drop_duplicates(['stock_id','period_start','period_end']))} 筆,"
          f"{alld['date'].min()}→{alld['date'].max()}")
    return changed


def delisting(token):
    d = fc.api_data(token, "TaiwanStockDelisting", start_date="1990-01-01")
    if d.empty:
        return "missing"
    return fc.write_if_changed("data/delisting.csv", d.sort_values("date"),
                               keys=["stock_id", "date"])


def industry_chain(token):
    d = fc.api_data(token, "TaiwanStockIndustryChain")
    if d.empty:
        return "missing"
    d["stock_id"] = d["stock_id"].astype(str)
    return fc.write_if_changed("data/industry_chain.csv", d,
                               keys=["stock_id", "industry", "sub_industry"])


def main():
    token = fc.get_token()
    fc.check_token(token)
    for name, fn in [("disposition", disposition), ("delisting", delisting),
                     ("industry_chain", industry_chain)]:
        r = fn(token)
        print(f"  {name}: {'寫入' if r is True else ('no-op' if r is False else r)}")
    u, lim = fc.token_usage(token)
    print(f"✅ regulatory 完成。用量 {u}/{lim}")


if __name__ == "__main__":
    main()
