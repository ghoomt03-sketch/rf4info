from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from typing import Iterable

import pandas as pd

from .config import DATA_DIR, DB_PATH, MAPS, PUBLIC_MODE


SCHEMA = """
CREATE TABLE IF NOT EXISTS maps (
    map_id TEXT PRIMARY KEY,
    name_zh TEXT NOT NULL,
    name_en TEXT NOT NULL,
    unlock_level INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS weather_forecasts (
    map_id TEXT NOT NULL,
    forecast_slot TEXT NOT NULL,
    horizon INTEGER NOT NULL,
    weather_code TEXT,
    forecast_day_temperature REAL,
    wind_speed REAL,
    collected_at TEXT NOT NULL,
    source_url TEXT NOT NULL,
    PRIMARY KEY (map_id, forecast_slot, collected_at)
);
CREATE INDEX IF NOT EXISTS idx_weather_latest ON weather_forecasts(map_id, horizon, collected_at);

CREATE TABLE IF NOT EXISTS earnings_samples (
    source_key TEXT PRIMARY KEY,
    rank_no INTEGER,
    map_id TEXT,
    silver_per_hour REAL,
    catch_count INTEGER,
    coverage_pct REAL,
    composition TEXT,
    species_composition TEXT,
    coordinates TEXT,
    spot_details TEXT,
    map_calibration TEXT,
    map_image_path TEXT,
    period TEXT,
    collected_at TEXT NOT NULL,
    source_url TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_earnings_latest ON earnings_samples(map_id, collected_at);

CREATE TABLE IF NOT EXISTS map_assets (
    map_id TEXT PRIMARY KEY,
    map_calibration TEXT NOT NULL,
    map_image_path TEXT NOT NULL,
    image_url TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    source_url TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS catch_records (
    source_id TEXT PRIMARY KEY,
    fish_id TEXT,
    fish_name_en TEXT,
    map_id TEXT,
    coordinate TEXT,
    clip TEXT,
    game_time TEXT,
    depth TEXT,
    weather_code TEXT,
    catch_temperature REAL,
    rig TEXT,
    baits TEXT,
    fish_grade TEXT,
    caught_at TEXT,
    collected_at TEXT NOT NULL,
    source_url TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_catches_map_time ON catch_records(map_id, caught_at);

CREATE TABLE IF NOT EXISTS official_weekly_records (
    source_key TEXT PRIMARY KEY,
    week_key TEXT NOT NULL,
    region TEXT NOT NULL,
    fish_name TEXT NOT NULL,
    weight_kg REAL,
    map_name TEXT,
    bait_name TEXT,
    player_name TEXT,
    record_date TEXT,
    collected_at TEXT NOT NULL,
    source_url TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_weekly_fish_date ON official_weekly_records(fish_name, record_date);

CREATE TABLE IF NOT EXISTS community_spots (
    source_key TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    source_record_id TEXT,
    evidence_type TEXT NOT NULL,
    map_id TEXT,
    map_name_raw TEXT,
    coordinate TEXT,
    spot_label TEXT,
    fish_id TEXT,
    fish_name TEXT,
    fish_name_raw TEXT,
    baits TEXT,
    rig TEXT,
    clip TEXT,
    depth TEXT,
    quality_label TEXT,
    catch_count INTEGER,
    reported_at TEXT,
    collected_at TEXT NOT NULL,
    validation_status TEXT NOT NULL,
    raw_payload TEXT,
    source_url TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_community_source_latest ON community_spots(source_name, collected_at);
CREATE INDEX IF NOT EXISTS idx_community_map_fish ON community_spots(map_id, fish_name, reported_at);

CREATE TABLE IF NOT EXISTS refresh_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    success INTEGER NOT NULL,
    row_count INTEGER NOT NULL,
    message TEXT
);

CREATE TABLE IF NOT EXISTS refresh_coverage (
    stream TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    previous_success_at TEXT,
    earliest_fetched_at TEXT,
    latest_fetched_at TEXT,
    fetched_rows INTEGER NOT NULL DEFAULT 0,
    overlap_rows INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS behavior_regimes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    map_id TEXT NOT NULL,
    fish_id TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    is_current INTEGER NOT NULL DEFAULT 1,
    change_type TEXT,
    confidence TEXT,
    evidence TEXT
);
"""


