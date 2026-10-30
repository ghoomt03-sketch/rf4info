from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime

from src.config import DATA_DIR, ROOT_DIR


PROFILES = {
    "fast": [
        ["refresh_data.py", "--if-stale", "--source", "rf4db_catches"],
    ],
    "hourly": [
        ["refresh_data.py", "--if-stale", "--source", "rf4db_weather", "--source", "rf4db_earnings",
         "--source", "rf4db_map_assets"],
    ],
    "six-hour": [
        ["refresh_data.py", "--if-stale", "--source", "rf4stat_activity", "--source", "rf4stat_posts",
         "--source", "rf4map_points", "--source", "official_weekly_cn"],
        ["backfill_history.py", "--record-pages", "300", "--condition-pages", "0", "--target-days", "90"],
    ],
}


def main() -> int:
    parser = argparse.ArgumentParser(description="按分级计划执行公开数据刷新")
    parser.add_argument("--profile", choices=PROFILES, required=True)
    args = parser.parse_args()
    log_dir = DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    log_path = log_dir / f"scheduled-{args.profile}-{stamp}.log"
    exit_code = 0
    with log_path.open("w", encoding="utf-8") as log:
        child_env = dict(os.environ)
        child_env["RF4_BACKGROUND"] = "1"
        for command in PROFILES[args.profile]:
            completed = subprocess.run(
                [sys.executable, str(ROOT_DIR / command[0]), *command[1:]],
                cwd=ROOT_DIR, stdout=log, stderr=subprocess.STDOUT, text=True,
                env=child_env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            exit_code = max(exit_code, completed.returncode)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
