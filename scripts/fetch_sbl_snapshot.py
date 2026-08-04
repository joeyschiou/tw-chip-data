"""
fetch_sbl_snapshot.py — TWSE 借券/放空法規快照,**前瞻累積**。

╔═══════════════════════════════════════════════════════════════════════╗
║ 為什麼是「前瞻累積」而不是回填                                        ║
║                                                                       ║
║ wave 17 稽核結論:**歷史標借費率買不到** ——                            ║
║   · TWSE OpenAPI 143 個端點只有 `/SBL/TWT96U`(當日可借券**股數**快照, ║
║     無歷史、非費率)                                                   ║
║   · 官網 `/SBL/*`、`/marginTrading/TWT93U` 等 14 條路徑實測全部 404    ║
║   · FinMind 無任何借券 dataset                                        ║
║                                                                       ║
║ → **歷史買不到,就從今天開始自己長。**每晚存一份當日快照,             ║
║   一年後就有一年的序列。                                              ║
╚═══════════════════════════════════════════════════════════════════════╝

抓兩張:
  `/v1/SBL/TWT96U`      上市上櫃股票**當日可借券賣出股數**(券源量)
  `/exchangeReport/TWT92U`  逐日融資融券可交易 / 暫停融券借券 / 平盤下禁令名單
                            (這張**有歷史**,但一併每日存,避免未來端點變動)

存成 data/sbl_snapshot/{date}.csv(utf-8-sig),idempotent:同日已存則跳過。
"""
import csv
import datetime as dt
import os
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "sbl_snapshot")
H = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.twse.com.tw/"}


def today_tpe():
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)).strftime("%Y-%m-%d")


def get(url, params=None, tries=3):
    for _ in range(tries):
        try:
            r = requests.get(url, params=params, headers=H, timeout=60)
            return r.json()
        except Exception:  # noqa: BLE001
            time.sleep(3)
    return None


def main():
    os.makedirs(OUT, exist_ok=True)
    d = today_tpe()
    path = os.path.join(OUT, f"{d}.csv")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        print(f"[sbl] {d} 已存在,跳過")
        return 0

    rows = []
    j = get("https://openapi.twse.com.tw/v1/SBL/TWT96U")
    if isinstance(j, list):
        for r in j:
            for code_k, vol_k, mkt in (("TWSECode", "TWSEAvailableVolume", "twse"),
                                       ("GRETAICode", "GRETAIAvailableVolume", "tpex")):
                sid = str(r.get(code_k) or "").strip()
                if not sid:
                    continue
                rows.append({"date": d, "source": "TWT96U", "market": mkt,
                             "stock_id": sid,
                             "available_volume": str(r.get(vol_k) or "").replace(",", ""),
                             "susp_margin_short": "", "susp_sbl_short": "",
                             "no_below_flat": ""})
    else:
        print("[sbl] TWT96U 抓取失敗", file=sys.stderr)

    k = get("https://www.twse.com.tw/exchangeReport/TWT92U",
            {"date": d.replace("-", ""), "response": "json"})
    if isinstance(k, dict) and (k.get("data") or []):
        for r in k["data"]:
            rows.append({
                "date": d, "source": "TWT92U", "market": "twse",
                "stock_id": str(r[0]).strip(), "available_volume": "",
                "susp_margin_short": "Y" if str(r[2]).strip() == "*" else "",
                "susp_sbl_short": "Y" if len(r) > 3 and str(r[3]).strip() == "*" else "",
                "no_below_flat": "Y" if len(r) > 4 and str(r[4]).strip() == "*" else "",
            })
    else:
        print("[sbl] TWT92U 無資料(可能非交易日)")

    if not rows:
        print(f"[sbl] {d} 無資料,不寫檔")
        return 0
    cols = ["date", "source", "market", "stock_id", "available_volume",
            "susp_margin_short", "susp_sbl_short", "no_below_flat"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    n96 = sum(1 for r in rows if r["source"] == "TWT96U")
    n92 = sum(1 for r in rows if r["source"] == "TWT92U")
    print(f"[sbl] {d} → {path}(TWT96U {n96} 列 / TWT92U {n92} 列)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
