"""
test_regime.py — regime 管線回歸驗證(可重複執行、全唯讀、不依賴網路)

用法:python scripts/test_regime.py       # 從 repo 根目錄跑;全綠 exit 0,任一條 FAIL exit 1

設計原則:不鎖任何跨快照數值。這裡沒有一條斷言寫死今天的 index/ma120/日期——
全部是「同一份 daily_adj 快照內」的結構不變量,換一份快照重跑一樣該綠。
所有寫檔都在 tempfile 暫存目錄;工作區的 data/screener/market_index.csv 有 sha256 前後比對,
state.json / trades.csv 全程不碰。

五條不變量:
  T1 決定性     同一快照連跑兩次 build_market_index(),輸出位元一致
  T2 邊界       基底最後日 == daily_adj 最後日 → n_ext=0 且輸出與輸入位元一致
  T3 截斷回測   基底砍到 adj_last − 5 交易日、raw 延伸,逐日對照 adj 真值(核心)
  T4 min-N      日線覆蓋不足的資料日必須被棄算,regime 停在最後有效日
  T5 落地衛生   load_or_build_index 落地的 CSV 無 provisional 欄、日期嚴格遞增、追平 adj

⚠️ T3 未來若在新快照上失敗,不必然是程式壞。raw 延伸的偏移來源是除權息(raw 含除息跳空、
   adj 不含),遇到異常除權息週(集中除息、大權值股除息)或半市場資料日,偏移會超出容忍帶。
   T3 失敗時會印出逐日偏移表(raw index / adj index / 差 / 兩邊 regime),交人工裁決:
   看偏移是不是單向下偏、是不是集中在特定幾天,再決定是改容忍值還是真的有 bug。

註:T1 跑兩次真實 build(含完整檔案 I/O)以驗證決定性;T1 之後測試會替 screener_core.clean_adj
   裝上 lru_cache 以縮短總執行時間——這只影響本測試進程,production 程式碼不受影響。
"""
import os
import sys
import time
import shutil
import hashlib
import tempfile
import functools

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import screener_core as sc            # noqa: E402
import screener as sr                 # noqa: E402

MKT = "data/screener/market_index.csv"
CUT_DAYS = 5                          # T3 截斷深度(交易日)
KNIFE_PP = 0.25                       # 刀口帶:|buffer| < 此值(對數百分點)容許 regime 分歧
DRIFT_PER_DAY_PP = 0.05               # T3a 每延伸日容忍偏移(對數百分點)
DRIFT_BASE_PP = 0.05                  # T3a 固定容忍項
DIR_TOL_PP = 0.02                     # T3c 方向性容忍(raw 只准比 adj 低)


def _bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


def _buf_pp(row) -> float:
    """buffer,對數百分點。"""
    return (float(row["index"]) - float(row["ma120"])) * 100


# ───────────────────────── T1 ─────────────────────────

def t1_deterministic(ctx):
    a = sc.build_market_index()
    b = sc.build_market_index()
    ctx["base"] = a
    ba, bb = _bytes(a), _bytes(b)
    if ba == bb:
        return True, [f"兩次 build 輸出位元一致({len(a)} 列,最後日 {a['date'].iloc[-1]})"]
    d = []
    if len(a) != len(b):
        d.append(f"列數不同:{len(a)} vs {len(b)}")
    else:
        m = a.merge(b, on="date", suffixes=("_1", "_2"))
        bad = m[~np.isclose(m["index_1"], m["index_2"], rtol=0, atol=0)]
        d.append(f"index 不同的日數:{len(bad)}")
        d += [f"  {r['date']}  {r['index_1']!r} vs {r['index_2']!r}" for _, r in bad.head(10).iterrows()]
    return False, d


# ───────────────────────── T2 ─────────────────────────

def t2_boundary(ctx):
    base = ctx["base"]
    a_last = sc.adj_last_date()
    d = []
    if base["date"].iloc[-1] != a_last:
        return False, [f"前提不成立:基底最後日 {base['date'].iloc[-1]} != daily_adj 最後日 {a_last}"]
    ext, n_ext, adj_last = sc.extend_market_index(base)
    ok = True
    if n_ext != 0:
        ok = False
        d.append(f"n_ext 應為 0,實際 {n_ext};多出的日:"
                 f"{list(ext[ext['provisional'].astype(bool)]['date'])}")
    if adj_last != a_last:
        ok = False
        d.append(f"回傳 adj_last {adj_last} != {a_last}")
    lhs = base.drop(columns=[c for c in ("provisional",) if c in base.columns])
    rhs = ext.drop(columns=[c for c in ("provisional",) if c in ext.columns])
    if _bytes(lhs) != _bytes(rhs):
        ok = False
        d.append(f"輸出與輸入不一致:{len(lhs)} 列 vs {len(rhs)} 列")
        if len(lhs) == len(rhs):
            m = lhs.merge(rhs, on="date", suffixes=("_in", "_out"))
            bad = m[m["index_in"] != m["index_out"]]
            d += [f"  {r['date']}  {r['index_in']!r} vs {r['index_out']!r}" for _, r in bad.head(10).iterrows()]
    if ok:
        d.append(f"基底 == adj 最後日({a_last})→ n_ext=0,輸出位元與輸入一致")
    return ok, d


