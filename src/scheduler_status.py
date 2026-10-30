from __future__ import annotations

import json
import os
import subprocess

import pandas as pd

from .config import DATA_DIR


TASKS = {
    "RF4Local-Fast-Catches": ("RF4DB普通/星级渔获", 15),
    "RF4Local-Hourly-RF4DB": ("RF4DB天气与收益", 60),
    "RF4Local-SixHour-Sources": ("其余来源及历史补采", 360),
    "RF4Local-Publish-Public": ("脱敏公开数据发布", 60),
}


def scheduler_status() -> pd.DataFrame:
    state_path = DATA_DIR / "scheduler_state.json"
    installed = False
    try:
        installed = bool(json.loads(state_path.read_text(encoding="utf-8-sig")).get("installed"))
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    details: dict[str, dict] = {}
    if installed and os.name == "nt":
        names = ",".join(f"'{name}'" for name in TASKS)
        command = (
            f"@({names}) | ForEach-Object {{ $t=Get-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue; "
            "if($t){$i=Get-ScheduledTaskInfo -TaskName $_; [pscustomobject]@{name=$_;state=[string]$t.State;"
            "next_run=$i.NextRunTime.ToString('o');last_run=$i.LastRunTime.ToString('o');last_result=$i.LastTaskResult}}} "
            "| ConvertTo-Json -Compress"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", command],
                capture_output=True, text=True, timeout=10, check=True,
            )
            payload = json.loads(result.stdout or "[]")
            if isinstance(payload, dict):
                payload = [payload]
            details = {item["name"]: item for item in payload}
        except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
            details = {}
    rows = []
    for name, (scope, minutes) in TASKS.items():
        item = details.get(name, {})
        last_run = item.get("last_run")
        if isinstance(last_run, str) and last_run.startswith("1999-"):
            last_run = None
        last_result = item.get("last_result") if last_run else None
        rows.append({
            "task_name": name, "scope": scope, "interval_minutes": minutes,
            "installed": bool(item) if installed else False,
            "run_mode": "后台无窗口" if installed else "未安装",
            "state": item.get("state", "已登记，等待系统状态" if installed else "未安装"),
            "next_run": item.get("next_run"), "last_run": last_run,
            "last_result": last_result,
        })
    return pd.DataFrame(rows)
