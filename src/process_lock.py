from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR


LOCK_PATH = DATA_DIR / "collection.lock"


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except (OSError, PermissionError):
        return False


@contextmanager
def collection_lock(label: str):
    """跨进程互斥；崩溃遗留且 PID 已不存在的锁允许自动回收。"""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    for _ in range(2):
        try:
            descriptor = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            payload = json.dumps({
                "pid": os.getpid(), "label": label,
                "started_at": datetime.now().astimezone().isoformat(),
            }, ensure_ascii=False).encode("utf-8")
            os.write(descriptor, payload)
            os.close(descriptor)
            acquired = True
            break
        except FileExistsError:
            try:
                state = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
                if _pid_exists(int(state.get("pid", 0))):
                    break
                LOCK_PATH.unlink(missing_ok=True)
            except (OSError, ValueError, json.JSONDecodeError):
                break
    try:
        yield acquired
    finally:
        if acquired:
            try:
                state = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
                if int(state.get("pid", 0)) == os.getpid():
                    LOCK_PATH.unlink(missing_ok=True)
            except (OSError, ValueError, json.JSONDecodeError):
                pass
