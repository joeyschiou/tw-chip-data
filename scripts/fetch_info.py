"""
fetch_info.py — 全市場個股基本資料 → data/info.csv
一次 call(TaiwanStockInfo),給每檔上下文用(名稱/產業/上市櫃別)。
冪等:每次重抓全表覆寫;同資料 → 同檔。

欄位:stock_id, name, industry, type(twse=上市 / tpex=上櫃)
用法:python scripts/fetch_info.py   需要:FINMIND_TOKEN
"""

import os
import pandas as pd
import finmind_client as fc

OUT = "data/info.csv"


def build_info(token: str) -> pd.DataFrame:
    raw = fc.api_data(token, "TaiwanStockInfo")
    if raw.empty:
        return pd.DataFrame()
    raw["stock_id"] = raw["stock_id"].astype(str)
    # 只留上市/上櫃普通掛牌;排除 emerging(興櫃)與 Index 等
    keep = raw[raw["type"].str.lower().isin(["twse", "tpex"])].copy()
    # TaiwanStockInfo 一檔可能有多列(不同 industry_category);每檔留一列
    keep = keep.sort_values(["stock_id", "type"]).drop_duplicates("stock_id", keep="first")
    out = keep.rename(columns={"stock_name": "name", "industry_category": "industry"})
    out = out[["stock_id", "name", "industry", "type"]].sort_values("stock_id")
    return out.reset_index(drop=True)


def main() -> None:
    token = fc.get_token()
    fc.check_token(token)
    os.makedirs("data", exist_ok=True)

    df = build_info(token)
    if df.empty:
        print("⚠ TaiwanStockInfo 無資料,未寫出")
        return

    # 冪等 no-op:內容沒變就不動檔(避免無意義 diff)
    if os.path.exists(OUT):
        old = pd.read_csv(OUT, dtype=str).sort_values("stock_id").reset_index(drop=True)
        if old.equals(df.astype(str).reset_index(drop=True)):
            print(f"⏭ info 無變動({len(df)} 檔),no-op")
            return

    df.to_csv(OUT, index=False, encoding="utf-8-sig")
    print(f"✅ {OUT}:{len(df)} 檔(twse+tpex)")


if __name__ == "__main__":
    main()
