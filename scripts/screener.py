"""
screener.py — 生產版籌碼篩選機(每晚訊號報告 + 模型組合追蹤)
忠實實作 config/screener.yaml 的定案回測規格,禁止優化。核心訊號函式與 selftest 共用 screener_core。

用法:
  python scripts/screener.py                 # 以最新資料日跑
  python scripts/screener.py --date 2024-03-15   # dry-run 歷史日(兩階段驗證用)
  python scripts/screener.py --rebuild-index     # 全量重算 market_index
產出:data/screener/{report_YYYY-MM-DD.md, latest.md, state.json, trades.csv, market_index.csv}
"""
import os
import json
import argparse
import numpy as np
import pandas as pd
import yaml
import screener_core as sc

DIR = "data/screener"
STATE = f"{DIR}/state.json"
TRADES = f"{DIR}/trades.csv"
MKT = f"{DIR}/market_index.csv"
FEES = 0.004425      # 定案:毛報酬扣 0.4425% 手續稅費
STALE_MOVE = 0.11    # adj-stale 延伸窗內 raw 單日 |漲跌|>11% → 需人工確認


# ---------- market index / regime ----------

def load_or_build_index(rebuild: bool) -> pd.DataFrame:
    if rebuild or not os.path.exists(MKT):
        df = sc.build_market_index()
        os.makedirs(DIR, exist_ok=True)
        df.to_csv(MKT, index=False, encoding="utf-8-sig")
        return df
    return pd.read_csv(MKT, dtype={"date": str})


# ---------- 延伸(adj-stale)價格序列 ----------

def extended_series(sid: str):
    """回 (df[date,open,close], stale_bool)。adj 週更;其最後日之後用 raw 依比例延伸。"""
    a = sc.clean_adj(sid)
    if a is None or a.empty:
        return None, False
    a = a[["date", "open", "close"]].copy()
    last_adj = a["date"].iloc[-1]
    rp = f"{sc.DAILY_DIR}/{sid}.csv"
    stale = False
    if os.path.exists(rp):
        r = pd.read_csv(rp, dtype=str)
        for c in ("open", "close"):
            r[c] = pd.to_numeric(r[c], errors="coerce")
        r = r[(r["close"] > 0) & (r["open"] > 0)].sort_values("date")
        ov = r[r["date"] == last_adj]
        ext = r[r["date"] > last_adj]
        if len(ov) and len(ext):
            fo = a["open"].iloc[-1] / ov["open"].iloc[0]
            fc = a["close"].iloc[-1] / ov["close"].iloc[0]
            e = pd.DataFrame({"date": ext["date"].values,
                              "open": ext["open"].values * fo,
                              "close": ext["close"].values * fc})
            chg = ext["close"].pct_change().abs()
            if (chg > STALE_MOVE).any():
                stale = True
            a = pd.concat([a, e], ignore_index=True)
    return a.reset_index(drop=True), stale


# ---------- ranking ----------

def revenue_pub(sid: str) -> pd.DataFrame:
    p = f"{sc.REV_DIR}/{sid}.csv"
    if not os.path.exists(p):
        return pd.DataFrame(columns=["pub", "ym", "yoy"])
    r = pd.read_csv(p, dtype=str)
    r["yoy"] = pd.to_numeric(r["yoy"], errors="coerce")

    def pub(ym):
        y, m = map(int, str(ym).split("-"))
        nx = pd.Period(f"{y}-{m:02d}", freq="M") + 1
        return pd.Timestamp(f"{nx.year}-{nx.month:02d}-10")

    r = r[np.isfinite(r["yoy"])].copy()
    if r.empty:
        return pd.DataFrame(columns=["pub", "ym", "yoy"])
    r["pub"] = r["revenue_month"].map(pub)
    r["ym"] = r["revenue_month"]
    return r.sort_values("pub")[["pub", "ym", "yoy"]].reset_index(drop=True)


