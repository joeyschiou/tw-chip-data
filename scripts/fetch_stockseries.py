"""
fetch_stockseries.py — 通用「逐檔 date-range」個股序列回補(universe scope)
把結構相同的 4 個 per-id dataset 收斂成一支(DRY):
  adj       TaiwanStockPriceAdj           → data/daily_adj/{id}.csv   還原股價(2015)
  short     TaiwanDailyShortSaleBalances  → data/short/{id}.csv       融券+借券賣出餘額(2015)
  pledge    TaiwanStockLoanCollateralBalance → data/pledge/{id}.csv   借貸擔保品餘額(2015)
  shortsusp TaiwanStockMarginShortSaleSuspension → data/short_suspension/{id}.csv 停券/回補日(2015)

欄位照 FinMind 實測原樣存(僅去掉 stock_id,存進檔名);utf-8-sig;append-dedup。
checkpoint 續傳(每 dataset 一個檔)+ 用量守衛(每 50 檔查一次,逼近 90% 停,重跑接續)。

用法:
  python scripts/fetch_stockseries.py --dataset adj                # 全 universe 回補(過夜)
  python scripts/fetch_stockseries.py --dataset short --new-only    # 只補沒檔的
  python scripts/fetch_stockseries.py --dataset adj --stock 2330
需要:FINMIND_TOKEN;data/info.csv + data/daily/*.csv(算 universe)
"""
import os
import argparse
import pandas as pd
import finmind_client as fc

CFG = {
    "adj":       {"ds": "TaiwanStockPriceAdj",              "dir": "data/daily_adj",
                  "start": "2015-01-01", "key": ["date"]},
    "short":     {"ds": "TaiwanDailyShortSaleBalances",     "dir": "data/short",
                  "start": "2015-01-01", "key": ["date"]},
    "pledge":    {"ds": "TaiwanStockLoanCollateralBalance", "dir": "data/pledge",
                  "start": "2015-01-01", "key": ["date"]},
    "shortsusp": {"ds": "TaiwanStockMarginShortSaleSuspension", "dir": "data/short_suspension",
                  "start": "2015-01-01", "key": ["date", "end_date", "reason"]},
}
STOP_RATIO = 0.9
GUARD_EVERY = 50


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True, choices=list(CFG))
    ap.add_argument("--stock", default=None, help="只抓這一檔")
    ap.add_argument("--new-only", action="store_true", help="只抓還沒有檔的代號")
    args = ap.parse_args()
    cfg = CFG[args.dataset]

    token = fc.get_token()
    fc.check_token(token)
    os.makedirs(cfg["dir"], exist_ok=True)
    ckpt = f".backfill_checkpoint_{args.dataset}.txt"

    ids = [str(args.stock)] if args.stock else fc.load_universe()
    if args.new_only:
        ids = [s for s in ids if not os.path.exists(f"{cfg['dir']}/{s}.csv")]
    done = set()
    if not args.stock and os.path.exists(ckpt):
        with open(ckpt, encoding="utf-8") as f:
            done = {l.strip() for l in f if l.strip()}
    todo = [s for s in ids if s not in done]
    print(f"{args.dataset}({cfg['ds']}):universe {len(ids)};已完成 {len(done)};待抓 {len(todo)}")

    calls = 0
    for i, sid in enumerate(todo, 1):
        if i % GUARD_EVERY == 1 and i > 1:
            u, lim = fc.token_usage(token)
            if u and lim and u > lim * STOP_RATIO:
                print(f"   ⏸ 用量 {u}/{lim},停下續傳(重跑同指令從 checkpoint 接續)")
                break
        d = fc.api_data(token, cfg["ds"], data_id=sid, start_date=cfg["start"])
        calls += 1
        if not d.empty:
            d = d.drop(columns=[c for c in ["stock_id"] if c in d.columns])
            cols = ["date"] + [c for c in d.columns if c != "date"]   # date 擺第一欄
            fc.write_if_changed(f"{cfg['dir']}/{sid}.csv", d[cols].sort_values("date"), keys=cfg["key"])
        if not args.stock:
            with open(ckpt, "a", encoding="utf-8") as f:
                f.write(sid + "\n")     # 含無資料的股(記進 checkpoint,免下輪重試)
        if i % 200 == 0 or i == len(todo):
            u, lim = fc.token_usage(token)
            print(f"   [{i}/{len(todo)}] {sid} 累計 {calls} call;用量 {u}/{lim}", flush=True)

    u, lim = fc.token_usage(token)
    n = len([f for f in os.listdir(cfg["dir"]) if f.endswith(".csv")])
    print(f"✅ {args.dataset}:本輪 {calls} call;{cfg['dir']} 共 {n} 檔;用量 {u}/{lim}")


if __name__ == "__main__":
    main()
