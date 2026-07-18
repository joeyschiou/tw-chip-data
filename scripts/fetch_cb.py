"""
fetch_cb.py — 可轉債(CB)四表 → data/cb/
  info.csv                     TaiwanStockConvertibleBondInfo(全表 1 call)+ 衍生 stock_id=cb_id[:4](join 鍵)
  overview.csv                 TaiwanStockConvertibleBondDailyOverview(每日全市場快照 append)
  daily/{cb_id}.csv            TaiwanStockConvertibleBondDaily(逐 CB,2015 起或上市起)
  institutional/{cb_id}.csv    TaiwanStockConvertibleBondInstitutionalInvestors(逐 CB)

cb_id↔股票代號:Taiwan CB 代號前 4 碼 = 標的股代號(11011→1101 台泥;已驗)。
checkpoint 續傳(daily/institutional 各一)+ 用量守衛。utf-8-sig、append-dedup。

用法:
  python scripts/fetch_cb.py --tables info,overview     # 便宜的先跑
  python scripts/fetch_cb.py --tables daily             # 逐 CB(過夜)
  python scripts/fetch_cb.py --tables institutional
需要:FINMIND_TOKEN
"""
import os
import time
import argparse
import pandas as pd
import finmind_client as fc

OUT = "data/cb"
START = "2015-01-01"
STOP_RATIO = 0.9
GUARD_EVERY = 50


def cb_ids(token) -> list:
    p = f"{OUT}/info.csv"
    if os.path.exists(p):
        return pd.read_csv(p, dtype=str)["cb_id"].astype(str).tolist()
    d = fc.api_data(token, "TaiwanStockConvertibleBondInfo")
    return d["cb_id"].astype(str).tolist() if not d.empty else []


def do_info(token):
    d = fc.api_data(token, "TaiwanStockConvertibleBondInfo")
    if d.empty:
        return "missing"
    d["cb_id"] = d["cb_id"].astype(str)
    d["stock_id"] = d["cb_id"].str[:4]          # join 鍵
    cols = ["cb_id", "stock_id"] + [c for c in d.columns if c not in ("cb_id", "stock_id")]
    os.makedirs(OUT, exist_ok=True)
    return fc.write_if_changed(f"{OUT}/info.csv", d[cols], keys=["cb_id"])


def do_overview(token):
    # 0b:overview 是每日全市場快照,寬 range 回空 → 抓近 10 天窗(每日 append 累積歷史)
    from datetime import date, timedelta
    start = (date.today() - timedelta(days=10)).isoformat()
    d = fc.api_data(token, "TaiwanStockConvertibleBondDailyOverview", start_date=start)
    if d.empty:
        return "missing"
    os.makedirs(OUT, exist_ok=True)
    return fc.write_if_changed(f"{OUT}/overview.csv", d.sort_values(["date", "cb_id"]),
                               keys=["cb_id", "date"])


def do_per_cb(token, dataset, subdir, key, wait_quota=False):
    d0 = os.path.join(OUT, subdir)
    os.makedirs(d0, exist_ok=True)
    ckpt = f".backfill_checkpoint_cb_{subdir}.txt"
    ids = cb_ids(token)
    done = set()
    if os.path.exists(ckpt):
        with open(ckpt, encoding="utf-8") as f:
            done = {l.strip() for l in f if l.strip()}
    todo = [c for c in ids if c not in done]
    print(f"   cb {subdir}:{len(ids)} CB;已完成 {len(done)};待抓 {len(todo)}")
    calls = 0
    for i, cid in enumerate(todo, 1):
        if i % GUARD_EVERY == 1 and i > 1:
            u, lim = fc.token_usage(token)
            if u and lim and u > lim * STOP_RATIO:
                if wait_quota:
                    while u and lim and u > lim * 0.6:
                        print(f"   ⏳ 用量 {u}/{lim},等額度… sleep 5m", flush=True)
                        time.sleep(300); u, lim = fc.token_usage(token)
                else:
                    print(f"   ⏸ 用量 {u}/{lim},停下續傳"); break
        d = fc.api_data(token, dataset, data_id=cid, start_date=START)
        calls += 1
        if not d.empty:
            d["cb_id"] = cid
            cols = ["date"] + [c for c in d.columns if c != "date"] if "date" in d.columns else list(d.columns)
            fc.write_if_changed(f"{d0}/{cid}.csv", d[cols], keys=key)
        with open(ckpt, "a", encoding="utf-8") as f:
            f.write(cid + "\n")
        if i % 200 == 0 or i == len(todo):
            u, lim = fc.token_usage(token)
            print(f"   [{i}/{len(todo)}] 累計 {calls} call;用量 {u}/{lim}", flush=True)
    return calls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tables", default="info,overview",
                    help="info,overview,daily,institutional")
    ap.add_argument("--wait-quota", action="store_true", help="逼近上限 sleep 等額度續跑(workflow 用)")
    args = ap.parse_args()
    token = fc.get_token()
    fc.check_token(token)
    tabs = args.tables.split(",")
    if "info" in tabs:
        print("  info:", do_info(token))
    if "overview" in tabs:
        print("  overview:", do_overview(token))
    if "daily" in tabs:
        do_per_cb(token, "TaiwanStockConvertibleBondDaily", "daily", ["date"], args.wait_quota)
    if "institutional" in tabs:
        do_per_cb(token, "TaiwanStockConvertibleBondInstitutionalInvestors",
                  "institutional", ["date"], args.wait_quota)
    u, lim = fc.token_usage(token)
    print(f"✅ cb 完成。用量 {u}/{lim}")


if __name__ == "__main__":
    main()
