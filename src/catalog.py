from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing

from .config import DATA_DIR, DB_PATH, FISH_ZH


RF4DB_FISH_CATALOG_PATH = DATA_DIR / "fish_names_rf4db_zh.json"


def rf4db_fish_catalog() -> dict[str, str]:
    """读取本地目录与公开快照，形成稳定 ID→中文标准名主表。"""
    catalog = dict(FISH_ZH)
    try:
        cached = json.loads(RF4DB_FISH_CATALOG_PATH.read_text(encoding="utf-8"))
        if isinstance(cached, dict):
            catalog.update({str(key): str(value) for key, value in cached.items()})
    except (OSError, json.JSONDecodeError):
        pass
    if not DB_PATH.exists():
        return catalog
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            rows = conn.execute(
                """SELECT fish_id, fish_name_en, COUNT(*) AS samples
                   FROM catch_records
                   WHERE fish_id IS NOT NULL AND fish_name_en IS NOT NULL
                   GROUP BY fish_id, fish_name_en
                   ORDER BY samples ASC"""
            ).fetchall()
    except sqlite3.Error:
        return catalog
    for fish_id, fish_name, _ in rows:
        name = str(fish_name or "").strip()
        if fish_id and re.search(r"[\u4e00-\u9fff]", name):
            catalog[str(fish_id)] = name
    return catalog