# ───────────────────────── T3 ─────────────────────────

def _truncate(base: pd.DataFrame, back: int) -> pd.DataFrame:
    """砍掉尾端 back 個交易日,回基底副本。"""
    return base.iloc[:len(base) - back].copy()


def t3_truncation(ctx):
    base = ctx["base"]
    if len(base) < CUT_DAYS + 130:
        return None, [f"快照太短({len(base)} 列),跳過"]
    trunc = _truncate(base, CUT_DAYS)
    ext, n_ext, adj_last = sc.extend_market_index(trunc)
    ctx["t3"] = (ext, n_ext, adj_last)
    if n_ext == 0:
        return None, [f"基底砍到 {adj_last} 後 raw 延伸 0 日(可能全被 min-N 棄算:"
                      f"{ext.attrs.get('ext_skipped')}),無從對照,跳過"]

    rows, fails = [], []
    prov = ext[ext["provisional"].astype(bool)]
    for i, (_, r) in enumerate(prov.iterrows(), 1):
        truth = base[base["date"] == r["date"]]
        if not len(truth):
            fails.append(f"{r['date']}:adj 基底無此日,無法對照")
            continue
        t = truth.iloc[0]
        diff_pp = (float(r["index"]) - float(t["index"])) * 100
        tol = DRIFT_PER_DAY_PP * i + DRIFT_BASE_PP
        rb, tb = _buf_pp(r), _buf_pp(t)
        rreg, treg = bool(r["regime"]), bool(t["regime"])
        knife = abs(tb) < KNIFE_PP
        rows.append((r["date"], i, float(r["index"]), float(t["index"]), diff_pp, tol,
                     rb, tb, rreg, treg, knife))
        # a. 累積偏移
        if abs(diff_pp) > tol:
            fails.append(f"T3a {r['date']}(第 {i} 延伸日):|偏移| {abs(diff_pp):.3f}pp > 容忍 {tol:.3f}pp")
        # b. regime 分歧只准出現在刀口日
        if rreg != treg and not knife:
            fails.append(f"T3b {r['date']}:regime 分歧(raw={rreg} adj={treg})但 adj buffer "
                         f"{tb:+.3f}pp 不在刀口帶(|buffer| < {KNIFE_PP}pp)")
    # c. 方向性:末日 raw 不得高於 adj(除息偏差單向下偏)
    if rows:
        last = rows[-1]
        if last[4] > DIR_TOL_PP:
            fails.append(f"T3c 延伸末日 {last[0]}:raw 比 adj 高 {last[4]:+.3f}pp "
                         f"> 容忍 {DIR_TOL_PP}pp(除息偏差應為單向下偏)")

    d = [f"基底砍到 {adj_last},raw 延伸 {n_ext} 日;逐日對照表(單位:對數百分點):",
         f"  {'date':<12}{'raw index':>11}{'adj index':>11}{'差pp':>9}{'容忍pp':>9}"
         f"{'raw buf':>9}{'adj buf':>9}  raw/adj reg  刀口"]
    for (dt, i, ri, ti, dp, tol, rb, tb, rr, tr, kn) in rows:
        d.append(f"  {dt:<12}{ri:>11.6f}{ti:>11.6f}{dp:>9.3f}{tol:>9.3f}"
                 f"{rb:>9.3f}{tb:>9.3f}   {str(rr):<5}/{str(tr):<5}  {'✓' if kn else ''}")
    sk = ext.attrs.get("ext_skipped", [])
    if sk:
        d.append(f"  (min-N 棄算:{sk})")
    return (not fails), d + fails


# ───────────────────────── T4 ─────────────────────────

