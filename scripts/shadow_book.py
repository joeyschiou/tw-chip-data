"""
shadow_book.py — 第二本「紙上帳」(影子倉),零風險、不動錢、不影響現有 screener 帳。

背景:tw-quant-research 的 wave 1–3 預註冊研究產出第一個完整候選
  `W2_E1__slots20_fix40`——**訊號層與 Config B 完全相同**(60 日突破 ∧ 營收 YoY≥15%
  ∧ regime 閘),只有**收割器**不同:

  | | 生產帳(Config B) | 影子帳(候選 wrapper) |
  |---|---|---|
  | 槽數 | 5 | **20** |
  | 持有 | 20 交易日 | **40 交易日** |
  | 進場 | 次日開盤 | 次日開盤(相同) |
  | 撮合誠實度 | — | **次日開盤即漲停 → 跳過不追** |

研究端結論(特徵匹配 null 基準,dev 2015–2022 / val 2023–2024):
  matched α dev +3.66pp(t=5.31)、val +3.94pp(t=3.09);
  cash-in-0050 幾何 dev 21.0% vs 0050 10.2%、val 46.9% vs 39.8%,MDD 均更小。
  **這是紙上結果,尚未通過 holdout;影子倉的目的就是累積前瞻樣本。**

設計原則:**fail-safe**。screener 呼叫端一律包 try/except;本模組自己也不拋例外到外面,
任何異常都只回一行警告,絕不影響 nightly 的既有輸出。

輸出:
  data/screener/shadow_state.json   持倉與 pending(獨立於生產帳)
  data/screener/shadow_trades.csv   逐筆平倉紀錄
  回傳 report 區塊(list[str])由 screener 併入報告
"""
import json
import os

import numpy as np
import pandas as pd

DIR = "data/screener"
STATE = f"{DIR}/shadow_state.json"
TRADES = f"{DIR}/shadow_trades.csv"

N_SLOTS = 20          # 候選 wrapper:20 槽(生產帳是 5)
HOLD_DAYS = 40        # 候選 wrapper:40 交易日(生產帳是 20)
FEES = 0.004425       # 與生產帳同一組摩擦常數

# 台股檔位(用於「次日開盤是否即漲停」的判定;交易所盤前公告的漲停價是無條件捨去到檔位)
_TICKS = [(10, 0.01), (50, 0.05), (100, 0.1), (500, 0.5), (1000, 1.0), (float("inf"), 5.0)]


def _tick(p):
    for hi, t in _TICKS:
        if p < hi:
            return t
    return 5.0


def limit_up_price(prev_close):
    """±10% 後無條件捨去到檔位(TWSE 實務;與研究引擎的 fallback 同口徑)。"""
    if prev_close is None or not np.isfinite(prev_close) or prev_close <= 0:
        return None
    t = _tick(prev_close)
    return round(np.floor(prev_close * 1.10 / t) * t, 4)


# ---------------------------------------------------------------- 包絡基建
# 解鎖條件② 要求「>=90% 成交紀錄落在『模型滑價 + 10bps』包絡內」。
#
# ⚠ **誠實範圍聲明(wave 7 凍結,務必連同任何包絡數字一起引用)**
#   影子倉是**紙上帳**,固定以當日開盤價成交,實際滑價恆為 0。
#   因此「實際滑價 <= 模型滑價 + 10bps」對紙上帳**恆真**,是空檢定,不具驗證力。
#   → 紙上成交能驗證的是**作業誠實**:訊號正確、時點正確、用到的價格真的存在且可成交。
#   → **衝擊係數 c 無法由紙上帳驗證**,只能由試點(docs/pilot_protocol.md)或真實下單驗證。
#   本模組因此把兩件事分開記:
#     (a) ops_ok  作業誠實包絡 —— 成交價確實落在當日 [low, high] 且當日有量、未觸漲停
#     (b) model_slip_bps  模型**預估**滑價 —— 只記錄不比對,備妥欄位供未來真實成交回填
C_IMPACT = 0.75       # wave 5 預先聲明的 fallback;wave 6 實測區間 [0.58, 3.105]
AUCTION_FRAC = 0.03   # 開盤集合競價量佔全日量的比例(粗估;研究端實測中位約 3%)
ENVELOPE_EXTRA_BPS = 10.0


