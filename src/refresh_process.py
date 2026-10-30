from __future__ import annotations

import os
import json
import signal
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import IO

from .config import DATA_DIR, ROOT_DIR
from .refresh_reporting import SOURCE_LABELS, result_state


_LOCK = threading.Lock()
_PROCESS: subprocess.Popen | None = None
_LOG_HANDLE: IO[str] | None = None
_LOG_PATH: Path | None = None
_RUN_ID = 0
_CANCELLED = False
_STATUS_PATH = DATA_DIR / "refresh_job.json"


def _read_status() -> dict:
    if not _STATUS_PATH.exists():
        return {}
    try:
        return json.loads(_STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_status(**values) -> dict:
    status = _read_status()
    status.update(values)
    status["updated_at"] = datetime.now().astimezone().isoformat()
    _STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = _STATUS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(_STATUS_PATH)
    return status


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        ctypes.windll.kernel32.CloseHandle(process)
        return True
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError, PermissionError):
        return False
    return True


def _close_log() -> None:
    global _LOG_HANDLE
    if _LOG_HANDLE is not None:
        _LOG_HANDLE.close()
        _LOG_HANDLE = None


def get_refresh_state() -> dict:
    with _LOCK:
        persisted = _read_status()
        if _PROCESS is None and persisted.get("state") == "running":
            persisted_pid = int(persisted.get("pid", 0) or 0)
            if not _pid_exists(persisted_pid):
                persisted = _write_status(
                    state="failed",
                    running=False,
                    exit_code=None,
                    stage="后台进程已结束，但未写入完成状态",
                )
        exit_code = _PROCESS.poll() if _PROCESS is not None else None
        running = _PROCESS is not None and exit_code is None
        if _PROCESS is not None and not running:
            _close_log()
        if _PROCESS is not None and not running and persisted.get("state") == "running":
            persisted = _write_status(
                state="completed" if exit_code == 0 else "failed",
                running=False,
                exit_code=exit_code,
                stage="刷新完成" if exit_code == 0 else "刷新进程异常退出",
            )
        if persisted:
            if persisted.get("state") == "completed" and persisted.get("results"):
                # 兼容升级前被记录为“完成”的部分失败任务。
                persisted["state"] = result_state(persisted["results"])
            persisted.setdefault("running", persisted.get("state") == "running")
            persisted.setdefault("exit_code", exit_code)
            persisted.setdefault("run_id", _RUN_ID)
            persisted.setdefault("log_path", str(_LOG_PATH) if _LOG_PATH else "")
            return persisted
        return {"state": "idle", "running": False, "exit_code": None, "run_id": 0, "log_path": ""}


def start_refresh(sources: tuple[str, ...] = ()) -> dict:
    from .refresh import SOURCES
    global _PROCESS, _LOG_HANDLE, _LOG_PATH, _RUN_ID, _CANCELLED
    with _LOCK:
        if any(source not in SOURCES for source in sources):
            raise ValueError("不支持的刷新来源")
        if _PROCESS is not None and _PROCESS.poll() is None:
            return {"started": False, "message": "刷新已经在运行"}
        _close_log()
        log_dir = DATA_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        _LOG_PATH = log_dir / f"refresh-{stamp}.log"
        _LOG_HANDLE = _LOG_PATH.open("w", encoding="utf-8")
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        persisted = _read_status()
        _RUN_ID = max(_RUN_ID, int(persisted.get("run_id", 0))) + 1
        _PROCESS = subprocess.Popen(
            [
                sys.executable,
                str(ROOT_DIR / "refresh_data.py"),
                "--status-file",
                str(_STATUS_PATH),
                "--run-id",
                str(_RUN_ID),
                *[value for source in sources for value in ("--source", source)],
            ],
            cwd=ROOT_DIR,
            stdout=_LOG_HANDLE,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        _CANCELLED = False
        _write_status(
            state="running",
            running=True,
            pid=_PROCESS.pid,
            run_id=_RUN_ID,
            stage="启动后台进程",
            completed_sources=0,
            total_sources=len(set(sources)) if sources else len(SOURCES),
            results={},
            log_path=str(_LOG_PATH),
            exit_code=None,
        )
        return {"started": True, "message": "后台刷新已开始", "run_id": _RUN_ID}


def stop_refresh() -> dict:
    global _CANCELLED
    with _LOCK:
        if _PROCESS is None:
            persisted = _read_status()
            persisted_pid = int(persisted.get("pid", 0) or 0)
            if persisted.get("state") == "running" and _pid_exists(persisted_pid):
                os.kill(persisted_pid, signal.SIGTERM)
                _CANCELLED = True
                _write_status(state="cancelled", running=False, exit_code=None, stage="用户已中止")
                return {"stopped": True, "message": "刷新已中止，现有缓存保持不变"}
            _close_log()
            return {"stopped": False, "message": "当前没有正在运行的刷新"}
        if _PROCESS.poll() is not None:
            _close_log()
            return {"stopped": False, "message": "当前没有正在运行的刷新"}
        _PROCESS.terminate()
        try:
            _PROCESS.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _PROCESS.kill()
            _PROCESS.wait(timeout=3)
        _CANCELLED = True
        _close_log()
        _write_status(
            state="cancelled",
            running=False,
            exit_code=_PROCESS.returncode,
            stage="用户已中止",
        )
        return {"stopped": True, "message": "刷新已中止，现有缓存保持不变"}
