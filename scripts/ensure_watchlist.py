"""
ensure_watchlist.py — 確保某代號在 watchlist.yaml 裡。已存在則跳過;
不存在則在檔尾以純文字 append 一個 ticker 區塊(保留原檔所有註解)。
用法:python scripts/ensure_watchlist.py --stock 6278 --market twse --note "台表科"
"""
import argparse, yaml
WL = "config/watchlist.yaml"
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock", required=True)
    ap.add_argument("--market", required=True, choices=["twse","tpex"])
    ap.add_argument("--note", default="")
    a = ap.parse_args()
    with open(WL, encoding="utf-8") as f:
        existing = {str(t["id"]) for t in yaml.safe_load(f).get("tickers", [])}
    if a.stock in existing:
        print(f"⏭ {a.stock} 已在 watchlist,不重複加入。"); return
    note = a.note or f"{a.stock}(backfill 加入)"
    block = f'\n  - id: "{a.stock}"\n    market: {a.market}\n    note: {note}\n'
    with open(WL, "a", encoding="utf-8") as f:
        f.write(block)
    print(f"✅ 已把 {a.stock}({a.market})加入 watchlist。")
if __name__ == "__main__":
    main()