def envelope_record(sid, fill_px, series_fn, today):
    """回一組欄位:整張股數、參與率、模型預估滑價、作業誠實包絡。失敗一律回 None 不拋。"""
    rec = {"shares": None, "lots": None, "affordable": None, "est_part": None,
           "model_slip_bps": None, "envelope_hi_bps": None, "ops_ok": None}
    try:
        e, _stale = series_fn(sid)                 # series_fn 回 (DataFrame, stale)
        if e is None or not len(e) or "date" not in e:
            return rec
        cur = e[e["date"] == today]
        if not len(cur):
            return rec
        row = cur.iloc[0]
        e = e[e["date"] <= today]                  # 波動只用到當日為止(PIT)
        # 整張顆粒度:每槽 NT$100K(NT$2M / 20 槽)
        per_slot = 2_000_000 / N_SLOTS
        lots = int(per_slot // (fill_px * 1000)) if fill_px > 0 else 0
        rec["lots"], rec["shares"] = lots, lots * 1000
        rec["affordable"] = lots >= 1
        vol = float(row.get("Trading_Volume") or row.get("volume") or 0)
        if vol > 0 and rec["shares"]:
            auction = vol * AUCTION_FRAC
            part = rec["shares"] / auction if auction > 0 else None
            if part and np.isfinite(part):
                r = e["close"].pct_change().tail(60)
                sig = float(r.std()) if r.notna().sum() > 10 else 0.03
                # 一律轉原生 float:positions 會被 json.dump 存進 shadow_state.json,
                # np.float64 會直接讓存檔炸掉。
                rec["est_part"] = float(round(part, 5))
                rec["model_slip_bps"] = float(round(C_IMPACT * sig * np.sqrt(part)
                                                    * 1e4, 2))
                rec["envelope_hi_bps"] = float(round(rec["model_slip_bps"]
                                                     + ENVELOPE_EXTRA_BPS, 2))
        lo, hi = float(row.get("low") or 0), float(row.get("high") or 0)
        if lo > 0 and hi > 0:
            rec["ops_ok"] = bool(lo - 1e-9 <= fill_px <= hi + 1e-9 and vol > 0)
    except Exception:  # noqa: BLE001
        pass
    return rec


def _load():
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"positions": [], "pending": [], "last_run_date": None,
            "signals_seen": 0, "days_run": 0}