def ensure_database() -> None:
    if PUBLIC_MODE:
        if not DB_PATH.is_file():
            raise FileNotFoundError(f"公开数据快照不存在：{DB_PATH}")
        with closing(sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)) as conn:
            required = {
                "maps", "weather_forecasts", "earnings_samples", "map_assets",
                "catch_records", "official_weekly_records", "community_spots",
                "refresh_runs", "refresh_coverage",
            }
            existing = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            missing = sorted(required - existing)
            if missing:
                raise RuntimeError(f"公开数据快照缺少表：{', '.join(missing)}")
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.executescript(SCHEMA)
        earning_columns = {row[1] for row in conn.execute("PRAGMA table_info(earnings_samples)")}
        weekly_columns = {row[1] for row in conn.execute("PRAGMA table_info(official_weekly_records)")}
        community_columns = {row[1] for row in conn.execute("PRAGMA table_info(community_spots)")}
        for name, kind in {"fish_id": "TEXT", "map_id": "TEXT", "bait_ids": "TEXT",
                           "category": "TEXT", "source_name": "TEXT", "trophy_threshold_kg": "REAL",
                           "threshold_source_url": "TEXT", "threshold_checked_at": "TEXT"}.items():
            if name not in weekly_columns:
                conn.execute(f"ALTER TABLE official_weekly_records ADD COLUMN {name} {kind}")
        if "coordinates" not in earning_columns:
            conn.execute("ALTER TABLE earnings_samples ADD COLUMN coordinates TEXT")
        if "species_composition" not in earning_columns:
            conn.execute("ALTER TABLE earnings_samples ADD COLUMN species_composition TEXT")
        if "spot_details" not in earning_columns:
            conn.execute("ALTER TABLE earnings_samples ADD COLUMN spot_details TEXT")
        if "map_calibration" not in earning_columns:
            conn.execute("ALTER TABLE earnings_samples ADD COLUMN map_calibration TEXT")
        if "map_image_path" not in earning_columns:
            conn.execute("ALTER TABLE earnings_samples ADD COLUMN map_image_path TEXT")
        if "spot_label" not in community_columns:
            conn.execute("ALTER TABLE community_spots ADD COLUMN spot_label TEXT")
        legacy_holes = conn.execute(
            """SELECT source_key,raw_payload,validation_status FROM community_spots
               WHERE source_name='RF4-STAT 帖子' AND coordinate IS NULL
               AND spot_label IS NULL AND raw_payload IS NOT NULL"""
        ).fetchall()
        for source_key, raw_payload, validation_status in legacy_holes:
            try:
                position_raw = str(json.loads(raw_payload).get("position_raw") or "").strip()
            except (TypeError, json.JSONDecodeError):
                continue
            match = re.fullmatch(r"(\d{1,3})\s*(?:坑)?", position_raw)
            if not match:
                continue
            remaining = [
                item for item in str(validation_status or "").split("；")
                if item and item != "坐标无效"
            ]
            conn.execute(
                "UPDATE community_spots SET spot_label=?,validation_status=? WHERE source_key=?",
                (f"{int(match.group(1))}坑", "；".join(remaining) or "有效", source_key),
            )
        conn.executemany(
            "INSERT OR REPLACE INTO maps(map_id,name_zh,name_en,unlock_level) VALUES(?,?,?,?)",
            [(key, *value) for key, value in MAPS.items()],
        )
        history_state_path = DATA_DIR / "history_backfill_state.json"
        history_coverage = conn.execute(
            "SELECT 1 FROM refresh_coverage WHERE stream='rf4db_catches_normal_history'"
        ).fetchone()
        if history_coverage is None and history_state_path.exists():
            try:
                history_state = json.loads(history_state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                history_state = {}
            if history_state:
                completed = bool(history_state.get("normal_completed"))
                next_page = max(1, int(history_state.get("next_record_page", 1) or 1))
                conn.execute(
                    """INSERT INTO refresh_coverage
                    (stream,source,status,previous_success_at,earliest_fetched_at,latest_fetched_at,
                     fetched_rows,overlap_rows,note,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "rf4db_catches_normal_history", "rf4db_catches",
                        "complete" if completed else "backfilling", None,
                        history_state.get("normal_oldest_seen"), None,
                        int(history_state.get("last_batch_rows", 0) or 0), 0,
                        "90日目标已达到。" if completed else f"旧缺口续传中；下一批从第{next_page}页开始。",
                        history_state.get("updated_at") or datetime.now().astimezone().isoformat(),
                    ),
                )


@contextmanager
def connect():
    ensure_database()
    if PUBLIC_MODE:
        conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=ON")
    else:
        conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        if not PUBLIC_MODE:
            conn.commit()
    finally:
        conn.close()


def upsert_weather(rows: Iterable[dict]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    sql = """INSERT OR REPLACE INTO weather_forecasts
        (map_id,forecast_slot,horizon,weather_code,forecast_day_temperature,wind_speed,collected_at,source_url)
        VALUES(:map_id,:forecast_slot,:horizon,:weather_code,:forecast_day_temperature,:wind_speed,:collected_at,:source_url)"""
    with connect() as conn:
        conn.executemany(sql, rows)
    return len(rows)


def upsert_earnings(rows: Iterable[dict]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    sql = """INSERT OR REPLACE INTO earnings_samples
        (source_key,rank_no,map_id,silver_per_hour,catch_count,coverage_pct,composition,species_composition,coordinates,spot_details,map_calibration,map_image_path,period,collected_at,source_url)
        VALUES(:source_key,:rank_no,:map_id,:silver_per_hour,:catch_count,:coverage_pct,:composition,:species_composition,:coordinates,:spot_details,:map_calibration,:map_image_path,:period,:collected_at,:source_url)"""
    with connect() as conn:
        conn.executemany(sql, rows)
    return len(rows)


def upsert_map_assets(rows: Iterable[dict]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    sql = """INSERT OR REPLACE INTO map_assets
        (map_id,map_calibration,map_image_path,image_url,collected_at,source_url)
        VALUES(:map_id,:map_calibration,:map_image_path,:image_url,:collected_at,:source_url)"""
    with connect() as conn:
        conn.executemany(sql, rows)
    return len(rows)


def upsert_catches(rows: Iterable[dict]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    sql = """INSERT OR REPLACE INTO catch_records
        (source_id,fish_id,fish_name_en,map_id,coordinate,clip,game_time,depth,weather_code,catch_temperature,rig,baits,fish_grade,caught_at,collected_at,source_url)
        VALUES(:source_id,:fish_id,:fish_name_en,:map_id,:coordinate,:clip,:game_time,:depth,:weather_code,:catch_temperature,:rig,:baits,:fish_grade,:caught_at,:collected_at,:source_url)"""
    with connect() as conn:
        conn.executemany(sql, rows)
    from .normalization import reconcile_catch_bait_names, reconcile_community_fish_names
    reconcile_catch_bait_names()
    reconcile_community_fish_names()
    return len(rows)


def upsert_official_weekly(rows: Iterable[dict]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    sql = """INSERT OR REPLACE INTO official_weekly_records
        (source_key,week_key,region,fish_name,weight_kg,map_name,bait_name,player_name,record_date,collected_at,source_url,
         fish_id,map_id,bait_ids,category,source_name,trophy_threshold_kg,threshold_source_url,threshold_checked_at)
        VALUES(:source_key,:week_key,:region,:fish_name,:weight_kg,:map_name,:bait_name,:player_name,:record_date,:collected_at,:source_url,
         :fish_id,:map_id,:bait_ids,:category,:source_name,:trophy_threshold_kg,:threshold_source_url,:threshold_checked_at)"""
    extra = ("fish_id", "map_id", "bait_ids", "category", "source_name", "trophy_threshold_kg", "threshold_source_url", "threshold_checked_at")
    rows = [{**dict.fromkeys(extra), **row} for row in rows]
    with connect() as conn:
        backup = DATA_DIR / "rf4-before-weekly-v2.sqlite3"
        if not backup.exists():
            with closing(sqlite3.connect(backup)) as destination:
                conn.backup(destination)
        scopes = {(row["week_key"], row["region"]) for row in rows}
        conn.executemany(
            "DELETE FROM official_weekly_records WHERE week_key=? AND region=?",
            scopes,
        )
        conn.executemany(sql, rows)
    return len(rows)


def upsert_community_spots(rows: Iterable[dict]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    sql = """INSERT OR REPLACE INTO community_spots
        (source_key,source_name,source_record_id,evidence_type,map_id,map_name_raw,coordinate,spot_label,
         fish_id,fish_name,fish_name_raw,baits,rig,clip,depth,quality_label,catch_count,
         reported_at,collected_at,validation_status,raw_payload,source_url)
        VALUES(:source_key,:source_name,:source_record_id,:evidence_type,:map_id,:map_name_raw,:coordinate,:spot_label,
         :fish_id,:fish_name,:fish_name_raw,:baits,:rig,:clip,:depth,:quality_label,:catch_count,
         :reported_at,:collected_at,:validation_status,:raw_payload,:source_url)"""
    rows = [{"spot_label": None, **row} for row in rows]
    with connect() as conn:
        conn.executemany(sql, rows)
    from .normalization import reconcile_community_fish_names
    reconcile_community_fish_names()
    return len(rows)


def log_refresh(source: str, started_at: str, success: bool, row_count: int, message: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO refresh_runs(source,started_at,finished_at,success,row_count,message) VALUES(?,?,?,?,?,?)",
            (source, started_at, datetime.now().astimezone().isoformat(), int(success), row_count, message[:1000]),
        )


def read_frame(query: str, params: tuple = ()) -> pd.DataFrame:
    with connect() as conn:
        return pd.read_sql_query(query, conn, params=params)


def last_success(source: str) -> datetime | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT finished_at FROM refresh_runs WHERE source=? AND success=1 ORDER BY id DESC LIMIT 1",
            (source,),
        ).fetchone()
    return datetime.fromisoformat(row[0]) if row else None


def existing_record_keys(source: str, rows: Iterable[dict]) -> set[str]:
    """在本批写入前查询已存在的稳定记录键，用于判断分页是否衔接。"""
    rows = list(rows)
    if source == "rf4db_catches":
        keys = [str(row.get("source_id") or "") for row in rows]
        table, column = "catch_records", "source_id"
    elif source in {"rf4stat_activity", "rf4stat_posts", "rf4map_points"}:
        keys = [str(row.get("source_key") or "") for row in rows]
        table, column = "community_spots", "source_key"
    elif source == "official_weekly_cn":
        keys = [str(row.get("source_key") or "") for row in rows]
        table, column = "official_weekly_records", "source_key"
    elif source == "rf4db_earnings":
        keys = [str(row.get("source_key") or "") for row in rows]
        table, column = "earnings_samples", "source_key"
    else:
        return set()
    keys = list(dict.fromkeys(key for key in keys if key))
    if not keys:
        return set()
    found: set[str] = set()
    with connect() as conn:
        for offset in range(0, len(keys), 500):
            batch = keys[offset:offset + 500]
            placeholders = ",".join("?" for _ in batch)
            found.update(
                str(row[0]) for row in conn.execute(
                    f"SELECT {column} FROM {table} WHERE {column} IN ({placeholders})", batch
                )
            )
    return found


def recent_catch_ids(fish_grade: str, limit: int = 5000) -> set[str]:
    operator = "=" if fish_grade == "normal" else "<>"
    with connect() as conn:
        rows = conn.execute(
            f"SELECT source_id FROM catch_records WHERE fish_grade {operator} 'normal' "
            "ORDER BY rowid DESC LIMIT ?", (max(1, int(limit)),),
        ).fetchall()
    return {str(row[0]) for row in rows}


def upsert_refresh_coverage(records: Iterable[dict]) -> None:
    records = list(records)
    if not records:
        return
    sql = """INSERT OR REPLACE INTO refresh_coverage
        (stream,source,status,previous_success_at,earliest_fetched_at,latest_fetched_at,
         fetched_rows,overlap_rows,note,updated_at)
        VALUES(:stream,:source,:status,:previous_success_at,:earliest_fetched_at,:latest_fetched_at,
         :fetched_rows,:overlap_rows,:note,:updated_at)"""
    with connect() as conn:
        conn.executemany(sql, records)

