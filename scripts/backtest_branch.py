"""
backtest_branch.py — 分點回測資料生成(突破∧營收事件抽樣 → 回補窗 → 只存精簡指標)

目的:測 Stage 2 分點層能否加值,特別是「2022 空頭的虧損是不是隔日沖 churn 撐的假突破」。
**不存原始逐分點**(單日一檔就 ~5000 列,存了 repo 會爆);原始算完即丟,每(股,日)只留 6 個指標。

事件定義(確定性,seed=42 可重現):
  close ≥ 前 60 交易日最高(創 60 日新高) ∧ as-of 最近「已公布」月營收 yoy ≥ 0.15
  同股兩事件至少隔 20 交易日;event_date ≥ 2021-07-01。
  * 無 lookahead:月營收 M 視為「次月 11 日」才可得(對齊 fetch_revenue 的 PUBLISH_DAY 守衛)。
  * 交易日曆取自 data/daily/2330.csv 的日期(calendar.csv 只到 2024,涵蓋不到 2021;
    且本任務不得改動其他 dataset)。

回補窗:[max(2021-06-30, event−45 交易日), min(最後交易日, event+25 交易日)]
  45 進場前給無 lookahead 濾網,25 進場後給出場擇時測試;同檔多事件取聯集去重。

產出 data/branch_backtest/{id}.csv(utf-8-sig, append-dedup on date):
  date, total_buy, top10_net_buy, top10_conc, next_day_reversal, foreign_net_share

用法:
  python scripts/backtest_branch.py --plan      # 只抽樣+算窗+印量級,不抓
  python scripts/backtest_branch.py             # 回補(可續傳;中斷/重開機重跑即接續)
需要:FINMIND_TOKEN;data/daily、data/revenue、config/universe.csv
"""

import os
import sys
import json
import time
import argparse
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_branch as fb          # 沿用既有單日分點抓取 + aggregate,不重寫

OUT_DIR = "data/branch_backtest"
SAMPLE = f"{OUT_DIR}/_sample.csv"
MANIFEST = f"{OUT_DIR}/_manifest.json"
CKPT = ".backfill_checkpoint_btbranch.txt"     # gitignored

SEED = 42
EVENT_START = "2021-07-01"
BRANCH_MIN = "2021-06-30"      # FinMind 分點資料起點
HIGH_WIN = 60
YOY_MIN = 0.15
MIN_GAP_TD = 20
LOOKBACK_TD = 45
FORWARD_TD = 25
N_2022 = 500
N_OTHER = 500
FOREIGN_KEYS = ["摩根", "美林", "瑞銀", "高盛", "花旗", "野村", "法興", "麥格理", "美商"]
THROTTLE = 0.35
STOP_RATIO = 0.9
GUARD_EVERY = 50               # 每 ≤50 次抓取檢查一次用量


# ---------- 交易日曆 ----------

def trading_days() -> list:
    """交易日曆取自 2330 日線(最可靠的『哪天有開市』來源;calendar.csv 只到 2024)。"""
    d = pd.read_csv("data/daily/2330.csv", usecols=["date"], dtype=str)
    return sorted(d["date"].astype(str).unique())


# ---------- 事件 ----------

def revenue_asof(sid: str):
    """回 [(可得日, revenue_month, yoy)] 已排序;月營收 M 於次月 11 日才視為已公布(無 lookahead)。"""
    p = f"data/revenue/{sid}.csv"
    if not os.path.exists(p):
        return []
    d = pd.read_csv(p, dtype=str)
    rows = []
    for _, r in d.iterrows():
        ym = str(r["revenue_month"])
        try:
            y, m = map(int, ym.split("-"))
        except ValueError:
            continue
        nxt = pd.Period(f"{y}-{m:02d}", freq="M") + 1
        avail = f"{nxt.year}-{nxt.month:02d}-11"
        yoy = pd.to_numeric(r.get("yoy"), errors="coerce")
        rows.append((avail, ym, yoy))
    rows.sort()
    return rows


