"""
runner_backfill.py — B–E 桶連板事件的分點定向回補(可中斷續跑)

對象:data/screener/runner_census.csv 中 branch-era ∧ B–E 桶(20 日均成交金額 >=30M)的事件,
      每事件 D-120 ~ D+20 個「該檔自己的交易日」窗口,同檔跨事件去重疊。
端點:taiwan_stock_trading_daily_report(sponsor);節流預設 5,500 req/hr。
落地:data/branch_research/{sid}.csv —— 全分點、weighted avg cost 排除 price=0、utf-8-sig,
      欄位與 data/branch 相同(唯讀重用 fetch_branch.fetch_raw_branch / aggregate)。

用法:
  python scripts/runner_backfill.py --plan-only        # 只建 todo 與成本估算,零 API
  python scripts/runner_backfill.py                    # 開跑(可隨時 Ctrl-C,重跑自動續)
  python scripts/runner_backfill.py --verify-only      # 只做覆蓋核對
  python scripts/runner_backfill.py --slice top25      # 改存 top-25∪tagged 切片(省 85% 空間)

續跑機制:
  - 已有的 (檔,日) 直接跳過(掃 data/branch_research ∪ data/branch)。
  - 空回應(停牌/處置分盤/官方缺漏日)記進 state 的 empty_pairs,**下次不再重試**,避免打爆配額。
  - 每 FLUSH_EVERY 對 per-sid 落地一次;中斷最多損失一個 flush 區塊。

自我節制(非修改 nightly,只是本腳本讓路):
  - 預設在台北 21:30–02:00 暫停(nightly daily-update 22:00 起跑,需要約 5,400 call/hr)。
    2026-07-27 那次夜更就是因為起跑時配額已被吃掉 3,002 而早停,只覆蓋 811/2,134 檔。
    `--no-blackout` 可關閉。
"""
import os
import sys
import json
import time
import glob
import argparse
import threading
import datetime as dt
import concurrent.futures as cf

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "research"))
import fetch_branch as fb                      # noqa: E402  唯讀重用抓取+彙總
import finmind_client as fc                    # noqa: E402

CENSUS = "data/screener/runner_census.csv"
OUTDIR = "data/branch_research"
STATE = f"{OUTDIR}/_runner_backfill_state.json"
COV_CSV = "data/screener/runner_backfill_coverage.csv"
MISS_CSV = "data/screener/runner_backfill_missing_days.csv"
DAILY = "data/daily"

DATA_START = fb.DATA_START                     # 2021-06-30,分點資料起點
KNOWN_GAPS = fb.KNOWN_GAPS                     # 官方明列缺漏日
COLS = ["date", "broker_id", "broker_name", "buy_shares", "sell_shares",
        "net_shares", "avg_buy_price", "avg_sell_price"]
FLUSH_EVERY = 150
BLACKOUT = (dt.time(21, 30), dt.time(2, 0))    # 台北時間,讓路給 nightly


def stock_days(sid):
    p = f"{DAILY}/{sid}.csv"
    if not os.path.exists(p):
        return []
    d = pd.read_csv(p, dtype=str, usecols=["date", "close"])
    d["c"] = pd.to_numeric(d["close"], errors="coerce")
    d = d[d["c"] > 0].drop_duplicates("date", keep="last").sort_values("date")
    return d["date"].tolist()


def build_todo(window, post):
    """回 (todo[list of (sid,date)], events[list], stock_days_cache)。純本機,零 API。"""
    ev = pd.read_csv(CENSUS, dtype=str)
    ev["branch_era"] = ev["branch_era"].astype(str).str.lower() == "true"
    ev = ev[ev["branch_era"] & (ev["value_bucket"] != "A <30M")].copy()
    ev["event_idx"] = ev["event_idx"].astype(int)
    cache, need, per_event = {}, set(), []
    for sid, i, edate in zip(ev["sid"], ev["event_idx"], ev["event_date"]):
        if sid not in cache:
            cache[sid] = stock_days(sid)
        ds = cache[sid]
        lo, hi = max(0, i - window), min(len(ds), i + post + 1)
        win = [d for d in ds[lo:hi] if d >= DATA_START and d not in KNOWN_GAPS]
        per_event.append({"sid": sid, "event_date": edate, "window_days": len(win),
                          "window_start": win[0] if win else "", "window_end": win[-1] if win else "",
                          "raw_span": hi - lo})
        need.update((sid, d) for d in win)
    return sorted(need), per_event, cache


