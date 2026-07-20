"""
migrate_margin_units.py — 一次性單位遷移:data/daily/*.csv 的
margin_balance_shares / short_balance_shares 從「張」×1000 改存「股」(schema 鐵則)。

- 兩欄非空值 ×1000;空值維持空。
- 輸出整數(Int64,不要 32548000.0);utf-8-sig;內容沒變的檔不重寫。
- 防重跑 sentinel:成功後建 data/.migrations/margin_units_x1000.done,存在即拒跑。

用法:python scripts/migrate_margin_units.py
"""
import os
import sys
import glob
import pandas as pd

SENTINEL_DIR = "data/.migrations"
SENTINEL = f"{SENTINEL_DIR}/margin_units_x1000.done"
COLS = ["margin_balance_shares", "short_balance_shares"]


def main():
    if os.path.exists(SENTINEL):
        sys.exit(f"⛔ 已遷移過({SENTINEL} 存在),拒絕重跑。")

    files = sorted(glob.glob("data/daily/*.csv"))
    changed = 0
    for f in files:
        d = pd.read_csv(f, dtype=str)
        cols = [c for c in COLS if c in d.columns]
        if not cols:
            continue
        file_changed = False
        for c in cols:
            v = pd.to_numeric(d[c], errors="coerce")          # 空→NaN
            if not v.notna().any():
                continue                                       # 整欄空,略過
            new = (v * 1000).astype("Int64")                   # ×1000,NaN→<NA>
            old_int = v.astype("Int64")
            if not new.equals(old_int):                        # 有實質變動才算(0×1000=0 不算)
                file_changed = True
            d[c] = new                                         # <NA> 於 to_csv 寫成空字串
        if file_changed:
            d.to_csv(f, index=False, encoding="utf-8-sig")
            changed += 1

    os.makedirs(SENTINEL_DIR, exist_ok=True)
    with open(SENTINEL, "w", encoding="utf-8") as fh:
        fh.write("margin_balance_shares / short_balance_shares ×1000 (張→股) 完成。\n")
    print(f"✅ 遷移完成:掃 {len(files)} 檔,重寫 {changed} 檔(有 margin 資料者)。sentinel:{SENTINEL}")


if __name__ == "__main__":
    main()
