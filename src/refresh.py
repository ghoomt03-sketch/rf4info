from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from .refresh_reporting import failure_message

from .collectors.rf4db import collect_catches, collect_earnings, collect_map_assets, collect_weather
from .collectors.community import collect_rf4map, collect_rf4stat
from .collectors.rf4stat_posts import collect_rf4stat_posts
from .collectors.official_weekly import collect_official_weekly
from .db import (
    last_success,
    log_refresh,
    recent_catch_ids,
    upsert_refresh_coverage,
    upsert_catches,
    upsert_community_spots,
    upsert_earnings,
    upsert_map_assets,
    upsert_official_weekly,
    upsert_weather,
)
from .coverage import assess_refresh


def collect_catches_adaptive() -> list[dict]:
    """高频时遇到既有ID即停止；停刷后最多扩大到安全页数并交给历史补采续传。"""
    normal_ids = recent_catch_ids("normal")
    star_ids = recent_catch_ids("star")
    return collect_catches(
        record_pages=200 if normal_ids else 20,
        pages=60 if star_ids else 20,
        stop_record_ids=normal_ids,
        stop_star_ids=star_ids,
    )


SOURCES = {
    "rf4db_weather": (collect_weather, upsert_weather, timedelta(minutes=55)),
    "rf4db_earnings": (collect_earnings, upsert_earnings, timedelta(minutes=30)),
    "rf4db_map_assets": (collect_map_assets, upsert_map_assets, timedelta(hours=24)),
    "rf4db_catches": (collect_catches_adaptive, upsert_catches, timedelta(minutes=14)),
    "rf4stat_activity": (collect_rf4stat, upsert_community_spots, timedelta(hours=2)),
    "rf4stat_posts": (collect_rf4stat_posts, len, timedelta(minutes=30)),
    "rf4map_points": (collect_rf4map, upsert_community_spots, timedelta(minutes=30)),
    "official_weekly_cn": (collect_official_weekly, upsert_official_weekly, timedelta(hours=6)),
}


def refresh_all(
    if_stale: bool = True,
    progress_callback: Callable[[str, int, int], None] | None = None,
    sources: tuple[str, ...] = (),
) -> dict[str, dict]:
    results: dict[str, dict] = {}
    now = datetime.now().astimezone()
    selected = {source: SOURCES[source] for source in sources} if sources else SOURCES
    total = len(selected)
    for index, (source, (collector, writer, ttl)) in enumerate(selected.items()):
        if progress_callback:
            progress_callback(source, index, total)
        previous = last_success(source)
        if if_stale and previous and now - previous < ttl:
            results[source] = {"status": "cached", "rows": 0, "message": "缓存仍在有效期"}
            if progress_callback:
                progress_callback(source, index + 1, total)
            continue
        started = datetime.now().astimezone().isoformat()
        try:
            rows = collector()
            coverage_records = assess_refresh(source, rows, previous)
            count = writer(rows)
            upsert_refresh_coverage(coverage_records)
            message = getattr(rows, "message", "")
            log_refresh(source, started, True, count, message)
            results[source] = {
                "status": "updated", "rows": count, "message": message,
                "coverage": {record["stream"]: record["status"] for record in coverage_records},
            }
        except Exception as exc:
            message = failure_message(exc)
            saved_rows = getattr(exc, 'saved_rows', 0)
            log_refresh(source, started, False, saved_rows, message)
            results[source] = {"status": "failed", "rows": saved_rows, "message": message}
        if progress_callback:
            progress_callback(source, index + 1, total)
    return results

