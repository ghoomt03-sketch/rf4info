from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from export_public_snapshot import DEFAULT_SOURCE, export_public_snapshot
from src.config import DATA_DIR, ROOT_DIR


PUBLIC_DIR = ROOT_DIR / "public_data"
STATE_PATH = DATA_DIR / "public_publish_state.json"
LOCK_PATH = DATA_DIR / "public_publish.lock"
EXPECTED_REMOTE_SUFFIX = "github.com/ghoomt03-sketch/rf4info.git"
COMMIT_PREFIX = "Update public data"


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _log(message: str) -> None:
    log_dir = DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"public-publish-{datetime.now():%Y%m%d}.log"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{_now()}] {message}\n")


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


def _write_state(**values: object) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    current: dict[str, object] = {}
    try:
        current = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    current.update(values)
    temporary = STATE_PATH.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, STATE_PATH)


def _read_state() -> dict[str, object]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


@contextmanager
def _publish_lock() -> Iterator[bool]:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    try:
        for _ in range(2):
            try:
                descriptor = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(descriptor, f"{os.getpid()}\n".encode())
                break
            except FileExistsError:
                try:
                    pid = int(LOCK_PATH.read_text(encoding="utf-8").strip())
                    if _pid_exists(pid):
                        break
                except (OSError, ValueError):
                    pass
                LOCK_PATH.unlink(missing_ok=True)
        if descriptor is None:
            yield False
            return
        yield True
    finally:
        if descriptor is not None:
            os.close(descriptor)
            LOCK_PATH.unlink(missing_ok=True)


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        ["git", *args], cwd=ROOT_DIR, capture_output=True, text=True,
        timeout=180, env=environment,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"git {' '.join(args)} 失败：{detail}")
    return result


def _snapshot_signature(manifest: dict) -> str:
    stable = dict(manifest)
    stable.pop("generated_at", None)
    return json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _source_fingerprint(path: Path = DEFAULT_SOURCE) -> dict[str, int | str]:
    stat = path.stat()
    with path.open("rb") as handle:
        header = handle.read(100)
        if len(header) < 100 or header[:16] != b"SQLite format 3\x00":
            raise RuntimeError("内部工作库不是有效的 SQLite 3 文件")
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "size": stat.st_size,
        "content_sha256": digest.hexdigest(),
    }


def _load_manifest(directory: Path) -> dict | None:
    try:
        return json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _validate_staged_paths(paths: list[str]) -> None:
    invalid = [path for path in paths if path != "public_data" and not path.startswith("public_data/")]
    if invalid:
        raise RuntimeError(f"检测到公开目录之外的暂存文件：{', '.join(invalid)}")


def _replace_public_data(candidate: Path) -> Path:
    previous = PUBLIC_DIR.with_name("public_data.previous")
    if previous.exists():
        shutil.rmtree(previous)
    if PUBLIC_DIR.exists():
        os.replace(PUBLIC_DIR, previous)
    try:
        shutil.copytree(candidate, PUBLIC_DIR)
    except Exception:
        if PUBLIC_DIR.exists():
            shutil.rmtree(PUBLIC_DIR, ignore_errors=True)
        if previous.exists():
            os.replace(previous, PUBLIC_DIR)
        raise
    return previous


def _restore_previous(previous: Path) -> None:
    if PUBLIC_DIR.exists():
        shutil.rmtree(PUBLIC_DIR, ignore_errors=True)
    if previous.exists():
        os.replace(previous, PUBLIC_DIR)


def _assert_repository_ready() -> None:
    if _git("branch", "--show-current").stdout.strip() != "main":
        raise RuntimeError("自动发布只允许在 main 分支运行")
    remote = _git("remote", "get-url", "origin").stdout.strip().replace("\\", "/")
    if EXPECTED_REMOTE_SUFFIX not in remote:
        raise RuntimeError(f"origin 不是已登记的公开仓库：{remote}")
    if _git("diff", "--cached", "--quiet", check=False).returncode:
        raise RuntimeError("索引中存在预先暂存的改动，已跳过自动发布以免误提交")