def _save(s):
    os.makedirs(DIR, exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def run(today, cal, cal_idx, cands, regime_on, series_fn, names=None):
    """
    today      當日日期字串
    cal/cal_idx 交易日曆與索引(由 screener 傳入,避免重算)
    cands      screener 已算好的候選清單(含 id/name/score/yoy/liq_ok/stale)
    regime_on  regime 閘是否開啟
    series_fn  sid -> (DataFrame(含 date/open/close), stale) 的取數函式
    回 (report_lines, stats)
    """
    st = _load()
    it = cal_idx.get(today)
    if it is None:
        return ["## 影子倉:⚠ 今日不在交易日曆,跳過"], {}

    if st.get("last_run_date") == today:
        return [f"## 影子倉:今日({today})已執行過,跳過"], {}

    def px(sid, field):
        try:
            e, _ = series_fn(sid)
            if e is None:
                return None
            r = e[e["date"] == today]
            return float(r[field].iloc[0]) if len(r) else None
        except Exception:  # noqa: BLE001
            return None

    def prev_close(sid):
        try:
            e, _ = series_fn(sid)
            if e is None:
                return None
            r = e[e["date"] < today]
            return float(r["close"].iloc[-1]) if len(r) else None
        except Exception:  # noqa: BLE001
            return None

    positions = st.get("positions", [])
    held = {p["id"] for p in positions}

    # ---------- STAGE 1:昨晚 pending → 今日開盤進場(漲停跳過)----------
    fills, skipped_lu = [], []
    for pend in st.get("pending", []):
        sid = pend["id"]
        if sid in held or len(positions) >= N_SLOTS:
            continue
        o = px(sid, "open")
        if o is None:
            continue
        lu = limit_up_price(prev_close(sid))
        if lu is not None and o >= lu - 1e-9:
            skipped_lu.append(sid)          # 撮合誠實度:開盤即漲停不追
            continue
        exit_due = cal[it + HOLD_DAYS] if it + HOLD_DAYS < len(cal) else None
        env = envelope_record(sid, o, series_fn, today)
        positions.append({"id": sid, "name": pend.get("name", ""), "entry_date": today,
                          "entry_price": o, "exit_due": exit_due,
                          "signal_date": pend.get("signal_date"), **env})
        held.add(sid)
        fills.append(sid)

    # ---------- STAGE 2:到期出場(40 交易日)----------
    trades = []
    if os.path.exists(TRADES):
        try:
            trades = pd.read_csv(TRADES, dtype=str).to_dict("records")
        except Exception:  # noqa: BLE001
            trades = []
    keep, exits = [], []
    for p in positions:
        ei = cal_idx.get(p["entry_date"])
        c = px(p["id"], "close")
        if ei is not None and (it - ei) >= HOLD_DAYS and c is not None:
            gross = c / p["entry_price"] - 1
            trades.append({"id": p["id"], "name": p.get("name", ""),
                           "entry_date": p["entry_date"],
                           "entry_price": f"{p['entry_price']:.4f}", "exit_date": today,
                           "exit_price": f"{c:.4f}", "reason": f"hold_{HOLD_DAYS}",
                           "gross_return": f"{gross:.6f}",
                           "net_return": f"{gross - FEES:.6f}",
                           # 包絡基建(進場時記錄,平倉時一併落帳)
                           "lots": p.get("lots"), "shares": p.get("shares"),
                           "affordable": p.get("affordable"),
                           "est_part": p.get("est_part"),
                           "model_slip_bps": p.get("model_slip_bps"),
                           "envelope_hi_bps": p.get("envelope_hi_bps"),
                           "ops_ok": p.get("ops_ok"),
                           # 紙上帳的實際滑價恆為 0(固定開盤成交)——留欄位供未來真實成交回填
                           "actual_slip_bps": 0.0, "fill_source": "paper_open"})
            exits.append(p["id"])
        else:
            keep.append(p)
    positions = keep
    if trades:
        os.makedirs(DIR, exist_ok=True)
        pd.DataFrame(trades).drop_duplicates(["id", "entry_date"], keep="last").to_csv(
            TRADES, index=False, encoding="utf-8-sig")

    # ---------- STAGE 3:今日訊號 → 明日 pending(20 槽)----------
    pending = []
    n_sig = len([c for c in cands if c.get("liq_ok") and not c.get("stale")])
    if regime_on:
        held = {p["id"] for p in positions}
        free = N_SLOTS - len(positions)
        for c in cands:
            if len(pending) >= free:
                break
            if c["id"] in held or not c.get("liq_ok") or c.get("stale"):
                continue
            pending.append({"id": c["id"], "name": c.get("name", ""),
                            "signal_date": today})

    st.update({"positions": positions, "pending": pending, "last_run_date": today,
               "signals_seen": st.get("signals_seen", 0) + n_sig,
               "days_run": st.get("days_run", 0) + 1})
    _save(st)

    # ---------- 報告區塊 ----------
    dr = max(st["days_run"], 1)
    closed = len(trades)
    stats = {"n_positions": len(positions), "n_pending": len(pending),
             "n_signals_today": n_sig,
             "avg_signals_per_day": round(st["signals_seen"] / dr, 2),
             "closed_trades": closed, "days_run": dr,
             "turns_per_year_est": round(len(fills) * 252 / dr, 1) if dr else None}
    L = [f"## 影子倉(候選 wrapper:{N_SLOTS} 槽 / {HOLD_DAYS} 交易日)— 紙上帳,不動錢",
         f"- 訊號層與生產帳**完全相同**,只換收割器。研究端 dev/val 皆過 T3,**尚未過 holdout**。",
         f"- 今日訊號 {n_sig} 檔;持倉 {len(positions)}/{N_SLOTS};明日 pending {len(pending)}",
         f"- 今日進場 {len(fills)}{('(' + ','.join(fills) + ')') if fills else ''}"
         f";到期出場 {len(exits)}{('(' + ','.join(exits) + ')') if exits else ''}"]
    if skipped_lu:
        L.append(f"- ⚠ 開盤即漲停跳過 {len(skipped_lu)} 檔:{','.join(skipped_lu)}(誠實度規則)")
    # 包絡基建:整張顆粒度與作業誠實(**不是**衝擊係數驗證,見模組頂端聲明)
    newp = [p for p in positions if p.get("entry_date") == today]
    if newp:
        unaff = [p["id"] for p in newp if p.get("affordable") is False]
        bad = [p["id"] for p in newp if p.get("ops_ok") is False]
        slips = [p["model_slip_bps"] for p in newp if p.get("model_slip_bps") is not None]
        L.append(f"- 執行基建:整張買不起 {len(unaff)} 檔"
                 f"{('(' + ','.join(unaff) + ')') if unaff else ''}"
                 f";模型預估滑價中位 "
                 f"{(f'{float(np.median(slips)):.1f}bps' if slips else 'n/a')}"
                 f";作業誠實包絡異常 {len(bad)} 檔"
                 f"{('(' + ','.join(bad) + ')') if bad else ''}")
        L.append("  (⚠ 紙上帳固定開盤成交,實際滑價恆為 0 → **『落在模型滑價包絡內』對紙上帳恆真,"
                 "不具驗證力**;c 只能由試點或真實下單驗證)")
    L.append(f"- 累計運行 {dr} 日,平均每日訊號 {stats['avg_signals_per_day']} 檔,"
             f"已平倉 {closed} 筆;log:`{TRADES}`")
    if closed:
        try:
            t = pd.read_csv(TRADES)
            nr = pd.to_numeric(t["net_return"], errors="coerce").dropna()
            if len(nr):
                L.append(f"- 影子帳累計:{len(nr)} 筆,平均 {nr.mean()*100:+.2f}%、"
                         f"中位 {nr.median()*100:+.2f}%、勝率 {(nr>0).mean()*100:.1f}%")
        except Exception:  # noqa: BLE001
            pass
    L.append("")
    return L, stats
