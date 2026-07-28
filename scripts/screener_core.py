"""
screener_core.py — 篩選機共用核心(screener.py 與 screener_selftest.py 共用同一組函式)
所有口徑照定案回測規格「逐位」實作,禁止優化。單位:margin_balance_shares 存「股」,張=股/1000。
"""
import os
import numpy as np
import pandas as pd

ADJ_DIR = "data/daily_adj"
DAILY_DIR = "data/daily"
REV_DIR = "data/revenue"

# 延伸日的最低參與檔數。universe ≈ 2,100,成分(≥130 列)≈ 1,930;
# 1500 ≈ universe 七成。日線夜更若被配額守衛早停(如 2026-07-27 只覆蓋 811 檔),
# 等權平均會建在半個市場上 → 該日寧可棄算,讓 regime 停在前一有效日(過期但誠實)。
EXT_MIN_N = 1500


def universe_ids() -> list:
    return pd.read_csv("config/universe.csv", dtype=str)["id"].astype(str).tolist()


def clean_adj(sid: str):
    """主序列:daily_adj 清洗(close>0 & open>0 & Trading_Volume>0,依日期排序)。"""
    p = f"{ADJ_DIR}/{sid}.csv"
    if not os.path.exists(p):
        return None
    d = pd.read_csv(p, dtype=str)
    for c in ("open", "close", "Trading_Volume"):
        if c not in d.columns:
            return None
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d[(d["close"] > 0) & (d["open"] > 0) & (d["Trading_Volume"] > 0)].copy()
    d = d.dropna(subset=["open", "close", "Trading_Volume"])
    return d.sort_values("date").reset_index(drop=True)


def yoy_asof(sid: str, dates: pd.Series) -> pd.Series:
    """月營收公布日=次月10日(日曆日);merge_asof backward 對映到清洗後日期。只用有限 yoy。"""
    p = f"{REV_DIR}/{sid}.csv"
    n = len(dates)
    if not os.path.exists(p):
        return pd.Series([np.nan] * n)
    r = pd.read_csv(p, dtype=str)
    if "yoy" not in r.columns or "revenue_month" not in r.columns:
        return pd.Series([np.nan] * n)
    r["yoy"] = pd.to_numeric(r["yoy"], errors="coerce")
    r = r[np.isfinite(r["yoy"])].copy()
    if r.empty:
        return pd.Series([np.nan] * n)

    def pub(ym):
        y, m = map(int, str(ym).split("-"))
        nxt = pd.Period(f"{y}-{m:02d}", freq="M") + 1
        return pd.Timestamp(f"{nxt.year}-{nxt.month:02d}-10")

    r["pub"] = r["revenue_month"].map(pub)
    r = r.sort_values("pub")[["pub", "yoy"]].rename(columns={"pub": "date"})
    left = pd.DataFrame({"date": pd.to_datetime(pd.Series(list(dates)))})
    left["_ord"] = range(n)
    left = left.sort_values("date")
    m = pd.merge_asof(left, r, on="date", direction="backward")
    return m.sort_values("_ord")["yoy"].reset_index(drop=True)


def margin_on_adj(sid: str, adj_dates) -> pd.Series:
    """data/daily 的 margin_balance_shares reindex 到清洗後 adj 日期(缺=NaN,不 ffill)。"""
    p = f"{DAILY_DIR}/{sid}.csv"
    if not os.path.exists(p):
        return pd.Series([np.nan] * len(adj_dates))
    dd = pd.read_csv(p, dtype=str)
    if "margin_balance_shares" not in dd.columns:
        return pd.Series([np.nan] * len(adj_dates))
    dd["margin_balance_shares"] = pd.to_numeric(dd["margin_balance_shares"], errors="coerce")
    s = dd.drop_duplicates("date").set_index("date")["margin_balance_shares"]
    return s.reindex(list(adj_dates)).reset_index(drop=True)


def breakout_signals(d: pd.DataFrame, yoy: pd.Series,
                     lookback=60, yoy_min=0.15, spacing=20, i_lo=65, i_hi_off=22) -> list:
    """成立訊號索引:a) close>=前60日最高(不含i) b) yoy>=0.15 c) 距上次>=20交易日。i∈[i_lo, n-i_hi_off)。"""
    c = d["close"].to_numpy(dtype=float)
    n = len(c)
    if n <= i_lo + i_hi_off:
        return []
    prior = pd.Series(c).rolling(lookback).max().shift(1).to_numpy()   # max(i-60..i-1)
    yv = pd.to_numeric(yoy, errors="coerce").to_numpy(dtype=float)
    hi = n - i_hi_off
    mask = np.zeros(n, dtype=bool)
    idx = np.arange(i_lo, hi)
    ok = (c[idx] >= prior[idx]) & np.isfinite(prior[idx]) & np.isfinite(yv[idx]) & (yv[idx] >= yoy_min)
    mask[idx[ok]] = True
    out, last = [], -10**9
    for i in np.nonzero(mask)[0]:
        if i - last >= spacing:
            out.append(int(i))
            last = i
    return out


