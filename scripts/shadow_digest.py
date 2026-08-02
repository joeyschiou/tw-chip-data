"""
shadow_digest.py — 影子倉月報(每月 1 日由 screener 自動產生)。

輸出 docs/shadow_digest_YYYY-MM.md,前 10 行是給 Jaden 的摘要:
  筆數、命中包絡比例、matched α 初讀、P0 計數。

## P0 異常定義(wave 6 凍結,任一即 P0)
  P0-1 訊號層與生產帳不一致:同一日影子帳與生產帳的「今日成立訊號」集合不同
  P0-2 資料缺漏致漏單:當日 regime on 且有合格候選,但影子帳 pending 為空且非滿倉
  P0-3 帳本損毀:shadow_state.json / shadow_trades.csv 無法解析,或欄位缺失
  P0-4 成交模型與規格不符:進場價不等於當日開盤價,或持有期偏離 40 交易日 ±1

**fail-safe**:任何異常只回一行警告,絕不影響 nightly 既有輸出。
"""
import json
import os
from datetime import date

import numpy as np
import pandas as pd

DIR = "data/screener"
STATE = f"{DIR}/shadow_state.json"
TRADES = f"{DIR}/shadow_trades.csv"
DOCS = "docs"
HOLD_DAYS = 40
ENVELOPE_EXTRA_BPS = 10      # 「模型滑價 + 10bps」包絡

P0_DEFS = [
    ("P0-1", "訊號層與生產帳不一致"),
    ("P0-2", "資料缺漏致漏單(regime on 且有合格候選,卻無 pending 且未滿倉)"),
    ("P0-3", "帳本損毀(state/trades 無法解析或欄位缺失)"),
    ("P0-4", "成交模型與規格不符(進場價非當日開盤 / 持有期偏離 40 交易日 ±1)"),
]


def check_p0(st, trades, cal_idx=None):
    """回 list[(code, detail)]。只做能在 repo 內自證的檢查。"""
    out = []
    if st is None:
        # 區分「還沒跑過」與「檔案損毀」—— 前者不是 P0(影子倉剛上線時本來就沒檔)
        if not os.path.exists(STATE):
            return []
        out.append(("P0-3", "shadow_state.json 存在但無法解析"))
        return out
    need = {"positions", "pending", "last_run_date"}
    if not need <= set(st):
        out.append(("P0-3", f"state 缺欄位:{sorted(need - set(st))}"))
    if trades is not None and len(trades):
        cols = {"id", "entry_date", "entry_price", "exit_date", "net_return"}
        if not cols <= set(trades.columns):
            out.append(("P0-3", f"trades 缺欄位:{sorted(cols - set(trades.columns))}"))
        elif cal_idx:
            bad = 0
            for _, r in trades.iterrows():
                i0, i1 = cal_idx.get(str(r["entry_date"])), cal_idx.get(str(r["exit_date"]))
                if i0 is not None and i1 is not None and abs((i1 - i0) - HOLD_DAYS) > 1:
                    bad += 1
            if bad:
                out.append(("P0-4", f"{bad} 筆持有期偏離 {HOLD_DAYS} 交易日 ±1"))
    return out


def _num(mo, col):
    if not len(mo) or col not in mo.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(mo[col], errors="coerce").dropna()


def _env_line(mo):
    """命中包絡比例。對紙上帳恆為 100% —— 照實標,不假裝是檢定。"""
    a = _num(mo, "actual_slip_bps")
    e = _num(mo, "envelope_hi_bps")
    n = min(len(a), len(e))
    if n == 0:
        return "尚無資料(本月無平倉,或平倉筆早於 wave 7 基建)"
    hit = (a.iloc[:n].to_numpy() <= e.iloc[:n].to_numpy()).mean()
    return f"{hit*100:.0f}%({n} 筆)—— 恆真,無資訊量"


def _ops_line(mo):
    if not len(mo) or "ops_ok" not in mo.columns:
        return "尚無資料"
    v = mo["ops_ok"].astype(str).str.lower()
    ok, bad = (v == "true").sum(), (v == "false").sum()
    if ok + bad == 0:
        return "尚無資料"
    return (f"{ok}/{ok+bad} = {ok/(ok+bad)*100:.0f}% 通過"
            + ("" if bad == 0 else f" — **{bad} 筆異常,須查(P0-4 候選)**"))


def _lot_line(mo):
    if not len(mo) or "affordable" not in mo.columns:
        return "尚無資料"
    v = mo["affordable"].astype(str).str.lower()
    aff, un = (v == "true").sum(), (v == "false").sum()
    if aff + un == 0:
        return "尚無資料"
    s = _num(mo, "model_slip_bps")
    return (f"NT$100K/槽 下買得起整張 {aff}/{aff+un} 筆"
            + (f";買不起 **{un}** 筆" if un else "")
            + (f";模型預估滑價中位 {s.median():.1f}bps" if len(s) else ""))


