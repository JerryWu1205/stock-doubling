"""run.bat 用：盤中把每 15 分鐘的排程拆成每 5 分鐘跑一次。

  python waitslot.py 0   記下這一輪開始的時間
  python waitslot.py N   等到開始後第 N×5 分鐘；美股、台股都沒在交易，或已經趕不上（前一次跑太久）就回傳 1，run.bat 就結束。
"""
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

MARK = Path(__file__).resolve().parent / "logs" / "run_start.txt"
HOURS = [("America/New_York", "09:30", "16:00"), ("Asia/Taipei", "09:00", "13:30")]


def trading():
    for tz, a, b in HOURS:
        now = datetime.now(ZoneInfo(tz))
        if now.weekday() < 5 and a <= now.strftime("%H:%M") < b:
            return True
    return False


def main(n):
    if n == 0:
        MARK.parent.mkdir(exist_ok=True)
        MARK.write_text(str(time.time()))
        return 0
    try:
        start = float(MARK.read_text())
    except (OSError, ValueError):
        return 1
    target = start + n * 300
    if not trading() or time.time() > target + 60:
        return 1
    time.sleep(max(0.0, target - time.time()))
    return 0 if trading() else 1


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 0))