def rank_score(sid, sig_date, first_bar, rev, cfg) -> dict:
    """+2 驚喜(30日曆天內驚喜公布) +1 新上市;回 dict(score, surprise, young, yoy)。"""
    rk = cfg["ranking"]
    sd = pd.Timestamp(sig_date)
    # 當前 as-of yoy
    asof = rev[rev["pub"] <= sd]
    yoy_now = asof["yoy"].iloc[-1] if len(asof) else np.nan
    # 驚喜:30 日曆天內有公布,且該月 yoy - 前3月yoy中位 >= +0.20 且 yoy>0
    surprise = False
    recent = rev[(rev["pub"] <= sd) & (rev["pub"] >= sd - pd.Timedelta(days=rk["surprise_fresh_days"]))]
    for _, row in recent.iterrows():
        prior3 = rev[rev["pub"] < row["pub"]].tail(3)
        if len(prior3) == 3 and prior3["yoy"].notna().all():
            if row["yoy"] > 0 and (row["yoy"] - prior3["yoy"].median()) >= rk["surprise_pp"]:
                surprise = True
                break
    # 新上市:上市月齡<=549天(proxy=首個清洗bar)且首日>2015-07-01
    young = False
    fb = pd.Timestamp(first_bar)
    if fb > pd.Timestamp("2015-07-01") and (sd - fb).days <= rk["young_max_days"]:
        young = True
    score = 2 * int(surprise) + 1 * int(young)
    return {"score": score, "surprise": surprise, "young": young, "yoy": yoy_now}


# ---------- churn(僅顯示)----------

def churn_flag(sid: str) -> str:
    p = f"data/branch/{sid}.csv"
    if not os.path.exists(p):
        return "—"
    b = pd.read_csv(p, dtype=str)
    for c in ("buy_shares", "sell_shares", "net_shares"):
        b[c] = pd.to_numeric(b[c], errors="coerce").fillna(0)
    dts = sorted(b["date"].unique())
    if len(dts) < 10:
        return "—"
    dts = dts[-11:]
    ratios = []
    for j in range(len(dts) - 1):
        d0, d1 = dts[j], dts[j + 1]
        g0 = b[b["date"] == d0]
        top10 = g0.nlargest(10, "net_shares")
        tnet = top10["net_shares"].sum()
        if tnet <= 0:
            continue
        nb = dict(zip(b[b["date"] == d1]["broker_id"], b[b["date"] == d1]["net_shares"]))
        dumped = sum(max(0.0, -nb.get(bid, 0)) for bid in top10["broker_id"])
        ratios.append(dumped / tnet)
    if not ratios:
        return "—"
    mean = float(np.mean(ratios))
    return f"⚠️高churn({mean:.2f})" if mean > 0.25 else f"{mean:.2f}"


# ---------- state ----------

def load_state():
    """允許檔頭 // 註解(使用者可手動編輯)。"""
    if not os.path.exists(STATE):
        return {"last_run_date": None, "positions": [], "pending": [], "farm_queue": []}
    lines = [l for l in open(STATE, encoding="utf-8") if not l.lstrip().startswith("//")]
    return json.loads("".join(lines))


def save_state(s):
    os.makedirs(DIR, exist_ok=True)
    hdr = ("// state.json — 模型組合狀態(可手動編輯)\n"
           "// positions: 持倉;pending: 明日開盤待買;farm_queue: 待回補分點的農場代號\n")
    with open(STATE, "w", encoding="utf-8") as f:
        f.write(hdr)
        json.dump(s, f, ensure_ascii=False, indent=2)


# ---------- 價格/流動性/名稱 ----------

def _at(ext, date, col):
    if ext is None:
        return None
    row = ext[ext["date"] == date]
    return float(row[col].iloc[0]) if len(row) else None


def liquidity_20d_median(sid):
    a = sc.clean_adj(sid)
    if a is None or "Trading_money" not in a.columns:
        return None
    v = pd.to_numeric(a["Trading_money"], errors="coerce").dropna().tail(20)
    return float(v.median()) if len(v) else None


def names_map():
    if not os.path.exists("data/info.csv"):
        return {}
    d = pd.read_csv("data/info.csv", dtype=str)
    return {str(r["stock_id"]): (r.get("name", ""), r.get("type", "")) for _, r in d.iterrows()}