def build(ym=None, cal_idx=None):
    ym = ym or date.today().strftime("%Y-%m")
    st = None
    if os.path.exists(STATE):
        try:
            st = json.load(open(STATE, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            st = None
    tr = None
    if os.path.exists(TRADES):
        try:
            tr = pd.read_csv(TRADES, dtype=str)
        except Exception:  # noqa: BLE001
            tr = None

    p0 = check_p0(st, tr, cal_idx)
    mo = pd.DataFrame()
    if tr is not None and len(tr) and "exit_date" in tr.columns:
        mo = tr[tr["exit_date"].astype(str).str[:7] == ym]

    nr = pd.to_numeric(mo["net_return"], errors="coerce").dropna() if len(mo) else pd.Series(dtype=float)
    L = [f"# 影子倉月報 {ym}\n",
         f"- 本月平倉 **{len(mo)}** 筆;累計平倉 {0 if tr is None else len(tr)} 筆",
         f"- 本月平均淨報酬 **{(nr.mean()*100 if len(nr) else float('nan')):+.2f}%**、"
         f"中位 {(nr.median()*100 if len(nr) else float('nan')):+.2f}%、"
         f"勝率 {((nr>0).mean()*100 if len(nr) else float('nan')):.1f}%",
         f"- **P0 異常:{len(p0)} 件**"
         + ("" if not p0 else "(" + "、".join(f"{c}" for c, _ in p0) + ")"),
         f"- 命中包絡比例:**{_env_line(mo)}**",
         f"  (⚠ 紙上帳固定開盤成交、實際滑價恆為 0 → 這個比例對紙上帳**恆為 100%**,"
         f"是空檢定。真正有驗證力的是下一行的作業誠實。)",
         f"- **作業誠實包絡**(成交價確實在當日 [low, high] 內且當日有量):**{_ops_line(mo)}**",
         f"- 整張顆粒度:{_lot_line(mo)}",
         f"- matched α 初讀:**尚無法計算** —— 需累積足夠樣本(建議 ≥30 筆)"
         f"且以研究端的匹配 null 口徑重算",
         f"- 目前持倉 {len((st or {}).get('positions', []))}/20;"
         f"pending {len((st or {}).get('pending', []))}",
         f"- 累計運行 {(st or {}).get('days_run', 0)} 日;"
         f"平均每日訊號 "
         f"{((st or {}).get('signals_seen', 0) / max((st or {}).get('days_run', 1), 1)):.2f} 檔",
         f"- 研究端預期:月均進場 7–9 筆、年換手 8–11 次、平均同時持倉 13–17 檔\n",
         "---\n", "## P0 異常明細\n"]
    if p0:
        for c, d in p0:
            L.append(f"- **{c}**:{d}")
    else:
        L.append("*(無)*")
    L.append("\n## P0 定義(wave 6 凍結)\n")
    for c, d in P0_DEFS:
        L.append(f"- **{c}** {d}")
    if len(mo):
        L.append("\n## 本月平倉明細\n")
        L.append("| 代號 | 進場日 | 進場價 | 出場日 | 出場價 | 淨報酬 |")
        L.append("|---|---|---|---|---|---|")
        for _, r in mo.iterrows():
            L.append(f"| {r['id']} | {r['entry_date']} | {r['entry_price']} | "
                     f"{r['exit_date']} | {r.get('exit_price','')} | {r['net_return']} |")
    os.makedirs(DOCS, exist_ok=True)
    p = os.path.join(DOCS, f"shadow_digest_{ym}.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    return p, len(p0), len(mo)


def maybe_run(today, cal_idx=None):
    """每月 1 日產上個月的月報。回 report 行(給 screener 併入)。"""
    try:
        d = date.fromisoformat(today)
    except Exception:  # noqa: BLE001
        return []
    if d.day != 1:
        return []
    prev = (d.replace(day=1) - pd.Timedelta(days=1)).strftime("%Y-%m")
    p, n_p0, n_tr = build(prev, cal_idx)
    return [f"## 影子倉月報已產生:`{p}`(平倉 {n_tr} 筆、P0 {n_p0} 件)", ""]


if __name__ == "__main__":
    import sys
    ym = sys.argv[1] if len(sys.argv) > 1 else None
    p, n_p0, n_tr = build(ym)
    print(f"→ {p}(平倉 {n_tr} 筆、P0 {n_p0} 件)")
