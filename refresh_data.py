from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

from src.refresh import SOURCES, refresh_all
from src.refresh_reporting import result_state
from src.process_lock import collection_lock


def _write_status(path: Path | None, **values) -> None:
    if path is None:
        return
    current = {}
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}
    current.update(values)
    current["updated_at"] = datetime.now().astimezone().isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="更新俄罗斯钓鱼4本地缓存")
    parser.add_argument("--if-stale", action="store_true", help="仅在缓存过期时刷新")
    parser.add_argument("--status-file", type=Path, help="后台刷新任务状态文件")
    parser.add_argument("--run-id", type=int, default=0, help="后台刷新任务编号")
    parser.add_argument("--source", action="append", choices=list(SOURCES), help="只刷新指定来源，可重复指定")
    args = parser.parse_args()
    with collection_lock("refresh_data") as acquired:
        if not acquired:
            _write_status(
                args.status_file, state="completed", running=False, exit_code=0,
                run_id=args.run_id, stage="已有采集任务运行，本次定时触发已跳过", results={},
            )
            print("已有采集任务运行，本次触发已跳过。")
            return 0
        return _run(args)


def _run(args) -> int:
    _write_status(
        args.status_file, state="running", running=True, pid=os.getpid(), run_id=args.run_id,
        stage="准备刷新", completed_sources=0,
        total_sources=len(set(args.source)) if args.source else len(SOURCES), results={},
    )

    def report_progress(source: str, completed: int, total: int) -> None:
        _write_status(
            args.status_file,
            state="running",
            running=True,
            stage=source,
            completed_sources=completed,
            total_sources=total,
        )

    try:
        results = refresh_all(if_stale=args.if_stale, progress_callback=report_progress, sources=tuple(args.source or ()))
        print(json.dumps(results, ensure_ascii=False, indent=2))
        state = result_state(results)
        exit_code = 1 if state == "failed" else 0
        _write_status(
            args.status_file,
            state=state,
            running=False,
            exit_code=exit_code,
            stage={"completed": "全部来源刷新完成", "partial": "部分来源刷新失败", "failed": "全部来源刷新失败"}[state],
            results=results,
        )
        return exit_code
    except Exception as exc:
        _write_status(
            args.status_file,
            state="failed",
            running=False,
            exit_code=1,
            stage="刷新异常",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())