def t4_min_n(ctx):
    """
    用快照裡「真實存在的覆蓋不足日」構造:先從 T3 的延伸結果取得被棄算的資料日 D,
    再把基底砍到 D 的前一交易日 → 延伸應產生 0 個有效日、regime 停在基底最後日。
    快照裡若沒有覆蓋不足日(日線完整),此測跳過——那是好事,不是失敗。
    """
    base = ctx["base"]
    ext3 = ctx.get("t3", (None, 0, None))[0]
    sk = ext3.attrs.get("ext_skipped", []) if ext3 is not None else []
    if not sk:
        return None, [f"此快照 adj_last − {CUT_DAYS} 交易日內沒有覆蓋不足日"
                      f"(所有延伸日 N >= EXT_MIN_N={sc.EXT_MIN_N}),跳過"]
    bad_date, bad_n = sk[0]
    trunc = base[base["date"] < bad_date].copy()
    if not len(trunc):
        return False, [f"棄算日 {bad_date} 之前無基底列"]
    cut_last = trunc["date"].iloc[-1]
    ext, n_ext, adj_last = sc.extend_market_index(trunc)
    skipped = ext.attrs.get("ext_skipped", [])
    counts = ext.attrs.get("ext_counts", {})
    fails = []
    if n_ext != 0:
        fails.append(f"n_ext 應為 0(唯一延伸候選 {bad_date} 覆蓋不足),實際 {n_ext}")
    if not skipped or skipped[0][0] != bad_date:
        fails.append(f"ext_skipped 應記錄 {bad_date},實際 {skipped}")
    elif skipped[0][1] >= sc.EXT_MIN_N:
        fails.append(f"棄算日 N={skipped[0][1]} 不該 >= EXT_MIN_N={sc.EXT_MIN_N}")
    if ext["date"].iloc[-1] != cut_last:
        fails.append(f"regime 應停在最後有效日 {cut_last},實際 {ext['date'].iloc[-1]}")
    d = [f"覆蓋不足日 {bad_date}(N={bad_n} < EXT_MIN_N={sc.EXT_MIN_N});基底砍到 {cut_last}",
         f"  逐日 N:{counts}",
         f"  ext_skipped={skipped}  n_ext={n_ext}  最後列={ext['date'].iloc[-1]}"]
    return (not fails), d + fails


# ───────────────────────── T5 ─────────────────────────

def t5_landing(ctx):
    tmp = ctx["tmp"]
    p = os.path.join(tmp, "T5_market_index.csv")
    stale = ctx["base"].iloc[:200].copy()               # 明顯過期的基底 → 應觸發自動重建
    stale.to_csv(p, index=False, encoding="utf-8-sig")
    sr.load_or_build_index(False, path=p)
    got = pd.read_csv(p, dtype={"date": str})
    a_last = sc.adj_last_date()
    fails = []
    if "provisional" in got.columns:
        fails.append("落地 CSV 含 provisional 欄(延伸列不該落地)")
    if got["date"].iloc[-1] != a_last:
        fails.append(f"落地最後日 {got['date'].iloc[-1]} != daily_adj 最後日 {a_last}")
    dt = got["date"]
    if dt.duplicated().any():
        fails.append(f"日期重複:{list(dt[dt.duplicated()].head(5))}")
    if not (dt.values[1:] > dt.values[:-1]).all():
        bad = [f"{dt.iloc[i]} → {dt.iloc[i + 1]}" for i in range(len(dt) - 1) if dt.iloc[i + 1] <= dt.iloc[i]]
        fails.append(f"日期非嚴格遞增:{bad[:5]}")
    d = [f"注入 200 列過期基底 → 自動重建為 {len(got)} 列,最後日 {got['date'].iloc[-1]}",
         f"  欄位:{list(got.columns)};日期嚴格遞增無重複"]
    return (not fails), d + fails


TESTS = [("T1 決定性", t1_deterministic),
         ("T2 邊界", t2_boundary),
         ("T3 截斷回測", t3_truncation),
         ("T4 min-N", t4_min_n),
         ("T5 落地衛生", t5_landing)]


def main() -> int:
    if not os.path.isdir(sc.ADJ_DIR):
        print(f"❌ 找不到 {sc.ADJ_DIR},請從 repo 根目錄執行"); return 1
    h0 = hashlib.sha256(open(MKT, "rb").read()).hexdigest() if os.path.exists(MKT) else None
    tmp = tempfile.mkdtemp(prefix="test_regime_")
    ctx = {"tmp": tmp}
    n_fail = n_skip = 0
    try:
        for name, fn in TESTS:
            t0 = time.time()
            try:
                ok, detail = fn(ctx)
            except Exception as e:                       # noqa: BLE001
                ok, detail = False, [f"例外:{type(e).__name__}: {e}"]
            tag = "⏭ SKIP" if ok is None else ("✅ PASS" if ok else "❌ FAIL")
            print(f"\n{tag}  {name}  ({time.time() - t0:.1f}s)")
            for line in detail:
                print(f"    {line}")
            if ok is None:
                n_skip += 1
            elif not ok:
                n_fail += 1
            if name.startswith("T1"):                    # T1 之後才裝快取(見檔頭註)
                sc.clean_adj = functools.lru_cache(maxsize=None)(sc.clean_adj)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if h0 is not None:
        h1 = hashlib.sha256(open(MKT, "rb").read()).hexdigest()
        print(f"\n工作區 {MKT} sha256 {'未變 ✅' if h0 == h1 else '被改動 ❌'}")
        if h0 != h1:
            n_fail += 1
    print(f"\n{'=' * 60}\n{len(TESTS) - n_fail - n_skip} PASS / {n_fail} FAIL / {n_skip} SKIP")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
