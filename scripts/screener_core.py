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
    m = pd.to_numeric(margin, errors="coerce").to_numpy(dtype=float)
    roll20 = pd.Series(c).rolling(20).max().to_numpy()                 # 含當日 max(i-19..i)
    # data/daily 的 margin_balance_shares 實際單位是「張」(FinMind 原樣;名稱雖帶 _shares)。
    # yaml 的 drop_abs_lots / min_balance_lots 也是「張」→ 直接張對張比,不 ×1000。
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
