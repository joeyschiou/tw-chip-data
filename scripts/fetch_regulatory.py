"""
fetch_regulatory.py — 名冊小表(全市場單表)
  data/delisting.csv                     下市櫃表(0b:2001 起)
免費帳號不可用、已移除(實測 400 "Your level is register"):
  處置股 disposition(TaiwanStockDispositionSecuritiesPeriod)、產業鏈 industry_chain(TaiwanStockIndustryChain)。
  data/regulatory/disposition.csv、data/industry_chain.csv 保留為歷史,不再更新。

用法:python scripts/fetch_regulatory.py
需要:FINMIND_TOKEN
"""
import finmind_client as fc


def delisting(token):
    d = fc.api_data(token, "TaiwanStockDelisting", start_date="1990-01-01")
    if d.empty:
        return "missing"
    return fc.write_if_changed("data/delisting.csv", d.sort_values("date"),
                               keys=["stock_id", "date"])


def main():
    token = fc.get_token()
    fc.check_token(token)
    for name, fn in [("delisting", delisting)]:
        r = fn(token)
        print(f"  {name}: {'寫入' if r is True else ('no-op' if r is False else r)}")
    u, lim = fc.token_usage(token)
    print(f"✅ regulatory 完成。用量 {u}/{lim}")


if __name__ == "__main__":
    main()
