"""
fetch_universe.py — 產生全市場(上市 twse)普通股清單 → config/universe.csv
只給 fetch_daily.py 廣掃用;分點不吃這份。
用法:python scripts/fetch_universe.py   需要:FINMIND_TOKEN
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
    print("欄位:", list(df.columns))
    if "type" in df.columns: print("type 值:", df["type"].value_counts().to_dict())
    df["stock_id"] = df["stock_id"].astype(str)
    # 先確認上一行印出的 type 值:上市應是 "twse"。若實際值不同,改下面這條件。
    is_twse = df["type"].str.lower().eq("twse") if "type" in df.columns else True
    is_common = df["stock_id"].str.match(r"^[1-9]\d{3}$")   # 4 位、非0開頭 → 排除 00xx ETF、6位權證
    not_tdr = ~df["stock_id"].str.match(r"^91\d\d$")          # 排除 91xx TDR
    keep = df[is_twse & is_common & not_tdr].drop_duplicates("stock_id")
    out = keep[["stock_id","stock_name"]].rename(columns={"stock_id":"id","stock_name":"name"}).sort_values("id")
    os.makedirs("config", exist_ok=True)
    out.to_csv("config/universe.csv", index=False, encoding="utf-8-sig")
    print(f"✅ universe:{len(out)} 檔上市普通股 → config/universe.csv")
if __name__ == "__main__": main()
