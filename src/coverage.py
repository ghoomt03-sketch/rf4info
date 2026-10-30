from __future__ import annotations

from datetime import datetime
from typing import Iterable

import pandas as pd

from .db import existing_record_keys, upsert_refresh_coverage


SNAPSHOT_SOURCES = {
    "rf4db_weather": ("rf4db_weather", "天气为刷新时刻的未来预报快照，不能恢复未刷新时段。"),
    "rf4db_earnings": ("rf4db_earnings", "收益榜为滚动24小时快照，不能证明间隔内所有榜单变化。"),
    "rf4db_map_assets": ("rf4db_map_assets", "地图图鉴为当前底图与坐标校准快照，不包含点位活跃或收益含义。"),
    "rf4map_points": ("rf4map_points", "点位页未提供可靠事件时间，期间出现后删除的点位无法恢复。"),
    "official_weekly_cn": ("official_weekly_cn", "本周榜为当前周快照，跨周后不能恢复上周末尾变化。"),
}


def _time_bounds(rows: list[dict], field: str) -> tuple[str | None, str | None]:
    local_zone = datetime.now().astimezone().tzinfo
    values = []
    for row in rows:
        value = pd.to_datetime(row.get(field), errors="coerce")
        if pd.isna(value):
            continue
        value = value.tz_localize(local_zone) if value.tzinfo is None else value.tz_convert(local_zone)
        values.append(value)
    if not values:
        return None, None
    return min(values).isoformat(), max(values).isoformat()


def _record(stream: str, source: str, status: str, previous: datetime | None,
            rows: list[dict], overlap: int, note: str, time_field: str) -> dict:
    earliest, latest = _time_bounds(rows, time_field)
    return {
        "stream": stream,
        "source": source,
        "status": status,
        "previous_success_at": previous.isoformat() if previous else None,
        "earliest_fetched_at": earliest,
        "latest_fetched_at": latest,
        "fetched_rows": len(rows),
        "overlap_rows": overlap,
        "note": note,
        "updated_at": datetime.now().astimezone().isoformat(),
    }


def assess_refresh(source: str, rows: Iterable[dict], previous: datetime | None) -> list[dict]:
    """在写入本批数据前评估；稳定 ID 重叠只证明两次刷新区间已衔接。"""
    rows = list(rows)
    prior_keys = existing_record_keys(source, rows)
    if source in SNAPSHOT_SOURCES:
        stream, note = SNAPSHOT_SOURCES[source]
        return [_record(stream, source, "snapshot", previous, rows, len(prior_keys), note, "collected_at")]

    if source == "rf4db_catches":
        records = []
        for grade, stream, label in (
            ("normal", "rf4db_catches_normal", "普通渔获"),
            ("star", "rf4db_catches_star", "带条件星鱼"),
        ):
            subset = [row for row in rows if (row.get("fish_grade") == "normal") == (grade == "normal")]
            overlap = sum(str(row.get("source_id") or "") in prior_keys for row in subset)
            if not previous:
                status, note = "unknown", f"{label}首次采集，等待下一批稳定ID衔接判断。"
            elif overlap:
                status, note = "complete", f"本批遇到{overlap}条既有稳定ID；仅确认两次成功刷新之间已衔接。"
            else:
                status, note = "gap", f"本批未遇到既有稳定ID；已达到本批安全范围，历史补采将继续缩小缺口。"
            records.append(_record(stream, source, status, previous, subset, overlap, note, "caught_at"))
        return records

    if source == "rf4stat_activity":
        map_rows: dict[str, list[dict]] = {}
        for row in rows:
            map_rows.setdefault(str(row.get("map_id") or "未映射"), []).append(row)
        connected_maps = sum(
            any(str(row.get("source_key") or "") in prior_keys for row in subset)
            for subset in map_rows.values()
        )
        overlap = sum(str(row.get("source_key") or "") in prior_keys for row in rows)
        if not previous:
            status, note = "unknown", "首次采集，等待下一批按地图检查稳定ID衔接。"
        elif map_rows and connected_maps == len(map_rows):
            status, note = "complete", f"{connected_maps}张地图均遇到既有稳定ID；仅确认刷新区间衔接。"
        else:
            status, note = "gap", f"仅{connected_maps}/{len(map_rows)}张地图遇到既有稳定ID；每图结果上限可能造成缺口。"
        return [_record("rf4stat_activity", source, status, previous, rows, overlap, note, "reported_at")]

    if source == "rf4stat_posts":
        from .collectors.rf4stat_posts import load_posts_state
        state = load_posts_state()
        history_status = state.get("history_status", "pending")
        status = "complete" if history_status == "target_boundary" else "backfilling"
        note = state.get("stop_reason") or "历史分页游标仍在续采。"
        overlap = sum(str(row.get("source_key") or "") in prior_keys for row in rows)
        return [_record("rf4stat_posts", source, status, previous, rows, overlap, note, "reported_at")]

    return [_record(source, source, "unknown", previous, rows, len(prior_keys), "该来源尚无区间完整性判定规则。", "collected_at")]


def save_refresh_coverage(source: str, rows: Iterable[dict], previous: datetime | None) -> list[dict]:
    records = assess_refresh(source, rows, previous)
    upsert_refresh_coverage(records)
    return records