def weekday_tdays_until(today, target):
    """未來交易日數的近似:數 today→target 之間的平日(週一~五);未扣未來假日(無未來行事曆)。"""
    t0 = pd.Timestamp(today); t1 = pd.Timestamp(target)
    if t1 <= t0:
        return None
    return int(np.busday_count(t0.date(), (t1 + pd.Timedelta(days=1)).date()))


def daily_val(sid, col, date):
    p = f"data/daily/{sid}.csv"
    if not os.path.exists(p):
        return None
    d = pd.read_csv(p, dtype=str)
    r = d[d["date"] == date]
    if not len(r) or col not in d.columns:
        return None
    return pd.to_numeric(r[col].iloc[0], errors="coerce")


# ---------- 衛星 V4 / V5 ----------

def satellite_v4(today, cal, cal_idx, cfg, names):
    """停券起日在未來 window_tdays 交易日內,且現在券資比>=min_ratio、融券>=min_short_lots 張。"""
    out = []
    import glob
    for f in glob.glob("data/short_suspension/*.csv"):
        sid = os.path.basename(f)[:-4]
        d = pd.read_csv(f, dtype=str)
        if "date" not in d.columns:
            continue
        for _, r in d.iterrows():
            start = str(r["date"])[:10]
            td = weekday_tdays_until(today, start)
            if td is None or td > cfg["window_tdays"]:
                continue
            margin = daily_val(sid, "margin_balance_shares", today)
            short = daily_val(sid, "short_balance_shares", today)
            if margin and short and margin > 0:
                ratio = short / margin
                if ratio >= cfg["min_ratio"] and short >= cfg["min_short_lots"]:
                    out.append(f"{sid} {names.get(sid,('',''))[0]}:停券起 {start}(約 {td} 交易日後)"
                               f"、券資比 {ratio:.2f}、融券 {int(short)} 張")
            break
    return out


def satellite_v5(today, cal, cal_idx, cfg, names):
    """處置迄日在未來 within_tdays 交易日內。"""
    out = []
    p = "data/regulatory/disposition.csv"
    if not os.path.exists(p):
        return out
    d = pd.read_csv(p, dtype=str)
    for _, r in d.iterrows():
        end = str(r.get("period_end", ""))[:10]
        if not end or end < today:
            continue
        td = weekday_tdays_until(today, end)
        if td is None or td > cfg["within_tdays"]:
            continue
        sid = str(r["stock_id"])
        out.append(f"{sid} {names.get(sid,('',''))[0]}:處置迄 {end}(約 {td} 交易日後)")
    return out


# ---------- 農場擴充 ----------

ORIG_WATCHLIST = {"6831", "7795", "2330", "2059", "6278", "2327", "6510", "7769"}


def farm_expand(cands, pending, state):
    import subprocess, sys
    wl_path = "config/watchlist.yaml"
    cfg = yaml.safe_load(open(wl_path, encoding="utf-8"))
    have = {str(t["id"]) for t in cfg.get("tickers", [])}
    cap = yaml.safe_load(open("config/screener.yaml", encoding="utf-8"))["watchlist_farm"]["cap"]
    lookback = yaml.safe_load(open("config/screener.yaml", encoding="utf-8"))["watchlist_farm"]["backfill_lookback_days"]
    # 排序候選前 10 + 本晚新進(pending)
    top10 = [c["id"] for c in cands[:10]]
    entries = [p["id"] for p in pending]
    want = [x for x in dict.fromkeys(top10 + entries) if x not in have]
    room = cap - len(have)
    add = want[:max(0, room)]
    if add:
        info = names_map()
        with open(wl_path, "a", encoding="utf-8") as f:
            for sid in add:
                nm = info.get(sid, ("farm", ""))[0]
                f.write(f'\n  - id: "{sid}"\n    market: {info.get(sid,("",""))[1] or "twse"}\n    note: farm {nm}\n')
        print(f"   farm:watchlist +{len(add)} 檔(cap {cap};原 8 檔保留)")
    # 新增者 + 既有佇列 → 60 日 branch 回補;沒抓到的留佇列
    queue = list(dict.fromkeys(state.get("farm_queue", []) + add))
    todo = [s for s in queue if not os.path.exists(f"data/branch/{s}.csv")]
    if todo:
        try:
            subprocess.run([sys.executable, "scripts/backfill.py", "--tickers", ",".join(todo),
                            "--datasets", "branch", "--lookback-days", str(lookback)], timeout=3600)
        except Exception as e:
            print(f"   farm backfill 異常:{e}")
    state["farm_queue"] = [s for s in queue if not os.path.exists(f"data/branch/{s}.csv")]


