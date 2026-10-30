from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from export_public_snapshot import _week_key, export_public_snapshot
from rf4_public_runtime import DEFAULT_RUNTIME, prepare_public_runtime
from src.db import SCHEMA


class PublicSnapshotTests(unittest.TestCase):
    def test_default_runtime_uses_reusable_system_temp_directory(self):
        expected_parent = Path(tempfile.gettempdir()) / "rf4info"
        self.assertEqual(DEFAULT_RUNTIME.parent, expected_parent)

    def test_week_key_uses_beijing_natural_week(self):
        self.assertEqual(_week_key("2026-11-01T16:30:00Z"), "2026-W45")
        self.assertEqual(_week_key("2026-11-01T23:30:00+08:00"), "2026-W44")

    def test_export_removes_private_fields_and_rewrites_map_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.sqlite3"
            output_dir = root / "public_data"
            runtime = root / "runtime" / "public.sqlite3"
            with closing(sqlite3.connect(source)) as conn:
                conn.executescript(SCHEMA)
                conn.execute("INSERT INTO maps VALUES('map-a','地图','Map',1)")
                conn.execute(
                    "INSERT INTO weather_forecasts VALUES(?,?,?,?,?,?,?,?)",
                    ('map-a', '2026-10-30T10:00:00+08:00', 0, 'clear', 10.0, 1.0,
                     '2026-10-30T09:00:00+08:00', 'https://example.test/weather'),
                )
                conn.execute(
                    "INSERT INTO map_assets VALUES(?,?,?,?,?,?)",
                    ('map-a', '{}', r'G:\\project\\data\\maps\\map-a.webp',
                     'https://example.test/map.webp', '2026-10-30T09:00:00+08:00',
                     'https://example.test/map'),
                )
                conn.execute(
                    "INSERT INTO catch_records(source_id,map_id,collected_at,source_url) VALUES(?,?,?,?)",
                    ('catch-1', 'map-a', '2026-10-30T09:00:00+08:00', 'https://example.test/catch'),
                )
                conn.execute(
                    "INSERT INTO official_weekly_records(source_key,week_key,region,fish_name,player_name,collected_at,source_url) VALUES(?,?,?,?,?,?,?)",
                    ('weekly-1', '2026-10-26', 'CN', '鱼', '玩家姓名',
                     '2026-10-30T09:00:00+08:00', 'https://example.test/weekly'),
                )
                conn.execute(
                    "INSERT INTO community_spots(source_key,source_name,evidence_type,collected_at,validation_status,raw_payload,source_url) VALUES(?,?,?,?,?,?,?)",
                    ('spot-1', '社区', 'post', '2026-10-30T09:00:00+08:00', '有效',
                     '{"private":"raw"}', 'https://example.test/spot'),
                )
                conn.execute(
                    "INSERT INTO refresh_runs(source,started_at,finished_at,success,row_count,message) VALUES(?,?,?,?,?,?)",
                    ('source', 'a', 'b', 1, 1, r'G:\\private\\log.txt'),
                )
                conn.execute(
                    "INSERT INTO refresh_coverage(stream,source,status,fetched_rows,overlap_rows,note,updated_at) VALUES(?,?,?,?,?,?,?)",
                    ('stream', 'source', 'complete', 1, 1, '完整', '2026-10-30T09:00:00+08:00'),
                )
                conn.commit()

            manifest = export_public_snapshot(source, output_dir)
            stored_manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(stored_manifest, manifest)
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(len([item for item in manifest["files"] if "week" in item]), 1)
            for item in manifest["files"]:
                path = output_dir / item["path"]
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item["sha256"])

            progress_updates = []
            prepare_public_runtime(
                output_dir,
                runtime,
                progress_callback=lambda value, text: progress_updates.append((value, text)),
            )
            self.assertEqual(progress_updates[-1], (1.0, "公开数据加载完成"))
            self.assertTrue(any("组装周数据" in text for _, text in progress_updates))
            self.assertEqual(progress_updates, sorted(progress_updates, key=lambda item: item[0]))
            with closing(sqlite3.connect(runtime)) as conn:
                self.assertIsNone(conn.execute(
                    "SELECT player_name FROM official_weekly_records"
                ).fetchone()[0])
                self.assertIsNone(conn.execute(
                    "SELECT raw_payload FROM community_spots"
                ).fetchone()[0])
                self.assertIsNone(conn.execute("SELECT message FROM refresh_runs").fetchone()[0])
                self.assertEqual(
                    conn.execute("SELECT map_image_path FROM map_assets").fetchone()[0],
                    "data/maps/map-a.webp",
                )
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()
