"""
fetch_universe.py — 產生全市場「上市 twse + 上櫃 tpex」普通股清單 → config/universe.csv
給 fetch_daily 廣掃、以及 holders/float/revenue/daytrade 的 universe 切片用;分點(branch)不吃這份。
用法:python scripts/fetch_universe.py   需要:FINMIND_TOKEN

過濾(0b 實測 type 字串:twse=上市 / tpex=上櫃 / emerging=興櫃):
  - 保留 type ∈ {twse, tpex};排除 emerging(興櫃)
  - is_common:stock_id 4 位、非 0 開頭(排 00xx ETF、字母尾綴特別股如 2881A、6 位權證)
  - 排 91xx TDR;額外排 industry 含 ETF(保險)
輸出欄位:id, name, market(twse/tpex)
"""
import os, sys
import requests
import pandas as pd
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
def get_token():
    t = os.environ.get("FINMIND_TOKEN")
    if not t: sys.exit("找不到環境變數 FINMIND_TOKEN。")
    return t
def main():
    token = get_token()
    r = requests.get(FINMIND_URL, headers={"Authorization": f"Bearer {token}"},
                     params={"dataset": "TaiwanStockInfo"}, timeout=60)
    if r.status_code != 200: sys.exit(f"TaiwanStockInfo HTTP {r.status_code}")
    df = pd.DataFrame(r.json().get("data", []))
    if "type" in df.columns: print("type 值:", df["type"].value_counts().to_dict())
    df["stock_id"] = df["stock_id"].astype(str)
    df["type"] = df["type"].str.lower()
    is_listed = df["type"].isin(["twse", "tpex"])              # 上市 + 上櫃;排興櫃 emerging
    is_common = df["stock_id"].str.match(r"^[1-9]\d{3}$")       # 排 00xx ETF、特別股、權證
    not_tdr = ~df["stock_id"].str.match(r"^91\d\d$")            # 排 91xx TDR
    not_etf = ~df.get("industry_category", pd.Series("", index=df.index)).fillna("").str.contains("ETF")
    keep = df[is_listed & is_common & not_tdr & not_etf].sort_values(["stock_id", "type"])
    keep = keep.drop_duplicates("stock_id", keep="first")
    out = (keep[["stock_id", "stock_name", "type"]]
           .rename(columns={"stock_id": "id", "stock_name": "name", "type": "market"})
           .sort_values("id"))
    os.makedirs("config", exist_ok=True)
    out.to_csv("config/universe.csv", index=False, encoding="utf-8-sig")
    counts = out["market"].value_counts().to_dict()
    print(f"✅ universe:{len(out)} 檔普通股(twse={counts.get('twse',0)} / tpex={counts.get('tpex',0)})"
          f" → config/universe.csv")
if __name__ == "__main__": main()