def _retry_pending_push(state: dict[str, object]) -> bool:
    pending = str(state.get("pending_commit") or "")
    if not pending:
        return False
    head = _git("rev-parse", "HEAD").stdout.strip()
    subject = _git("show", "-s", "--format=%s", "HEAD").stdout.strip()
    if head != pending or not subject.startswith(COMMIT_PREFIX):
        raise RuntimeError("存在无法确认来源的本地提交，拒绝自动推送")
    _git("push", "origin", "main")
    _write_state(status="published", pending_commit=None, last_success_at=_now(), last_error=None)
    return True


def publish(*, push: bool = True) -> str:
    with _publish_lock() as acquired:
        if not acquired:
            _write_state(status="busy", last_attempt_at=_now(), last_error="已有发布任务正在运行")
            return "busy"
        _write_state(status="running", last_attempt_at=_now(), last_error=None)
        try:
            _assert_repository_ready()
            state = _read_state()
            if push and _retry_pending_push(state):
                _assert_repository_ready()

            if push:
                _git("fetch", "--quiet", "origin", "main")
                head = _git("rev-parse", "HEAD").stdout.strip()
                remote_head = _git("rev-parse", "origin/main").stdout.strip()
                if head != remote_head:
                    raise RuntimeError("本地 main 与 origin/main 不一致，拒绝自动发布")

            source_fingerprint = _source_fingerprint()
            if state.get("source_fingerprint") == source_fingerprint:
                _write_state(status="unchanged", last_success_at=_now(), last_error=None)
                print("内部工作库未变化，本轮无需发布。")
                return "unchanged"

            # 公开库可能较大；使用项目所在数据盘，避免系统临时盘空间不足。
            with tempfile.TemporaryDirectory(
                prefix=".public-publish-", dir=ROOT_DIR
            ) as folder:
                candidate = Path(folder) / "public_data"
                manifest = export_public_snapshot(DEFAULT_SOURCE, candidate)
                current = _load_manifest(PUBLIC_DIR)
                if current and _snapshot_signature(current) == _snapshot_signature(manifest):
                    _write_state(
                        status="unchanged", source_fingerprint=source_fingerprint,
                        last_success_at=_now(), last_error=None,
                    )
                    print("公开数据无实质变化，本轮无需发布。")
                    return "unchanged"

                previous = _replace_public_data(candidate)
                committed = False
                try:
                    _git("add", "--all", "--", "public_data")
                    staged = [
                        item for item in _git("diff", "--cached", "--name-only", "-z").stdout.split("\0")
                        if item
                    ]
                    _validate_staged_paths(staged)
                    if not staged:
                        _restore_previous(previous)
                        _write_state(
                            status="unchanged", source_fingerprint=source_fingerprint,
                            last_success_at=_now(), last_error=None,
                        )
                        return "unchanged"
                    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %z")
                    _git("commit", "-m", f"{COMMIT_PREFIX} {stamp}")
                    committed = True
                except Exception:
                    _git("restore", "--staged", "--", "public_data", check=False)
                    if not committed:
                        _restore_previous(previous)
                    raise
                finally:
                    if committed and previous.exists():
                        shutil.rmtree(previous, ignore_errors=True)

            commit = _git("rev-parse", "HEAD").stdout.strip()
            if not push:
                _write_state(
                    status="committed_not_pushed", pending_commit=commit,
                    source_fingerprint=source_fingerprint, last_success_at=_now(), last_error=None,
                )
                return "committed_not_pushed"
            try:
                _git("push", "origin", "main")
            except Exception as exc:
                _write_state(
                    status="push_failed", pending_commit=commit,
                    source_fingerprint=source_fingerprint, last_error=str(exc),
                )
                raise
            _write_state(
                status="published", pending_commit=None, last_success_at=_now(),
                last_commit=commit, source_fingerprint=source_fingerprint, last_error=None,
            )
            print(f"公开数据已发布：{commit[:12]}")
            return "published"
        except Exception as exc:
            _write_state(status="failed", last_error=str(exc))
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description="导出、校验并发布脱敏公开数据")
    parser.add_argument("--no-push", action="store_true", help="只创建本地提交，不推送")
    args = parser.parse_args()
    try:
        result = publish(push=not args.no_push)
    except Exception as exc:
        _log(f"失败：{exc}")
        print(f"自动发布失败：{exc}", file=sys.stderr)
        return 1
    _log(f"完成：{result}")
    return 75 if result == "busy" else 0


if __name__ == "__main__":
    raise SystemExit(main())