def events_for(sid: str, td_idx: dict) -> list:
    """回該檔所有『突破∧營收』事件 [(date, yoy)],已套 20 交易日間隔。"""
    p = f"data/daily/{sid}.csv"
    if not os.path.exists(p):
        return []
    d = pd.read_csv(p, usecols=["date", "close"], dtype=str)
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    d = d.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    if len(d) < HIGH_WIN + 2:
        return []
    prior_max = d["close"].shift(1).rolling(HIGH_WIN).max()
    d["is_high"] = d["close"] >= prior_max

    rev = revenue_asof(sid)
    if not rev:
        return []
    avails = [r[0] for r in rev]

    out, last_i = [], None
    for _, r in d[d["is_high"]].iterrows():
        dt = str(r["date"])
        if dt < EVENT_START or dt not in td_idx:
            continue
        # as-of 最近已公布營收
        j = np.searchsorted(avails, dt, side="right") - 1
        if j < 0:
            continue
        yoy = rev[j][2]
        if pd.isna(yoy) or yoy < YOY_MIN:
            continue
        i = td_idx[dt]
        if last_i is not None and i - last_i < MIN_GAP_TD:
            continue      # 同股兩事件至少隔 20 交易日
        out.append((dt, float(yoy)))
        last_i = i
    return out


