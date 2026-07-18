"""
fetch_news.py — 新聞標題 → data/news/{id}.csv(**只 watchlist 8 檔**,給 skill 層用)
0b 實測:TaiwanStockNews 一個 call 只回「start_date 當天」的新聞(非區間),且深度淺
  —— 2021 回空,約 2024+ 才有。故逐日抓;預設從 2024-01-01(可 --start 調)。
只存 date, title, source, link(不存全文)。utf-8-sig、append-dedup。
checkpoint 記「(股,日)已抓」,續傳不重抓。用量守衛。

用法:python scripts/fetch_news.py [--start 2024-01-01]
需要:FINMIND_TOKEN;data/calendar 用 2330 daily 當交易日曆
"""
import os
import argparse
from datetime import date
import pandas as pd
import finmind_client as fc

OUT = "data/news"
CKPT = ".backfill_checkpoint_news.txt"
STOP_RATIO = 0.9
GUARD_EVERY = 50


def trading_days(start: str) -> list:
    d = pd.read_csv("data/daily/2330.csv", usecols=["date"], dtype=str)
    return sorted(x for x in d["date"].astype(str).unique() if x >= start)


def latest_news_date() -> str:
    """已存新聞的最新日期(nightly 從這裡續,不靠 gitignored checkpoint;Actions 才會增量)。"""
    best = ""
    if os.path.isdir(OUT):
        for f in os.listdir(OUT):
            if f.endswith(".csv"):
                d = pd.read_csv(f"{OUT}/{f}", usecols=["date"], dtype=str)
                if len(d):
                    best = max(best, str(d["date"].max())[:10])
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None,
                    help="起始日;省略=從已存最新日期續(首次落到 2024-01-01)。0b:2021 無資料")
    args = ap.parse_args()
    token = fc.get_token()
    fc.check_token(token)
    os.makedirs(OUT, exist_ok=True)

    ids = fc.watchlist_ids()          # **只 watchlist 8 檔**
    start = args.start or (latest_news_date() or "2024-01-01")
    days = trading_days(start)
    done = set()
    if os.path.exists(CKPT):
        with open(CKPT, encoding="utf-8") as f:
            done = {l.strip() for l in f if l.strip()}
    pairs = [(sid, d) for sid in ids for d in days if f"{sid}|{d}" not in done]
    print(f"news:{len(ids)} 檔 × {len(days)} 日;待抓 {len(pairs)} 個(股,日)")

    buf = {}      # sid -> list of rows
    calls = 0
    for i, (sid, d) in enumerate(pairs, 1):
        if i % GUARD_EVERY == 1 and i > 1:
            u, lim = fc.token_usage(token)
            if u and lim and u > lim * STOP_RATIO:
                print(f"   ⏸ 用量 {u}/{lim},停下續傳"); break
        r = fc.api_data(token, "TaiwanStockNews", data_id=sid, start_date=d, end_date=d)
        calls += 1
        if not r.empty:
            for _, x in r.iterrows():
                buf.setdefault(sid, []).append({"date": x.get("date"), "title": x.get("title"),
                                                "source": x.get("source"), "link": x.get("link")})
        with open(CKPT, "a", encoding="utf-8") as f:
            f.write(f"{sid}|{d}\n")
        # 每 500 筆 flush 一次(斷點保護)
        if i % 500 == 0 or i == len(pairs):
            for s, rows in buf.items():
                if rows:
                    fc.write_if_changed(f"{OUT}/{s}.csv", pd.DataFrame(rows),
                                        keys=["date", "title"])
            buf = {}
            u, lim = fc.token_usage(token)
            print(f"   [{i}/{len(pairs)}] 累計 {calls} call;用量 {u}/{lim}", flush=True)

    for s, rows in buf.items():
        if rows:
            fc.write_if_changed(f"{OUT}/{s}.csv", pd.DataFrame(rows), keys=["date", "title"])
    u, lim = fc.token_usage(token)
    print(f"✅ news:本輪 {calls} call;用量 {u}/{lim}")


if __name__ == "__main__":
    main()