def v1_events(d: pd.DataFrame, margin: pd.Series, cfg: dict,
              spacing=10, i_lo=25, i_hi_off=22) -> list:
    """V1 斷頭事件索引(不含 maintenance 閘;那是警示層)。i∈[i_lo, n-i_hi_off)。"""
    c = d["close"].to_numpy(dtype=float)
    n = len(c)
    if n <= i_lo + i_hi_off:
        return []
    # margin_balance_shares 已遷移為「股」(schema 鐵則);yaml 閾值是「張」→ 換算成張再比(股/1000)。
    m = pd.to_numeric(margin, errors="coerce").to_numpy(dtype=float) / 1000.0
    roll20 = pd.Series(c).rolling(20).max().to_numpy()                 # 含當日 max(i-19..i)
    drop_pct = cfg["drop_pct"]; drop_abs = cfg["drop_abs_lots"]
    min_bal = cfg["min_balance_lots"]; dd20 = cfg["drawdown_20d"]
    out, last = [], -10**9
    for i in range(i_lo, n - i_hi_off):
        m5 = m[i - 5]; mi = m[i]
        if not (np.isfinite(m5) and np.isfinite(mi) and m5 > 0):
            continue
        drop = m5 - mi
        if not (drop / m5 >= drop_pct and drop >= drop_abs and m5 >= min_bal):
            continue
        hi20 = roll20[i]
        if not (np.isfinite(hi20) and hi20 > 0 and (c[i] / hi20 - 1.0) <= dd20):
            continue
        if cfg.get("require_up_close", True) and not (c[i] > c[i - 1]):
            continue
        if i - last >= spacing:
            out.append(i)
            last = i
    return out


def build_market_index(min_rows=130, winsor=0.14, ma=120, min_periods=60) -> pd.DataFrame:
    """
    等權市場指數(定案口徑):
      成分 = universe ∩ daily_adj 存在 ∩ 清洗後 >=130 列。
      個股 log 報酬取在「清洗後連續陣列」上(自然跨缺口)。
      指數日報酬 = 當日有報酬成分股 log報酬 的算術平均,排除 |log報酬|>=0.14。
      index = 日均 log報酬累加(起點0);regime = index > 120日滾動均(min_periods=60)。
    """
    acc = {}   # date -> [sum, cnt]
    for sid in universe_ids():
        d = clean_adj(sid)
        if d is None or len(d) < min_rows:
            continue
        c = d["close"].to_numpy(dtype=float)
        lr = np.diff(np.log(c))                      # 清洗後連續陣列上的 log 報酬
        dates = d["date"].to_numpy()[1:]
        good = np.abs(lr) < winsor                   # |log報酬| >= 0.14 不計入
        for dt, r in zip(dates[good], lr[good]):
            a = acc.setdefault(dt, [0.0, 0])
            a[0] += r; a[1] += 1
    if not acc:
        return pd.DataFrame(columns=["date", "mkt_logret", "index", "ma120", "regime"])
    rows = sorted(acc.items())
    df = pd.DataFrame({"date": [x[0] for x in rows],
                       "mkt_logret": [x[1][0] / x[1][1] for x in rows]})
    df["index"] = df["mkt_logret"].cumsum()
    df["ma120"] = df["index"].rolling(ma, min_periods=min_periods).mean()
    df["regime"] = df["index"] > df["ma120"]         # NaN(未滿min_periods)→ False
    df.loc[df["ma120"].isna(), "regime"] = False
    return df