def build_sample(tds: list, td_idx: dict) -> pd.DataFrame:
    uni = pd.read_csv("config/universe.csv", dtype=str)["id"].astype(str).tolist()
    rows = []
    for n, sid in enumerate(uni, 1):
        for dt, yoy in events_for(sid, td_idx):
            rows.append({"id": sid, "event_date": dt, "yoy": round(yoy, 6)})
        if n % 400 == 0:
            print(f"   掃事件 {n}/{len(uni)} … 累計 {len(rows)}")
    ev = pd.DataFrame(rows)
    if ev.empty:
        sys.exit("❌ 找不到任何事件,停下回報(不硬湊)")
    ev["year"] = ev["event_date"].str[:4]
    ev["is_2022"] = ev["year"] == "2022"
    print(f"   全部事件 {len(ev)};2022={int(ev.is_2022.sum())} 其餘={int((~ev.is_2022).sum())}")
    print(f"   年份分布:{ev['year'].value_counts().sort_index().to_dict()}")

    rng = np.random.default_rng(SEED)

    def pick(sub, n):
        if len(sub) <= n:
            return sub
        idx = rng.choice(len(sub), size=n, replace=False)
        return sub.iloc[np.sort(idx)]

    s = pd.concat([pick(ev[ev.is_2022].reset_index(drop=True), N_2022),
                   pick(ev[~ev.is_2022].reset_index(drop=True), N_OTHER)],
                  ignore_index=True).sort_values(["id", "event_date"]).reset_index(drop=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    s.to_csv(SAMPLE, index=False, encoding="utf-8-sig")
    print(f"✅ {SAMPLE}:{len(s)} 事件(2022={int(s.is_2022.sum())} / 其餘={int((~s.is_2022).sum())})")
    return s


# ---------- 窗 ----------

def windows(sample: pd.DataFrame, tds: list, td_idx: dict) -> dict:
    """每檔要抓的日期集合(多事件窗聯集去重)。"""
    last = tds[-1]
    plan = {}
    for _, r in sample.iterrows():
        sid, dt = str(r["id"]), str(r["event_date"])
        i = td_idx[dt]
        lo = max(0, i - LOOKBACK_TD)
        hi = min(len(tds) - 1, i + FORWARD_TD)
        days = [d for d in tds[lo:hi + 1] if BRANCH_MIN <= d <= last]
        plan.setdefault(sid, set()).update(days)
    return {k: sorted(v) for k, v in plan.items()}


# ---------- 指標 ----------

def day_metrics(agg: pd.DataFrame) -> dict:
    """單日分點彙總 → 精簡指標(不含 reversal,需隔日)。回 dict + top10 broker set。"""
    total_buy = int(agg["buy_shares"].sum())
    top10 = agg.nlargest(10, "net_shares")
    t10_net = int(top10["net_shares"].sum())
    conc = round(t10_net / total_buy, 6) if total_buy > 0 else ""
    net_abs = agg["net_shares"].abs().sum()
    is_f = agg["broker_name"].fillna("").apply(lambda s: any(k in s for k in FOREIGN_KEYS))
    f_share = round(agg.loc[is_f, "net_shares"].abs().sum() / net_abs, 6) if net_abs > 0 else ""
    return {"total_buy": total_buy, "top10_net_buy": t10_net, "top10_conc": conc,
            "foreign_net_share": f_share}, set(top10["broker_id"].astype(str)), t10_net


def usage(token):
    try:
        import requests
        j = requests.get(fb.USERINFO_URL, headers={"Authorization": f"Bearer {token}"},
                         timeout=30).json()
        return j.get("user_count"), j.get("api_request_limit")
    except Exception:
        return None, None


def load_ckpt() -> set:
    if not os.path.exists(CKPT):
        return set()
    with open(CKPT, encoding="utf-8") as f:
        return {l.strip() for l in f if l.strip()}


def mark(sid: str):
    with open(CKPT, "a", encoding="utf-8") as f:
        f.write(sid + "\n")


def process_stock(token: str, sid: str, days: list, td_idx: dict) -> int:
    """抓整個窗(block)、算指標、寫精簡檔。回抓取次數。原始資料算完即丟。"""
    per_day = {}      # date -> (metrics, top10_ids, t10_net)
    net_by_day = {}   # date -> {broker_id: net}
    calls = 0
    for k, d in enumerate(days, 1):
        raw = fb.fetch_raw_branch(token, sid, d)
        calls += 1
        time.sleep(THROTTLE)
        if raw.empty:
            continue                     # 抓不到 → 該日無指標(記 NaN 由缺列表示),續跑
        agg = fb.aggregate(raw)
        m, t10, t10net = day_metrics(agg)
        per_day[d] = (m, t10, t10net)
        net_by_day[d] = dict(zip(agg["broker_id"].astype(str), agg["net_shares"]))
        if k % GUARD_EVERY == 0:
            u, lim = usage(token)
            if u and lim and u > lim * STOP_RATIO:
                raise RuntimeError(f"quota {u}/{lim}")   # 中止此檔(不 mark),下輪重抓

    if not per_day:
        return calls
    rows = []
    ds = sorted(per_day)
    for i, d in enumerate(ds):
        m, t10, t10net = per_day[d]
        nxt = ds[i + 1] if i + 1 < len(ds) else None
        rev = ""
        # 只有「窗內下一筆剛好是日曆上相鄰的下一個交易日」才算 reversal。
        # 同檔多事件的窗是聯集、中間有斷層,跨斷層(可能隔數月)算翻轉毫無意義 → 留 NaN。
        adjacent = (nxt is not None and d in td_idx and nxt in td_idx
                    and td_idx[nxt] == td_idx[d] + 1)
        if adjacent and t10net > 0:
            nb = net_by_day.get(nxt, {})
            dumped = sum(max(0, -nb.get(b, 0)) for b in t10)   # 隔日被倒掉量
            rev = round(dumped / t10net, 6)
        rows.append({"date": d, **m, "next_day_reversal": rev})
    df = pd.DataFrame(rows)[["date", "total_buy", "top10_net_buy", "top10_conc",
                             "next_day_reversal", "foreign_net_share"]]
    path = f"{OUT_DIR}/{sid}.csv"
    if os.path.exists(path):
        old = pd.read_csv(path, dtype=str)
        df = pd.concat([old, df.astype(str)], ignore_index=True)
        df = df.drop_duplicates("date", keep="last")
    df.sort_values("date").to_csv(path, index=False, encoding="utf-8-sig")
    return calls


def write_manifest(sample, plan, done, total_pairs, status):
    all_days = sorted({d for v in plan.values() for d in v})
    json.dump({
        "events": int(len(sample)),
        "events_2022": int(sample["is_2022"].sum()),
        "events_other": int((~sample["is_2022"]).sum()),
        "stocks": len(plan),
        "date_range": [all_days[0], all_days[-1]] if all_days else None,
        "stock_day_pairs_total": total_pairs,
        "stocks_done": len(done),
        "pairs_done": int(sum(len(plan[s]) for s in done if s in plan)),
        "status": status,
        "seed": SEED,
        "definition": {"high_window": HIGH_WIN, "yoy_min": YOY_MIN,
                       "min_gap_trading_days": MIN_GAP_TD,
                       "window": [-LOOKBACK_TD, FORWARD_TD],
                       "revenue_asof": "月營收 M 於次月 11 日才視為已公布(無 lookahead)"},
    }, open(MANIFEST, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser(description="分點回測資料生成")
    ap.add_argument("--plan", action="store_true", help="只抽樣+算窗+印量級,不抓")
    ap.add_argument("--resample", action="store_true", help="重抽事件(覆蓋 _sample.csv)")
    ap.add_argument("--max-hours", type=float, default=11.0, help="本次最長跑多久(過夜用)")
    args = ap.parse_args()

    token = fb.get_token()
    fb.check_token(token)
    os.makedirs(OUT_DIR, exist_ok=True)

    tds = trading_days()
    td_idx = {d: i for i, d in enumerate(tds)}
    print(f"交易日曆:{len(tds)} 日,{tds[0]} → {tds[-1]}")

    if args.resample or not os.path.exists(SAMPLE):
        sample = build_sample(tds, td_idx)
    else:
        sample = pd.read_csv(SAMPLE, dtype={"id": str})
        sample["is_2022"] = sample["is_2022"].astype(str).str.lower().eq("true")
        print(f"沿用 {SAMPLE}:{len(sample)} 事件")

    plan = windows(sample, tds, td_idx)
    total_pairs = sum(len(v) for v in plan.values())
    done = load_ckpt()
    todo = [s for s in sorted(plan) if s not in done]
    remain = sum(len(plan[s]) for s in todo)
    print(f"\n窗:{len(plan)} 檔 / {total_pairs} 個(股,日) = 預估 {total_pairs} call")
    print(f"已完成 {len(done)} 檔;待抓 {len(todo)} 檔 / {remain} call")
    write_manifest(sample, plan, done, total_pairs, "planned" if args.plan else "running")
    if args.plan:
        print("\n--plan 模式,不抓取。")
        return

    t0 = time.time()
    calls = 0

    def wait_quota(need: int) -> bool:
        """等到額度夠跑完整個窗才開跑(保證整檔原子性)。回 False = 該收工了。"""
        while True:
            if (time.time() - t0) / 3600 > args.max_hours:
                print(f"⏸ 已達 --max-hours {args.max_hours},停下續傳")
                return False
            u, lim = usage(token)
            if not u or not lim or u + need <= lim * STOP_RATIO:
                return True
            print(f"   ⏳ 用量 {u}/{lim},等額度回補(need {need})… sleep 5m", flush=True)
            time.sleep(300)

    for n, sid in enumerate(todo, 1):
        days = plan[sid]
        if not wait_quota(len(days)):
            break
        u, lim = usage(token)
        try:
            c = process_stock(token, sid, days, td_idx)
            calls += c
            mark(sid)
            if n % 10 == 0 or n == len(todo):
                print(f"   [{n}/{len(todo)}] {sid} ({len(days)}d) 累計 {calls} call;用量 {u}/{lim}",
                      flush=True)
        except RuntimeError as e:
            print(f"⏸ {sid} 中途觸及用量上限({e}),此檔不 mark、下輪重抓;停下續傳")
            break

    done = load_ckpt()
    status = "complete" if len(done) >= len(plan) else "partial"
    write_manifest(sample, plan, done, total_pairs, status)
    u, lim = usage(token)
    print(f"\n本輪 {calls} call;完成 {len(done)}/{len(plan)} 檔;狀態 {status};用量 {u}/{lim}")


if __name__ == "__main__":
    main()
