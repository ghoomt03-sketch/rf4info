from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
from contextlib import closing
from pathlib import Path
from typing import Callable


ROOT_DIR = Path(__file__).resolve().parent
PUBLIC_DATA_DIR = ROOT_DIR / "public_data"
DEFAULT_RUNTIME = Path(tempfile.gettempdir()) / "rf4info" / "public.sqlite3"
SHARDED_TABLES = ("catch_records", "community_spots", "official_weekly_records")
_PREPARE_LOCK = threading.Lock()
ProgressCallback = Callable[[float, str], None]


def _report_progress(callback: ProgressCallback | None, value: float, text: str) -> None:
    if callback is not None:
        callback(max(0.0, min(value, 1.0)), text)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_public_runtime(
    data_dir: Path = PUBLIC_DATA_DIR,
    runtime_path: Path = DEFAULT_RUNTIME,
    progress_callback: ProgressCallback | None = None,
) -> Path:
    with _PREPARE_LOCK:
        return _prepare_public_runtime(data_dir, runtime_path, progress_callback)


def _prepare_public_runtime(
    data_dir: Path,
    runtime_path: Path,
    progress_callback: ProgressCallback | None,
) -> Path:
    _report_progress(progress_callback, 0.02, "正在读取公开数据清单")
    data_dir = data_dir.resolve()
    runtime_path = runtime_path.resolve()
    manifest_path = data_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 2:
        raise RuntimeError("不支持的公开数据清单版本")

    version = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    version_path = runtime_path.with_suffix(".version")
    if runtime_path.is_file() and version_path.is_file():
        if version_path.read_text(encoding="utf-8").strip() == version:
            _report_progress(progress_callback, 1.0, "已复用校验通过的公开数据")
            return runtime_path

    files = manifest["files"]
    for file_index, item in enumerate(files, start=1):
        path = data_dir / item["path"]
        if not path.is_file():
            raise FileNotFoundError(f"公开数据分片缺失：{item['path']}")
        if _sha256(path) != item["sha256"]:
            raise RuntimeError(f"公开数据分片哈希不匹配：{item['path']}")
        _report_progress(
            progress_callback,
            0.05 + 0.35 * file_index / max(len(files), 1),
            f"正在校验公开数据（{file_index}/{len(files)}）",
        )

    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = runtime_path.with_suffix(".tmp")
    temporary.unlink(missing_ok=True)
    _report_progress(progress_callback, 0.43, "正在准备核心数据")
    shutil.copy2(data_dir / manifest["core"], temporary)

    try:
        with closing(sqlite3.connect(temporary)) as conn:
            weekly_files = [item for item in files if "week" in item]
            for index, item in enumerate(weekly_files):
                shard_path = data_dir / item["path"]
                alias = f"shard_{index}"
                conn.execute(f"ATTACH DATABASE ? AS {alias}", (str(shard_path),))
                try:
                    for table in SHARDED_TABLES:
                        if int(item.get("row_counts", {}).get(table, 0)):
                            conn.execute(
                                f'INSERT OR REPLACE INTO main."{table}" SELECT * FROM {alias}."{table}"'
                            )
                    conn.commit()
                finally:
                    conn.execute(f"DETACH DATABASE {alias}")
                _report_progress(
                    progress_callback,
                    0.45 + 0.40 * (index + 1) / max(len(weekly_files), 1),
                    f"正在组装周数据（{index + 1}/{len(weekly_files)}）",
                )
            _report_progress(progress_callback, 0.90, "正在检查数据库完整性")
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("组装后的公开运行库完整性检查失败")
            for table in SHARDED_TABLES:
                actual = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                expected = manifest["source_row_counts"][table]
                if actual != expected:
                    raise RuntimeError(f"{table} 组装行数不一致：{actual} != {expected}")
        os.replace(temporary, runtime_path)
        version_temporary = version_path.with_suffix(".version.tmp")
        version_temporary.write_text(version + "\n", encoding="utf-8")
        os.replace(version_temporary, version_path)
        _report_progress(progress_callback, 1.0, "公开数据加载完成")
        return runtime_path
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
