from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from src.collectors.rf4db import collect_catches
from src.config import DATA_DIR
from src.db import upsert_catches, upsert_refresh_coverage
from src.process_lock import collection_lock


STATE_PATH = DATA_DIR / "history_backfill_state.json"


def _load_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="限速、可续传地补采 RF4DB 普通流与带条件星鱼流历史")
    parser.add_argument("--batch-pages", type=int, help="兼容参数：同时设置两种数据流的本批页数")
    parser.add_argument("--record-pages", type=int, default=100, help="普通活跃度流本批页数")
    parser.add_argument("--condition-pages", type=int, default=100, help="带天气温度条件流本批页数")
    parser.add_argument("--target-days", type=int, choices=(30, 90), default=30, help="目标历史覆盖天数")
    parser.add_argument("--delay", type=float, default=0.25, help="页面请求间隔秒数")
    parser.add_argument("--state-file", type=Path, default=STATE_PATH)
    args = parser.parse_args()
    with collection_lock("history_backfill") as acquired:
        if not acquired:
            print("已有采集任务运行，本次历史补采已跳过。")
            return 0
        return _run(args)


def _run(args) -> int:
    state = _load_state(args.state_file)
    record_pages = args.batch_pages if args.batch_pages is not None else args.record_pages
    condition_pages = args.batch_pages if args.batch_pages is not None else args.condition_pages
    record_pages = max(0, min(record_pages, 500))
    condition_pages = max(0, min(condition_pages, 500))
    was_condition_exhausted = bool(state.get("condition_exhausted", False))
    if was_condition_exhausted:
        condition_pages = 0
    record_start_page = max(1, int(state.get("next_record_page", state.get("next_page", 1))))
    condition_start_page = max(1, int(state.get("next_condition_page", 1)))
    rows = collect_catches(
        pages=condition_pages,
        record_pages=record_pages,
        record_start_page=record_start_page,
        star_start_page=condition_start_page,
        request_delay=max(args.delay, 0.15),
    )
    inserted = upsert_catches(rows)
    normal_rows = [row for row in rows if row.get("fish_grade") == "normal"]
    condition_rows = [row for row in rows if row.get("fish_grade") != "normal"]
    normal_caught = pd.to_datetime(pd.Series([row.get("caught_at") for row in normal_rows]), errors="coerce")
    condition_caught = pd.to_datetime(
        pd.Series([row.get("caught_at") for row in condition_rows]), errors="coerce", utc=True
    ).dt.tz_localize(None)
    normal_oldest = normal_caught.min()
    condition_oldest = condition_caught.min()
    target = pd.Timestamp.now() - timedelta(days=int(args.target_days))
    normal_completed = bool(pd.notna(normal_oldest) and normal_oldest <= target)
    condition_completed = bool(pd.notna(condition_oldest) and condition_oldest <= target)
    previous_condition_oldest = pd.to_datetime(state.get("condition_oldest_seen"), errors="coerce")
    condition_exhausted = was_condition_exhausted or bool(
        condition_start_page >= 300
        and pd.notna(previous_condition_oldest)
        and pd.notna(condition_oldest)
        and condition_oldest >= previous_condition_oldest
    )
    new_state = {
        "next_record_page": record_start_page + record_pages,
        "next_condition_page": condition_start_page if condition_exhausted else condition_start_page + condition_pages,
        "target_days": args.target_days,
        "last_record_start_page": record_start_page,
        "last_condition_start_page": condition_start_page,
        "last_batch_rows": inserted,
        "normal_oldest_seen": normal_oldest.isoformat() if pd.notna(normal_oldest) else None,
        "condition_oldest_seen": condition_oldest.isoformat() if pd.notna(condition_oldest) else state.get("condition_oldest_seen"),
        "normal_completed": normal_completed,
        "condition_completed": condition_completed,
        "condition_exhausted": condition_exhausted,
        "completed": normal_completed and condition_completed,
        "updated_at": datetime.now().astimezone().isoformat(),
    }
    _save_state(args.state_file, new_state)
    upsert_refresh_coverage([{
        "stream": "rf4db_catches_normal_history",
        "source": "rf4db_catches",
        "status": "complete" if normal_completed else "backfilling",
        "previous_success_at": None,
        "earliest_fetched_at": new_state["normal_oldest_seen"],
        "latest_fetched_at": None,
        "fetched_rows": len(normal_rows),
        "overlap_rows": 0,
        "note": (
            f"90日目标已达到；本批从第{record_start_page}页完成。" if normal_completed else
            f"旧缺口续传中；本批第{record_start_page}页起，下一批第{new_state['next_record_page']}页。"
        ),
        "updated_at": new_state["updated_at"],
    }])
    print(json.dumps(new_state, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
