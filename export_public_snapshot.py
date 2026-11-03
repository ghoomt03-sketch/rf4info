from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE = ROOT_DIR / "data" / "rf4.sqlite3"
DEFAULT_OUTPUT = ROOT_DIR / "public_data"
BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")

REQUIRED_TABLES = {
    "maps", "weather_forecasts", "earnings_samples", "map_assets",
    "catch_records", "official_weekly_records", "community_spots",
    "refresh_runs", "refresh_coverage",
}
REQUIRED_NONEMPTY_TABLES = {"maps", "weather_forecasts", "map_assets", "catch_records"}
SHARDED_TABLES = {
    "catch_records": ("caught_at", "collected_at"),
    "community_spots": ("reported_at", "collected_at"),
    "official_weekly_records": ("record_date", "collected_at"),
}


def _repo_map_path(value: str | None) -> str | None:
    if not value:
        return None
    filename = str(value).replace("\\", "/").rsplit("/", 1)[-1]
    return f"data/maps/{filename}" if filename else None


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)


def _row_counts(conn: sqlite3.Connection, tables: set[str]) -> dict[str, int]:
    return {
        table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in sorted(tables)
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _gzip_file(source: Path, destination: Path) -> None:
    # 固定 gzip 头，避免源数据未变时仅因生成时间不同产生新提交。
    with source.open("rb") as source_handle, destination.open("wb") as raw_handle:
        with gzip.GzipFile(
            filename="", mode="wb", fileobj=raw_handle, compresslevel=6, mtime=0
        ) as destination_handle:
            shutil.copyfileobj(source_handle, destination_handle, 1024 * 1024)


def _week_key(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(text[:10], "%Y-%m-%d")
            except ValueError:
                continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=BEIJING_TIMEZONE)
        else:
            parsed = parsed.astimezone(BEIJING_TIMEZONE)
        iso_year, iso_week, _ = parsed.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    return "undated"


def _create_shard_schema(source: sqlite3.Connection, shard: sqlite3.Connection) -> None:
    for table in SHARDED_TABLES:
        sql = source.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
        shard.execute(sql)


def _sanitize(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE official_weekly_records SET player_name=NULL")
    conn.execute("UPDATE community_spots SET raw_payload=NULL")
    conn.execute("UPDATE refresh_runs SET message=NULL")
    for table in ("map_assets", "earnings_samples"):
        rows = conn.execute(
            f'SELECT rowid,map_image_path FROM "{table}" WHERE map_image_path IS NOT NULL'
        ).fetchall()
        conn.executemany(
            f'UPDATE "{table}" SET map_image_path=? WHERE rowid=?',
            [(_repo_map_path(path), rowid) for rowid, path in rows],
        )
    conn.commit()


def _finalize_database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA user_version=4")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("VACUUM")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"{path.name} 完整性检查失败：{integrity}")


def _json_value(value):
    if isinstance(value, pd.Timestamp):
        return value.isoformat() if pd.notna(value) else None
    if pd.isna(value):
        return None
    return value


def _materialize_models(database: Path) -> dict[str, int]:
    """用完整历史生成公开端只读模型，随后原始运行数据才会被裁剪。"""
    import src.db as db_module
    from src.analytics import (
        condition_history_coverage,
        data_source_inventory,
        history_coverage,
        map_temperature_baselines,
        recent_farm_earnings,
    )
    from src.habit_evidence import rf4stat_habit_records
    from src.trophy_periods import period_distributions

    original_path = db_module.DB_PATH
    db_module.DB_PATH = database
    with closing(sqlite3.connect(database)) as metadata_conn:
        generated_at = metadata_conn.execute(
            """SELECT MAX(value) FROM (
                SELECT MAX(COALESCE(caught_at,collected_at)) AS value FROM catch_records
                UNION ALL SELECT MAX(collected_at) FROM weather_forecasts
                UNION ALL SELECT MAX(collected_at) FROM earnings_samples
                UNION ALL SELECT MAX(COALESCE(reported_at,collected_at)) FROM community_spots
            )"""
        ).fetchone()[0] or "1970-01-01T00:00:00+00:00"
    try:
        models = {
            "model_map_temperature_baselines": map_temperature_baselines(),
            "model_history_coverage": history_coverage(),
            "model_condition_history_coverage": condition_history_coverage(),
            "model_source_inventory": data_source_inventory(),
            "model_recent_farm_earnings": recent_farm_earnings(),
        }
        with closing(sqlite3.connect(database)) as conn:
            catches = pd.read_sql_query(
                """SELECT map_id,fish_id,'RF4DB:' || source_id AS source_record_key,
                          'RF4DB渔获' AS source_name,fish_grade,game_time,caught_at
                   FROM catch_records
                   WHERE fish_grade='star' AND map_id IS NOT NULL AND fish_id IS NOT NULL""",
                conn,
            )
        rf4stat = rf4stat_habit_records()
        if not rf4stat.empty:
            rf4stat = rf4stat[rf4stat["fish_grade"].eq("star")][
                ["map_id", "fish_id", "source_record_key", "source_name", "fish_grade", "game_time", "caught_at"]
            ]
        evidence = pd.concat([catches, rf4stat], ignore_index=True, sort=False)
        with closing(sqlite3.connect(database)) as conn:
            posts = pd.read_sql_query(
                """SELECT map_id,fish_id,COUNT(DISTINCT source_record_id || ':' || fish_id) AS post_count
                   FROM community_spots WHERE source_name='RF4-STAT 帖子'
                   AND map_id IS NOT NULL AND fish_id IS NOT NULL GROUP BY map_id,fish_id""",
                conn,
            )
        post_counts = {
            (row.map_id, row.fish_id): int(row.post_count)
            for row in posts.itertuples(index=False)
        }
        trophy_rows = []
        for (map_id, fish_id), group in evidence.groupby(["map_id", "fish_id"], dropna=True):
            result = period_distributions(group, now=pd.to_datetime(generated_at, utc=True))
            payload = {
                "blocks": [
                    {
                        **{key: _json_value(value) for key, value in block.items() if key != "table"},
                        "table": [
                            {key: _json_value(value) for key, value in row.items()}
                            for row in block["table"].to_dict("records")
                        ],
                    }
                    for block in result["blocks"]
                ],
                "excluded_date": result["excluded_date"],
                "star_records": result["star_records"],
                "source_breakdown": result["source_breakdown"],
                "post_count": post_counts.get((map_id, fish_id), 0),
                "source_url": "RF4DB公开渔获 / RF4-STAT公开上鱼记录",
            }
            trophy_rows.append({
                "map_id": map_id,
                "fish_id": fish_id,
                "payload_json": json.dumps(payload, ensure_ascii=False),
                "generated_at": generated_at,
                "sample_count": int(result["star_records"]),
            })
        models["model_trophy_periods"] = pd.DataFrame(
            trophy_rows,
            columns=["map_id", "fish_id", "payload_json", "generated_at", "sample_count"],
        )
        versions = []
        with closing(sqlite3.connect(database)) as conn:
            source_start, source_end = conn.execute(
                "SELECT MIN(COALESCE(caught_at,collected_at)),MAX(COALESCE(caught_at,collected_at)) FROM catch_records"
            ).fetchone()
            for name, frame in models.items():
                frame.to_sql(name, conn, if_exists="replace", index=False)
                versions.append({
                    "model_name": name,
                    "model_version": generated_at,
                    "algorithm_version": "2026-11-03-v1",
                    "trained_from": source_start,
                    "trained_through": source_end,
                    "sample_count": len(frame),
                    "generated_at": generated_at,
                    "confidence": "按各模型既有样本门槛与页面标注解释",
                    "regime_id": None,
                    "note": "由完整历史离线生成；公开端只读，不在启动时重算",
                })
            pd.DataFrame(versions).to_sql(
                "public_model_versions", conn, if_exists="replace", index=False
            )
        return {name: len(frame) for name, frame in models.items()}
    finally:
        db_module.DB_PATH = original_path


def _prune_runtime_database(path: Path) -> None:
    """公开运行库只保留页面直接使用的近期原始数据。"""
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            """
            DELETE FROM catch_records
            WHERE julianday(COALESCE(caught_at,collected_at)) IS NULL
               OR julianday(COALESCE(caught_at,collected_at)) <=
                  (SELECT MAX(julianday(COALESCE(caught_at,collected_at))) - 2.0 FROM catch_records);

            CREATE TEMP TABLE keep_community(source_key TEXT PRIMARY KEY);
            INSERT OR IGNORE INTO keep_community
            SELECT source_key FROM community_spots c
            WHERE validation_status='有效' AND map_id IS NOT NULL AND (
                (source_name='RF4MAP' AND collected_at=(
                    SELECT MAX(collected_at) FROM community_spots WHERE source_name='RF4MAP'))
                OR (source_name='RF4-STAT' AND julianday(reported_at) > (
                    SELECT MAX(julianday(reported_at)) - 2.0 FROM community_spots WHERE source_name='RF4-STAT'))
                OR (source_name='RF4-STAT 帖子' AND julianday(reported_at) > (
                    SELECT MAX(julianday(reported_at)) - 2.0 FROM community_spots WHERE source_name='RF4-STAT 帖子'))
            );
            DELETE FROM community_spots WHERE source_key NOT IN (SELECT source_key FROM keep_community);
            DROP TABLE keep_community;

            DELETE FROM weather_forecasts
            WHERE collected_at != (
                SELECT MAX(w2.collected_at) FROM weather_forecasts w2
                WHERE w2.map_id=weather_forecasts.map_id
            );
            DELETE FROM earnings_samples
            WHERE collected_at != (SELECT MAX(collected_at) FROM earnings_samples);
            DELETE FROM official_weekly_records
            WHERE week_key != (SELECT MAX(week_key) FROM official_weekly_records);
            DELETE FROM refresh_runs WHERE id NOT IN (
                SELECT MAX(id) FROM refresh_runs GROUP BY source
            );
            """
        )
        conn.commit()


def export_public_snapshot(source: Path = DEFAULT_SOURCE, output_dir: Path = DEFAULT_OUTPUT) -> dict:
    source = source.resolve()
    output_dir = output_dir.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"内部工作库不存在：{source}")
    if output_dir == source.parent:
        raise ValueError("公开分片目录不能直接等于内部工作库目录")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent))
    full_copy = staging / "sanitized-full.sqlite3"
    runtime_path = staging / "runtime.sqlite3"
    runtime_bundle = staging / "runtime.sqlite3.gz"
    core_path = staging / "core.sqlite3"
    weeks_dir = staging / "weeks"
    weeks_dir.mkdir()

    try:
        with closing(_connect_readonly(source)) as source_conn, closing(sqlite3.connect(full_copy)) as copy_conn:
            source_conn.backup(copy_conn)

        with closing(sqlite3.connect(full_copy)) as conn:
            tables = _table_names(conn)
            missing = sorted(REQUIRED_TABLES - tables)
            if missing:
                raise RuntimeError(f"内部工作库缺少必要表：{', '.join(missing)}")
            _sanitize(conn)
            source_counts = _row_counts(conn, tables)
            empty = sorted(table for table in REQUIRED_NONEMPTY_TABLES if source_counts.get(table, 0) == 0)
            if empty:
                raise RuntimeError(f"公开数据关键表为空：{', '.join(empty)}")
        model_counts = _materialize_models(full_copy)

        shard_connections: dict[str, sqlite3.Connection] = {}
        shard_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        try:
            with closing(sqlite3.connect(full_copy)) as full_conn:
                for table, time_columns in SHARDED_TABLES.items():
                    columns = [row[1] for row in full_conn.execute(f'PRAGMA table_info("{table}")')]
                    placeholders = ",".join("?" for _ in columns)
                    insert_sql = f'INSERT INTO "{table}" VALUES({placeholders})'
                    time_indexes = [columns.index(column) for column in time_columns]
                    for row in full_conn.execute(f'SELECT * FROM "{table}"'):
                        week = _week_key(*(row[index] for index in time_indexes))
                        shard_conn = shard_connections.get(week)
                        if shard_conn is None:
                            shard_conn = sqlite3.connect(weeks_dir / f"{week}.sqlite3")
                            _create_shard_schema(full_conn, shard_conn)
                            shard_connections[week] = shard_conn
                        shard_conn.execute(insert_sql, row)
                        shard_counts[week][table] += 1
        finally:
            for shard_conn in shard_connections.values():
                shard_conn.commit()
                shard_conn.close()

        with closing(sqlite3.connect(full_copy)) as full_conn, closing(sqlite3.connect(runtime_path)) as runtime_conn:
            full_conn.backup(runtime_conn)
        _prune_runtime_database(runtime_path)
        _finalize_database(runtime_path)
        with closing(sqlite3.connect(runtime_path)) as runtime_conn, closing(sqlite3.connect(core_path)) as core_conn:
            runtime_conn.backup(core_conn)

        with closing(sqlite3.connect(core_path)) as core_conn:
            for table in SHARDED_TABLES:
                core_conn.execute(f'DELETE FROM "{table}"')
            core_conn.commit()

        _finalize_database(core_path)
        for shard_path in sorted(weeks_dir.glob("*.sqlite3")):
            _finalize_database(shard_path)

        shard_total = {
            table: sum(counts.get(table, 0) for counts in shard_counts.values())
            for table in SHARDED_TABLES
        }
        for table in SHARDED_TABLES:
            if shard_total[table] != source_counts[table]:
                raise RuntimeError(
                    f"{table} 分片行数不守恒：源库 {source_counts[table]}，分片 {shard_total[table]}"
                )

        with closing(sqlite3.connect(runtime_path)) as runtime_conn:
            runtime_counts = _row_counts(runtime_conn, _table_names(runtime_conn))
        _gzip_file(runtime_path, runtime_bundle)
        bundle_info = {
            "path": runtime_bundle.relative_to(staging).as_posix(),
            "size_bytes": runtime_bundle.stat().st_size,
            "uncompressed_size_bytes": runtime_path.stat().st_size,
            "sha256": _sha256(runtime_bundle),
            "row_counts": runtime_counts,
        }

        files = []
        for path in [core_path, *sorted(weeks_dir.glob("*.sqlite3"))]:
            relative = path.relative_to(staging).as_posix()
            item = {"path": relative, "size_bytes": path.stat().st_size, "sha256": _sha256(path)}
            if path.parent == weeks_dir:
                item["week"] = path.stem
                item["row_counts"] = dict(sorted(shard_counts[path.stem].items()))
            else:
                with closing(sqlite3.connect(path)) as conn:
                    item["row_counts"] = _row_counts(conn, _table_names(conn))
            files.append(item)

        manifest = {
            "schema_version": 4,
            "generated_at": datetime.now().astimezone().isoformat(),
            "core": "core.sqlite3",
            "runtime_bundle": bundle_info,
            "files": files,
            "source_row_counts": source_counts,
            "runtime_row_counts": runtime_counts,
            "model_row_counts": model_counts,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        full_copy.unlink()
        runtime_path.unlink()

        previous = output_dir.with_name(output_dir.name + ".previous")
        if previous.exists():
            shutil.rmtree(previous)
        if output_dir.exists():
            os.replace(output_dir, previous)
        try:
            shutil.copytree(staging, output_dir)
        except Exception:
            if output_dir.exists():
                shutil.rmtree(output_dir, ignore_errors=True)
            if previous.exists():
                os.replace(previous, output_dir)
            raise
        shutil.rmtree(staging)
        if previous.exists():
            shutil.rmtree(previous)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="生成脱敏的公开核心库与自然周 SQLite 分片")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    manifest = export_public_snapshot(args.source, args.output)
    total_size = sum(item["size_bytes"] for item in manifest["files"])
    bundle_size = manifest["runtime_bundle"]["size_bytes"]
    print(
        f"公开数据已生成：{args.output}；{len(manifest['files']) - 1} 个周分片，"
        f"分片合计 {total_size / 1024 / 1024:.2f} MiB，"
        f"启动包 {bundle_size / 1024 / 1024:.2f} MiB"
    )


if __name__ == "__main__":
    main()