def extend_market_index(idx_df, min_rows=130, winsor=0.14, ma=120, min_periods=60):
    """
    market_index 的 raw 尾端延伸(唯讀,嚴禁寫回 market_index.csv)。
    基底 index 建在 daily_adj 上(週更),尾端會落後日線;此函式把 adj_last 之後的交易日
    用 data/daily 的 raw close 補上,供當晚 regime 判讀。

      成分 = 與 build_market_index 完全相同(universe ∩ daily_adj 存在 ∩ 清洗後 >=130 列)。
      逐日 mkt_logret = 成分股 ln(close_t/close_{t-1}) 等權平均;
        t 或 t-1 缺 close(檔案缺列 / close<=0 / open<=0 / 量=0)該檔當日剔除,分母跟著減;
        |log報酬| >= winsor 亦剔除(同 build_market_index)。
      index_t = index_{t-1} + mkt_logret_t(加性;基底 index 就是日均 log 報酬累加)
      ma120 以「adj 歷史 + raw 延伸」合併序列滾動重算;regime_t = index_t > ma120_t。
      延伸列 provisional=True(基底列 False)。

    回 (ext_df, n_ext, adj_last)。逐日參與檔數放在 ext_df.attrs["ext_counts"]。
    """
    base = idx_df.copy()
    if "provisional" not in base.columns:
        base["provisional"] = False
    if base.empty:
        base.attrs["ext_counts"] = {}
        return base, 0, None
    base["date"] = base["date"].astype(str)
    base["index"] = pd.to_numeric(base["index"], errors="coerce")
    adj_last = base["date"].iloc[-1]

    closes = {}   # sid -> Series(date -> raw close),只留 >= adj_last
    for sid in universe_ids():
        d = clean_adj(sid)
        if d is None or len(d) < min_rows:
            continue                                  # 成分判定與 build_market_index 一致
        p = f"{DAILY_DIR}/{sid}.csv"
        if not os.path.exists(p):
            continue
        r = pd.read_csv(p, dtype=str)
        vcol = "volume_shares" if "volume_shares" in r.columns else "Trading_Volume"  # daily 與 daily_adj 欄名不同
        if not {"open", "close", vcol}.issubset(r.columns):
            continue
        for c in ("open", "close", vcol):
            r[c] = pd.to_numeric(r[c], errors="coerce")
        r = r[(r["close"] > 0) & (r["open"] > 0) & (r[vcol] > 0)]
        r = r[r["date"].astype(str) >= adj_last]
        if r.empty:
            continue
        closes[sid] = r.drop_duplicates("date").set_index("date")["close"]

    ext_dates = sorted({dt for s in closes.values() for dt in s.index if dt > adj_last})
    if not ext_dates:
        base.attrs["ext_counts"] = {}
        return base, 0, adj_last

    days = [adj_last] + ext_dates
    rets, counts, skipped, used = [], {}, [], []
    for i in range(1, len(days)):
        t, prev = days[i], days[i - 1]
        tot, n = 0.0, 0
        for s in closes.values():
            ct = s.get(t); cp = s.get(prev)
            if ct is None or cp is None or not (np.isfinite(ct) and np.isfinite(cp)):
                continue
            lr = float(np.log(ct / cp))
            if abs(lr) >= winsor:
                continue
            tot += lr; n += 1
        counts[t] = n
        if n < EXT_MIN_N:                             # 日線覆蓋不足 → 棄算,延伸停在前一有效日
            skipped.append((t, n))
            break
        used.append(t)
        rets.append(tot / n)

    if not used:
        base.attrs["ext_counts"] = counts
        base.attrs["ext_skipped"] = skipped
        return base, 0, adj_last
    ext_dates = used

    lvl = float(base["index"].iloc[-1])
    idx_vals = []
    for r in rets:
        lvl = lvl + r                                 # 定案:加性遞推(基底是 log 報酬 cumsum,量綱一致)
        idx_vals.append(lvl)

    ext = pd.DataFrame({"date": ext_dates, "mkt_logret": rets, "index": idx_vals,
                        "ma120": np.nan, "regime": False, "provisional": True})
    out = pd.concat([base, ext], ignore_index=True)
    out["ma120"] = pd.to_numeric(out["index"], errors="coerce").rolling(ma, min_periods=min_periods).mean()
    out["regime"] = out["index"] > out["ma120"]
    out.loc[out["ma120"].isna(), "regime"] = False
    out.attrs["ext_counts"] = counts
    out.attrs["ext_skipped"] = skipped
    return out, len(ext_dates), adj_last


def adj_last_date():
    """daily_adj 目錄的最新資料日(只讀各檔尾端 4KB 取 max,不載入整檔)。無檔案回 None。"""
    if not os.path.isdir(ADJ_DIR):
        return None
    latest = None
    with os.scandir(ADJ_DIR) as it:
        for e in it:
            if not e.name.endswith(".csv"):
                continue
            try:
                with open(e.path, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    f.seek(max(0, size - 4096))
                    lines = f.read().decode("utf-8-sig", "ignore").strip().splitlines()
            except OSError:
                continue
            if not lines:
                continue
            d = lines[-1].split(",")[0].strip()        # 尾端切片可能截半行,故只取最後一行
            if len(d) == 10 and d[4] == "-" and (latest is None or d > latest):
                latest = d
    return latest