def covered(sids):
    cov = set()
    for sid in sids:
        for base in (f"{OUTDIR}/{sid}.csv", f"data/branch/{sid}.csv"):
            if os.path.exists(base) and os.path.getsize(base) > 0:
                try:
                    for d in pd.read_csv(base, usecols=["date"], dtype=str)["date"].unique():
                        cov.add((sid, d))
                except Exception:
                    pass
    return cov


def load_state():
    if os.path.exists(STATE):
        try:
            s = json.load(open(STATE, encoding="utf-8"))
            s["empty_pairs"] = {tuple(x) for x in s.get("empty_pairs", [])}
            return s
        except Exception:
            pass
    return {"empty_pairs": set(), "fetched": 0, "started": None, "last": None}


def save_state(s):
    out = dict(s)
    out["empty_pairs"] = sorted(list(x) for x in s["empty_pairs"])
    json.dump(out, open(STATE, "w", encoding="utf-8"), ensure_ascii=False)


def dir_gb():
    return sum(os.path.getsize(f) for f in glob.glob(f"{OUTDIR}/*.csv")) / 1e9


def flush(buf, slice_mode):
    for sid, frames in buf.items():
        p = f"{OUTDIR}/{sid}.csv"
        new = pd.concat(frames, ignore_index=True)
        if os.path.exists(p) and os.path.getsize(p) > 0:
            try:
                new = pd.concat([pd.read_csv(p, dtype={"broker_id": str}), new], ignore_index=True)
            except pd.errors.EmptyDataError:
                pass
        new = new.drop_duplicates(subset=["date", "broker_id"], keep="last").sort_values(["date", "broker_id"])
        new[COLS].to_csv(p, index=False, encoding="utf-8-sig")
    buf.clear()


def in_blackout(now=None):
    t = (now or dt.datetime.now()).time()
    a, b = BLACKOUT
    return t >= a or t < b