# ---------- 報告 ----------

def fmt_pct(x):
    return "—" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{x*100:+.1f}%"


def write_report(date, regime_row, positions, exits, fills, pending, cands,
                 floored, v1, v4, v5, stale_list, latest_json, cal, cal_idx):
    L = [f"# 籌碼篩選機報告 — {date}\n"]
    L.append("## 資料新鮮度")
    if latest_json:
        for k, v in latest_json.get("datasets", {}).items():
            L.append(f"- {k}: 截至 {v.get('through')} ({v.get('status')})")
    L.append("")
    if regime_row is not None:
        on = bool(regime_row["regime"])
        L.append(f"## Regime:{'🟢 開機' if on else '🔴 關機(不進新倉)'}")
        L.append(f"- 等權指數 {float(regime_row['index']):.4f} vs 120MA "
                 f"{float(regime_row['ma120']):.4f}\n" if np.isfinite(float(regime_row.get('ma120') or np.nan))
                 else "- 120MA 未滿 60 日 → 關機\n")
    else:
        L.append("## Regime:⚠️ 無指數資料\n")
    L.append("## 模型組合持倉")
    if positions:
        L.append("| 代號 | 名稱 | 進場日 | 進場價 | 持有交易日 | exit_due | 最新收盤 | 浮動損益 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for p in positions:
            ei = cal_idx.get(p["entry_date"])
            held = (cal_idx.get(date, 0) - ei) if ei is not None else "—"
            cpx = _at(extended_series(p["id"])[0], date, "close")
            fl = (cpx / p["entry_price"] - 1) if cpx else None
            L.append(f"| {p['id']} | {p.get('name','')} | {p['entry_date']} | {p['entry_price']:.2f} "
                     f"| {held} | {p.get('exit_due') or 'T+20'} | {cpx if cpx else '—'} | {fmt_pct(fl)} |")
    else:
        L.append("*(無持倉)*")
    L.append("\n## 明日出場")
    L.append("*(出場於 exit_due 當日收盤實現;見上表)*" if positions else "*(無)*")
    L.append("\n## 明日開盤進場 pending")
    L += ([f"- {p['id']} {p.get('name','')}(分 {p['rank_score']})" for p in pending]
          or ["*(無 — 關機/無合格候選/已滿倉)*"])
    L.append("\n## 今日成立訊號候選(依分數/yoy 排序)")
    if cands:
        L.append("| 代號 | 名稱 | 市場 | 收盤 | yoy | 驚喜 | 新上市 | 分數 | churn | 流動性 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for c in cands:
            L.append(f"| {c['id']} | {c['name']} | {c['market']} | {c['close']:.2f} | {fmt_pct(c['yoy'])} "
                     f"| {'✓' if c['surprise'] else ''} | {'✓' if c['young'] else ''} | {c['score']} "
                     f"| {c['churn']} | {'✓' if c['liq_ok'] else '未達'} |")
    else:
        L.append("*(今日無成立訊號)*")
    if floored:
        L.append("\n### 未達流動性/價格線(透明,不進組合)")
        L += [f"- {c['id']} {c['name']}:收盤 {c['close']:.2f} / 20日中位成交值 {(c['liq'] or 0)/1e6:.0f}M"
              for c in floored]
    L.append("\n## 衛星警示(只警示,不建倉)")
    L.append("### V1 融資斷頭")
    L += ([f"- {x}" for x in v1] or ["*(無)*"])
    L.append("### V4 停券回補")
    L += ([f"- {x}" for x in v4] or ["*(無)*"])
    L.append("### V5 處置解除(樂透型:中位為負、僅極小倉)")
    L += ([f"- {x}" for x in v5] or ["*(無)*"])
    L.append("\n## 附註")
    L.append("- 生產衛生層(min_price 10 / 20日中位成交值 3000萬)為 **backtest deviation**,僅擋進場、透明列示。")
    L.append("- V4/V5 的「未來N交易日」用平日數近似(無未來交易所行事曆,未扣未來假日)。")
    L.append("- ⚠️ adj-stale 需人工確認:" + ("、".join(stale_list) if stale_list else "無") + "。")
    txt = "\n".join(L) + "\n"
    os.makedirs(DIR, exist_ok=True)
    open(f"{DIR}/report_{date}.md", "w", encoding="utf-8-sig").write(txt)
    open(f"{DIR}/latest.md", "w", encoding="utf-8-sig").write(txt)


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="以此資料日跑(dry-run 歷史日);省略=最新")
    ap.add_argument("--rebuild-index", action="store_true")
    ap.add_argument("--no-farm", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load(open("config/screener.yaml", encoding="utf-8"))
    me = cfg["main_engine"]; sat = cfg["satellites"]; floors = me["production_floors"]
    names = names_map()

    cal = sorted(pd.read_csv("data/daily/2330.csv", usecols=["date"], dtype=str)["date"].unique())
    cal_idx = {d: i for i, d in enumerate(cal)}
    today = args.date or cal[-1]
    if today not in cal_idx:
        print(f"⚠ {today} 不在交易日曆,結束"); return
    it = cal_idx[today]

    idx = load_or_build_index(args.rebuild_index)
    idx = idx[idx["date"] <= today]
    regime_row = idx.iloc[-1] if len(idx) else None
    regime_on = bool(regime_row["regime"]) if regime_row is not None else False

    latest_json = json.load(open("data/latest.json", encoding="utf-8")) if os.path.exists("data/latest.json") else {}
    state = load_state()
    positions = state.get("positions", [])

    # STAGE 1 fills:昨晚 pending → 今日開盤
    held = {p["id"] for p in positions}
    fills = []
    for pend in state.get("pending", []):
        sid = pend["id"]
        if sid in held:
            continue
        o = _at(extended_series(sid)[0], today, "open")
        if o is None:
            continue
        exit_due = cal[it + me["hold_days"]] if it + me["hold_days"] < len(cal) else None
        positions.append({"id": sid, "name": pend.get("name", ""), "entry_date": today,
                          "entry_price": o, "exit_due": exit_due,
                          "rank_score": pend.get("rank_score"), "signal_date": pend.get("signal_date")})
        held.add(sid); fills.append(sid)

    # STAGE 2 exits
    trades = pd.read_csv(TRADES, dtype=str).to_dict("records") if os.path.exists(TRADES) else []
    keep, exits = [], []
    for p in positions:
        ei = cal_idx.get(p["entry_date"])
        cpx = _at(extended_series(p["id"])[0], today, "close")
        do_exit, reason = False, ""
        if me["hard_stop"] is not None and cpx is not None and cpx < p["entry_price"] * (1 - me["hard_stop"]):
            do_exit, reason = True, "hard_stop"
        elif ei is not None and (it - ei) >= me["hold_days"]:
            do_exit, reason = True, "hold_20"
        if do_exit and cpx is not None:
            gross = cpx / p["entry_price"] - 1
            trades.append({"id": p["id"], "name": p.get("name", ""), "entry_date": p["entry_date"],
                           "entry_price": f"{p['entry_price']:.4f}", "exit_date": today,
                           "exit_price": f"{cpx:.4f}", "reason": reason,
                           "gross_return": f"{gross:.6f}", "net_return": f"{gross - FEES:.6f}"})
            exits.append(p["id"])
        else:
            keep.append(p)
    positions = keep
    if trades:
        pd.DataFrame(trades).drop_duplicates(["id", "entry_date"], keep="last").to_csv(
            TRADES, index=False, encoding="utf-8-sig")

    # STAGE 3 候選 + V1
    cands, stale_list, v1 = [], [], []
    maint = None
    if os.path.exists("data/macro/margin_maintenance.csv"):
        md = pd.read_csv("data/macro/margin_maintenance.csv", dtype=str)
        r = md[md["date"] <= today]
        maint = float(r["TotalExchangeMarginMaintenance"].iloc[-1]) if len(r) else None
    for sid in sc.universe_ids():
        ext, stale = extended_series(sid)
        if ext is None:
            continue
        e = ext[ext["date"] <= today].reset_index(drop=True)
        if len(e) < 66 or e["date"].iloc[-1] != today:
            continue
        yoy = sc.yoy_asof(sid, e["date"])
        margin = sc.margin_on_adj(sid, e["date"])
        v1ev = sc.v1_events(e, margin, sat["v1_margin_flush"], spacing=10, i_lo=25, i_hi_off=0)
        if (v1ev and v1ev[-1] == len(e) - 1 and maint is not None
                and maint < sat["v1_margin_flush"]["maintenance_below"]):
            dd = e["close"].iloc[-1] / pd.Series(e["close"]).rolling(20).max().iloc[-1] - 1
            v1.append(f"{sid} {names.get(sid,('',''))[0]}:回檔 {dd*100:.1f}%、維持率 {maint}")
        sig = sc.breakout_signals(e, yoy, lookback=me["breakout_lookback"], yoy_min=me["yoy_min"],
                                  spacing=me["spacing_days"], i_lo=65, i_hi_off=0)
        if not (sig and sig[-1] == len(e) - 1):
            continue
        nm, mkt = names.get(sid, ("", ""))
        rs = rank_score(sid, today, e["date"].iloc[0], revenue_pub(sid), cfg)
        close = e["close"].iloc[-1]
        liq = liquidity_20d_median(sid)
        liq_ok = (close >= floors["min_price"]) and (liq is not None and liq >= floors["min_liquidity_20d_median_value"])
        if stale:
            stale_list.append(f"{sid} {nm}")
        cands.append({"id": sid, "name": nm, "market": mkt, "close": close, "yoy": rs["yoy"],
                      "surprise": rs["surprise"], "young": rs["young"], "score": rs["score"],
                      "churn": churn_flag(sid), "liq": liq, "liq_ok": liq_ok, "stale": stale})
    cands.sort(key=lambda c: (-c["score"], -(c["yoy"] if c["yoy"] is not None and np.isfinite(c["yoy"]) else -9)))
    floored = [c for c in cands if not c["liq_ok"]]

    # STAGE 4 明日 pending
    pending = []
    if regime_on:
        held = {p["id"] for p in positions}
        free = me["n_slots"] - len(positions)
        for c in cands:
            if len(pending) >= free:
                break
            if c["id"] in held or not c["liq_ok"] or c["stale"]:
                continue
            pending.append({"id": c["id"], "name": c["name"], "rank_score": c["score"], "signal_date": today})

    v4 = satellite_v4(today, cal, cal_idx, sat["v4_short_cover"], names)
    v5 = satellite_v5(today, cal, cal_idx, sat["v5_disposition_release"], names)

    write_report(today, regime_row, positions, exits, fills, pending, cands, floored,
                 v1, v4, v5, stale_list, latest_json, cal, cal_idx)

    state["last_run_date"] = today
    state["positions"] = positions
    state["pending"] = pending
    save_state(state)

    if not args.no_farm:
        farm_expand(cands, pending, state)
        save_state(state)

    print(f"✅ screener {today}:regime {'ON' if regime_on else 'OFF'};候選 {len(cands)}、"
          f"pending {len(pending)}、持倉 {len(positions)}、出場 {len(exits)}、進場 {len(fills)}、"
          f"V1/V4/V5 {len(v1)}/{len(v4)}/{len(v5)}。→ {DIR}/latest.md")


if __name__ == "__main__":
    main()
