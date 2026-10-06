"""
wait_quota.py — 等 FinMind 用量降下來再繼續(接力迴圈用)。

FinMind 免費帳號的 600 call/hr 是「滾動 60 分鐘」計算,不是整點歸零
(實測:11:33 用到 626,12:03 查仍是 644)。所以不能「睡到整點」,
要每隔幾分鐘查 user_info,等用量降到 --below 以下才放行。

用法:python scripts/wait_quota.py --below 150 --max-minutes 75
回傳 0 = 用量已降到門檻下;1 = 等到上限仍未降(呼叫端自行決定要不要繼續)。
"""
import sys
import time
import argparse
import finmind_client as fc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--below", type=int, default=150, help="用量低於此數才放行")
    ap.add_argument("--max-minutes", type=int, default=75, help="最多等幾分鐘")
    ap.add_argument("--poll", type=int, default=120, help="查詢間隔秒數")
    args = ap.parse_args()

    token = fc.get_token()
    deadline = time.time() + args.max_minutes * 60
    while True:
        used, lim = fc.token_usage(token)
        print(f"   quota {used}/{lim}(門檻 < {args.below})", flush=True)
        if used is not None and used < args.below:
            return 0
        if time.time() + args.poll > deadline:
            print("   ⚠ 等到上限,用量仍未降下來")
            return 1
        time.sleep(args.poll)


if __name__ == "__main__":
    sys.exit(main())