def verify(per_event, window, post, empty_pairs):
    """逐事件窗口完整率 + 缺日清單(標記合法缺日)。"""
    sids = {e["sid"] for e in per_event}
    cov = covered(sids)
    rows, miss = [], []
    for e in per_event:
        sid = e["sid"]
        ds = stock_days(sid)
        i0 = ds.index(e["window_start"]) if e["window_start"] in ds else None
        win = []
        if i0 is not None and e["window_end"] in ds:
            win = [d for d in ds[i0:ds.index(e["window_end"]) + 1]
                   if d >= DATA_START and d not in KNOWN_GAPS]
        have = [d for d in win if (sid, d) in cov]
        gone = [d for d in win if (sid, d) not in cov]
        rows.append({"sid": sid, "event_date": e["event_date"],
                     "window_start": e["window_start"], "window_end": e["window_end"],
                     "window_days": len(win), "covered_days": len(have),
                     "missing_days": len(gone),
                     "coverage_pct": round(len(have) / len(win) * 100, 2) if win else 0.0})
        for d in gone:
            legal = "known_gap" if d in KNOWN_GAPS else ("empty_response" if (sid, d) in empty_pairs else "")
            miss.append({"sid": sid, "event_date": e["event_date"], "missing_date": d,
                         "legal_gap_reason": legal or "unfetched"})
    pd.DataFrame(rows).to_csv(COV_CSV, index=False, encoding="utf-8-sig")
    pd.DataFrame(miss).to_csv(MISS_CSV, index=False, encoding="utf-8-sig")
    return pd.DataFrame(rows), pd.DataFrame(miss)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=120)
    ap.add_argument("--post", type=int, default=20)
    ap.add_argument("--rate", type=int, default=5500, help="req/hr 節流上限(硬上限,並發也不會超過)")
    ap.add_argument("--workers", type=int, default=3, help="並發請求數;API 延遲 ~1.3s,單條只能跑 ~2,800/hr")
    ap.add_argument("--slice", choices=["full", "top25"], default="full")
    ap.add_argument("--halt-gb", type=float, default=5.0)
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--no-blackout", action="store_true")
    ap.add_argument("--max-calls", type=int, default=0, help=">0 時本輪最多抓 N 個 (檔,日)")
    args = ap.parse_args()

    todo_all, per_event, _ = build_todo(args.window, args.post)
    sids = sorted({s for s, _ in todo_all})
    state = load_state()

    if args.verify_only:
        cov_df, miss_df = verify(per_event, args.window, args.post, state["empty_pairs"])
        print(f"逐事件覆蓋:平均 {cov_df['coverage_pct'].mean():.2f}%,"
              f"100% 完整 {int((cov_df['coverage_pct'] >= 99.999).sum())}/{len(cov_df)} 事件")
        print(f"缺日 {len(miss_df)} 筆 → {MISS_CSV};覆蓋表 → {COV_CSV}")
        if len(miss_df):
            print(miss_df["legal_gap_reason"].value_counts().to_string())
        return 0

    cov = covered(sids)
    remain = [p for p in todo_all if p not in cov and p not in state["empty_pairs"]]
    kb = 26.7 if args.slice == "full" else 4.1
    print(f"事件 {len(per_event)} 筆,涉 {len(sids)} 檔")
    print(f"窗口 D-{args.window} ~ D+{args.post}(去重疊、剔除 <{DATA_START} 與官方缺漏日)"
          f" → stock-day {len(todo_all):,}")
    print(f"已有 {len(todo_all) - len(remain):,}(含 empty {len(state['empty_pairs']):,})"
          f" → **待抓 {len(remain):,}**")
    print(f"節流 {args.rate} req/hr → 約 {len(remain)/args.rate:.1f} 小時"
          f"(不含 blackout 暫停);slice={args.slice} 估增量 {len(remain)*kb/1e6:.2f} GB"
          f"(現有 {dir_gb():.2f} GB)")
    if args.plan_only:
        return 0
    if not remain:
        print("✅ 沒有待抓項目"); return 0

    token = fc.get_token()
    fb.check_token(token)
    os.makedirs(OUTDIR, exist_ok=True)
    if state["started"] is None:
        state["started"] = dt.datetime.now().isoformat(timespec="seconds")
    # 節流器:token-bucket 式的共享時槽。單執行緒時 API 延遲(實測 ~1.3s/call)才是瓶頸,
    # 只能跑到 ~2,800/hr;開 --workers 條並發、由這個鎖統一發時槽,才真的跑到 --rate 上限。
    interval = 3600.0 / args.rate
    slot = {"t": time.time()}
    lock = threading.Lock()

    def one(pair):
        sid, date = pair
        with lock:                                          # 全域速率閘門:每 interval 秒放行一個
            now = time.time()
            slot["t"] = max(now, slot["t"]) + interval
            wait = slot["t"] - interval - now
        if wait > 0:
            time.sleep(wait)
        try:
            return pair, fb.fetch_raw_branch(token, sid, date), None
        except Exception as e:                              # noqa: BLE001
            return pair, None, f"{type(e).__name__}: {e}"

    buf, n_ok, n_empty, done = {}, 0, 0, 0
    t0 = time.time()
    if args.max_calls:
        remain = remain[:args.max_calls]

    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for c0 in range(0, len(remain), FLUSH_EVERY):
            chunk = remain[c0:c0 + FLUSH_EVERY]
            while (not args.no_blackout) and in_blackout():
                print(f"⏸ blackout(台北 {BLACKOUT[0]}–{BLACKOUT[1]},讓路給 nightly),10 分鐘後再看…",
                      flush=True)
                time.sleep(600)
            if dir_gb() > args.halt_gb:
                print(f"⛔ HALT:{OUTDIR} 已達 {dir_gb():.2f} GB > --halt-gb {args.halt_gb}"); break

            for (sid, date), raw, err in pool.map(one, chunk):
                if err:
                    print(f"   ⚠ {sid} {date} 例外 {err};略過不重試", flush=True)
                if raw is None or raw.empty:
                    state["empty_pairs"].add((sid, date))    # 停牌/處置分盤/缺漏 → 記下不再重試
                    n_empty += 1
                else:
                    agg = fb.aggregate(raw)
                    if args.slice == "top25":
                        from deep_backfill_branch import slice_brokers
                        agg = slice_brokers(agg)
                    buf.setdefault(sid, []).append(agg)
                    n_ok += 1
                done += 1
                state["fetched"] = state.get("fetched", 0) + 1
                state["last"] = f"{sid} {date}"
            flush(buf, args.slice); save_state(state)
            el = time.time() - t0
            print(f"  [{done:,}/{len(remain):,}] ok {n_ok:,} empty {n_empty:,} "
                  f"| {dir_gb():.2f} GB | {el/3600:.2f}h | {done/max(el,1)*3600:,.0f} req/hr "
                  f"| ETA {(len(remain)-done)*el/max(done,1)/3600:.1f}h", flush=True)
    flush(buf, args.slice)
    save_state(state)
    print(f"本輪:成功 {n_ok:,}、空回應 {n_empty:,};目錄 {dir_gb():.2f} GB")

    cov_df, miss_df = verify(per_event, args.window, args.post, state["empty_pairs"])
    print(f"覆蓋核對:平均 {cov_df['coverage_pct'].mean():.2f}%,"
          f"完整事件 {int((cov_df['coverage_pct'] >= 99.999).sum())}/{len(cov_df)}")
    print(f"→ {COV_CSV} / {MISS_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
