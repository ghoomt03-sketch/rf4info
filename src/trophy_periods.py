from __future__ import annotations

import re
from datetime import datetime, timezone

import pandas as pd

from .db import read_frame
from .habit_evidence import rf4stat_habit_records, source_breakdown

PERIODS = [('早晨', '05:00–11:00', 6), ('午间', '11:00–17:00', 6),
           ('傍晚', '17:00–21:00', 4), ('夜间', '21:00–次日05:00', 8)]


def game_period(value) -> str | None:
    match = re.fullmatch(r'(\d{1,2}):(\d{2})', str(value or '').strip())
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        return None
    hour = int(match[1])
    return '早晨' if 5 <= hour < 11 else '午间' if 11 <= hour < 17 else '傍晚' if 17 <= hour < 21 else '夜间'


def period_distributions(records: pd.DataFrame, now=None) -> dict:
    now = pd.Timestamp(now if now is not None else datetime.now(timezone.utc))
    if now.tzinfo is None:
        now = now.tz_localize('UTC')
    now = now.as_unit('ns')
    records = records.copy()
    if 'source_record_key' not in records:
        records['source_record_key'] = records.get('source_id', pd.Series(records.index, index=records.index)).astype(str)
    if 'source_name' not in records:
        records['source_name'] = 'RF4DB渔获'
    rows = records[records['fish_grade'].eq('star')].drop_duplicates('source_record_key').copy()
    rows['period'] = rows['game_time'].map(game_period)
    # RF4DB star stream uses ISO UTC/offset timestamps, not ordinary stream local times.
    rows['capture_time'] = pd.to_datetime(rows['caught_at'], format='mixed', utc=True, errors='coerce')
    valid_date = rows['capture_time'].notna() & rows['capture_time'].le(now)
    excluded_date = int((~valid_date).sum())
    rows = rows[valid_date]
    cutoff7, cutoff30 = now - pd.Timedelta(days=7), now - pd.Timedelta(days=30)
    masks = [('近期7天', rows['capture_time'].gt(cutoff7)),
             ('此前23天', rows['capture_time'].le(cutoff7) & rows['capture_time'].gt(cutoff30)),
             ('30天前历史', rows['capture_time'].le(cutoff30))]
    blocks = []
    for title, mask in masks:
        group = rows[mask]
        eligible = group[group['period'].notna()]
        n = len(eligible)
        table = pd.DataFrame([dict(时段=name, 游戏时间=label, 时段小时数=hours,
                                  上星样本=int(eligible['period'].eq(name).sum()),
                                  样本占比_pct=round(100 * eligible['period'].eq(name).sum() / n, 1) if n else None,
                                  每游戏小时样本数=round(eligible['period'].eq(name).sum() / hours, 2) if n else None)
                              for name, label, hours in PERIODS])
        leaders = table.loc[table['上星样本'].eq(table['上星样本'].max()), '时段'].tolist() if n else []
        blocks.append(dict(title=title, table=table, eligible=n, missing=int(group['period'].isna().sum()),
                           start=group['capture_time'].min(), end=group['capture_time'].max(),
                           leaders='、'.join(leaders), status='无有效样本' if not n else '低样本观察' if n < 25 else '公开样本分布（未校正作钓暴露）'))
    star_rows = records[records['fish_grade'].eq('star')].drop_duplicates('source_record_key')
    return dict(
        blocks=blocks,
        excluded_date=excluded_date,
        star_records=len(star_rows),
        source_breakdown=source_breakdown(star_rows),
    )


def trophy_period_evidence(map_id: str, fish_id: str) -> dict:
    catches = read_frame("""SELECT source_id,fish_grade,game_time,caught_at FROM catch_records
        WHERE map_id=? AND fish_id=? AND fish_grade='star'""", (map_id, fish_id))
    if not catches.empty:
        catches['source_record_key'] = 'RF4DB:' + catches['source_id'].astype(str)
        catches['source_name'] = 'RF4DB渔获'
    rf4stat = rf4stat_habit_records(map_id=map_id, fish_id=fish_id)
    rf4stat = rf4stat[rf4stat['fish_grade'].eq('star')][
        ['source_record_key', 'source_name', 'fish_grade', 'game_time', 'caught_at']
    ]
    combined = pd.concat([catches, rf4stat], ignore_index=True, sort=False)
    result = period_distributions(combined)
    posts = read_frame("""SELECT source_record_id,fish_id,reported_at FROM community_spots
        WHERE source_name='RF4-STAT 帖子' AND map_id=? AND fish_id=?""", (map_id, fish_id))
    result['post_count'] = len(posts.drop_duplicates(['source_record_id', 'fish_id']))
    result['source_url'] = 'RF4DB公开渔获 / RF4-STAT公开上鱼记录'
    return result
