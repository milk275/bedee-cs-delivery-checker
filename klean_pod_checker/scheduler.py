"""Run the full CS status refresh hourly during the configured service window."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def next_run(
    now: datetime,
    *,
    start_hour: int = 8,
    end_hour: int = 22,
    minute: int = 0,
) -> datetime:
    if not 0 <= start_hour <= end_hour <= 23:
        raise ValueError("ช่วงเวลารันอัตโนมัติไม่ถูกต้อง")
    if not 0 <= minute <= 59:
        raise ValueError("นาทีรันอัตโนมัติไม่ถูกต้อง")
    candidates = [
        now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        for hour in range(start_hour, end_hour + 1)
    ]
    candidate = next((value for value in candidates if value > now), None)
    if candidate is not None:
        return candidate
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(
        hour=start_hour, minute=minute, second=0, microsecond=0
    )


def run_refresh() -> int:
    command = [sys.executable, "-m", "klean_pod_checker", "--skip-final"]
    return subprocess.run(command, check=False).returncode


def main() -> None:
    timezone = ZoneInfo(os.environ.get("SCHEDULE_TIMEZONE", "Asia/Bangkok"))
    start_hour = int(os.environ.get("SCHEDULE_START_HOUR", "8"))
    end_hour = int(os.environ.get("SCHEDULE_END_HOUR", "22"))
    minute = int(os.environ.get("SCHEDULE_MINUTE", "0"))
    if os.environ.get("SCHEDULE_RUN_ON_START", "false").casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        print("Running initial delivery status refresh", flush=True)
        run_refresh()

    while True:
        now = datetime.now(timezone)
        target = next_run(
            now,
            start_hour=start_hour,
            end_hour=end_hour,
            minute=minute,
        )
        delay = max(1, (target - now).total_seconds())
        print(f"Next delivery status refresh: {target.isoformat()}", flush=True)
        time.sleep(delay)
        return_code = run_refresh()
        if return_code:
            print(
                f"Delivery status refresh failed with exit code {return_code}; "
                "the scheduler will retry next hour",
                file=sys.stderr,
                flush=True,
            )


if __name__ == "__main__":
    main()
