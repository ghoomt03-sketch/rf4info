from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import re
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import requests
from bs4 import BeautifulSoup

from .config import DATA_DIR, RF4STAT_BASE


PROFILE_DIR = DATA_DIR / "rf4stat-chrome"
BACKGROUND_PROFILE_DIR = DATA_DIR / "rf4-background-chrome"
START_URL = f"{RF4STAT_BASE}/fishing/"
POSTS_URL = f"{RF4STAT_BASE}/posts/"
HOSTS = {urlparse(RF4STAT_BASE).hostname}
MANUAL_MESSAGE = "RF4-STAT 在项目专用Chrome中要求安全/人机验证；程序未操作验证控件，已保留已有缓存。"
POSTS_MANUAL_MESSAGE = "RF4-STAT 帖子分页要求安全/人机验证；程序已停止本来源并保留已有数据和续采位置。"


class BrowserCollectionError(RuntimeError):
    pass


def is_source_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in HOSTS and parsed.port in (None, 443)


def posts_pagination_filters(url: str) -> list[tuple[str, str]] | None:
    """Derive public post filters without treating a route as a data allowlist."""
    if not is_source_url(url):
        return None
    parsed = urlparse(url)
    path = parsed.path.rstrip('/')
    community = re.fullmatch(r'/community/(\d+)', path)
    if community:
        return [('community', community.group(1))]
    if path != '/posts':
        return None
    result: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        folded = key.casefold()
        if folded in {'ajax', 'page'} or any(term in folded for term in (
            'token', 'auth', 'session', 'cookie', 'password', 'signature', 'csrf',
        )):
            continue
        if len(key) <= 80 and len(value) <= 500:
            result.append((key, value))
    return result[:40]


def is_challenge(title: str, text: str = "") -> bool:
    title = title.casefold()
    text = text.casefold()
    return any(term in title for term in (
        "проверка безопасности", "安全检查", "安全验证", "security check", "just a moment",
    )) or any(term in text for term in (
        "подтвердите, что вы человек", "verify you are human", "请验证您是人类",
        "确认您是人类", "正在进行安全验证", "подтвердите, что вы не робот",
        "cf-chl-", "challenge-platform", "challenges.cloudflare.com/turnstile",
    ))


def _local_json(endpoint: str, path: str):
    # Never route the local debugging connection through environment proxies.
    with requests.Session() as session:
        session.trust_env = False
        response = session.get(endpoint + path, timeout=1)
        response.raise_for_status()
        return response.json()


def _background_mode(background: bool | None = None) -> bool:
    return os.environ.get("RF4_BACKGROUND") == "1" if background is None else background


def _profile_dir(background: bool | None = None) -> Path:
    return BACKGROUND_PROFILE_DIR if _background_mode(background) else PROFILE_DIR


def browser_endpoint(background: bool | None = None) -> str:
    profile_dir = _profile_dir(background)
    try:
        port = int((profile_dir / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()[0])
        if not 1024 <= port <= 65535:
            raise ValueError("port")
        endpoint = f"http://127.0.0.1:{port}"
        version = _local_json(endpoint, "/json/version")
        if not str(version.get("Browser", "")).startswith("Chrome/"):
            raise ValueError("browser")
        return endpoint
    except (OSError, ValueError, IndexError, requests.RequestException) as exc:
        raise BrowserCollectionError("项目专用Chrome尚未启动或连接已失效。") from exc


def _chrome_executable() -> str:
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "C:/Users/Default/AppData/Local")) / "Google/Chrome/Application/chrome.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("google-chrome") or shutil.which("chrome")
    if found:
        return found
    raise BrowserCollectionError("未找到 Google Chrome，请先安装 Chrome。")


def _hide_process_windows(pid: int) -> None:
    """Hide only top-level windows owned by the dedicated scheduled Chrome process."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def hide_if_owned(hwnd, _):
        owner_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        if owner_pid.value == pid and user32.IsWindowVisible(hwnd):
            user32.ShowWindow(hwnd, 0)
        return True

    user32.EnumWindows(callback_type(hide_if_owned), 0)


def hide_background_browser_windows() -> None:
    if not _background_mode():
        return
    try:
        pid = int((_profile_dir(True) / "BrowserProcessId").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    _hide_process_windows(pid)


def launch_browser(entry_url: str, background: bool | None = None) -> str:
    """Launch a visible manual Chrome or an isolated headless scheduled Chrome."""
    is_background = _background_mode(background)
    profile_dir = _profile_dir(is_background)
    profile_dir.mkdir(parents=True, exist_ok=True)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    port_file = profile_dir / "DevToolsActivePort"
    try:
        port_file.unlink()
    except FileNotFoundError:
        pass
    command = [
        _chrome_executable(), f"--user-data-dir={profile_dir.resolve()}",
        "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={port}",
        "--no-first-run", "--no-default-browser-check",
    ]
    if is_background:
        command.extend(["--start-minimized", "--window-position=-32000,-32000", "--window-size=1280,900"])
    command.append("--new-window")
    command.append(entry_url)
    popen_options = {
        "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "creationflags": subprocess.CREATE_NO_WINDOW if os.name == "nt" and is_background else 0,
    }
    if os.name == "nt" and is_background:
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        popen_options["startupinfo"] = startup
    process = subprocess.Popen(command, **popen_options)
    if is_background:
        (profile_dir / "BrowserProcessId").write_text(str(process.pid), encoding="utf-8")
    endpoint = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15
    while True:
        if is_background:
            _hide_process_windows(process.pid)
        try:
            version = _local_json(endpoint, "/json/version")
            if not str(version.get("Browser", "")).startswith("Chrome/"):
                raise ValueError("browser")
            port_file.write_text(f"{port}\nfixed-nonzero\n", encoding="utf-8")
            if is_background:
                _hide_process_windows(process.pid)
            return endpoint
        except (requests.RequestException, ValueError):
            if time.monotonic() >= deadline:
                raise BrowserCollectionError("Chrome 未能开放本机连接，请检查专用窗口是否正常启动。")
            time.sleep(0.3)


@contextmanager
def browser_lock():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handle = (DATA_DIR / "rf4stat-browser.lock").open("a+b")
    locked = False
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise BrowserCollectionError("RF4-STAT 正在另一个刷新任务中采集，请等待其结束。") from exc
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


class BrowserReader:
    """Read rendered RF4-STAT public pages in the project Chrome session."""

    def __init__(self, entry_url: str = START_URL):
        self.entry_url = entry_url

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        from . import browser_client

        try:
            endpoint = browser_endpoint()
        except BrowserCollectionError:
            launch_browser(self.entry_url)
            endpoint = browser_endpoint()
        shared = browser_client._CLIENT
        self.driver = None if shared is not None else sync_playwright().start()
        self.borrowed_context_registered = False
        try:
            self.browser = shared.browser if shared is not None else self.driver.chromium.connect_over_cdp(endpoint, timeout=10000)
            context = self.browser.contexts[0]
            if shared is None:
                browser_client._BORROWED_BROWSER = self.browser
                browser_client._BORROWED_CONTEXT = context
                self.borrowed_context_registered = True
            source_pages = [p for p in context.pages if is_source_url(p.url)]
            candidates = [p for p in source_pages
                          if urlparse(p.url).path.rstrip("/") == urlparse(self.entry_url).path.rstrip("/")]
            if self.entry_url == POSTS_URL:
                self.page = next((p for p in source_pages if not is_challenge(p.title())
                                  and p.locator('.post-item[data-post-id]').count()), None)
            else:
                self.page = None
            self.page = self.page or next(iter(candidates), None)
            if self.page is None:
                self.page = context.new_page()
            else:
                deadline = time.monotonic() + 20
                while is_challenge(
                    self.page.title(),
                    self.page.locator("body").inner_text(timeout=5000) + "\n" + self.page.content(),
                ):
                    if time.monotonic() >= deadline:
                        raise BrowserCollectionError(POSTS_MANUAL_MESSAGE if self.entry_url == POSTS_URL else MANUAL_MESSAGE)
                    self.page.wait_for_timeout(500)
            self.can_paginate_posts = posts_pagination_filters(self.page.url) is not None
            hide_background_browser_windows()
            return self
        except Exception:
            if self.borrowed_context_registered:
                browser_client._BORROWED_BROWSER = None
                browser_client._BORROWED_CONTEXT = None
            if self.driver is not None:
                self.driver.stop()
            raise

    def __exit__(self, *_):
        # Disconnect only; the real Chrome process and its session stay open.
        if getattr(self, "borrowed_context_registered", False):
            from . import browser_client

            browser_client.close_browser_client()
            browser_client._BORROWED_BROWSER = None
            browser_client._BORROWED_CONTEXT = None
        if self.driver is not None:
            self.driver.stop()

    def get(self, url: str, raw_name: str) -> BeautifulSoup:
        from playwright.sync_api import Error, TimeoutError as PlaywrightTimeout
        from .config import RAW_DIR
        from datetime import datetime
        import gzip

        if not is_source_url(url) or not (urlparse(url).path.startswith("/fishing/") or url == POSTS_URL):
            raise BrowserCollectionError("采集地址不属于允许的 RF4-STAT 公开渔获页。")
        try:
            response = self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
            if response is not None and response.status == 429:
                raise BrowserCollectionError("HTTP 429：RF4-STAT 限制请求频率，请稍后再刷新；保留已有缓存。")
            if response is not None and response.status == 403:
                raise BrowserCollectionError(POSTS_MANUAL_MESSAGE if url == POSTS_URL else MANUAL_MESSAGE)
            if response is not None and response.status >= 400:
                raise BrowserCollectionError(f"RF4-STAT 返回 HTTP {response.status}；保留已有缓存。")
            if not is_source_url(self.page.url):
                raise BrowserCollectionError("RF4-STAT 跳转到其他站点，已停止采集；保留已有缓存。")
            # Wait for ordinary page rendering, never interact with verification controls.
            deadline = time.monotonic() + 10
            while True:
                title = self.page.title()
                text = self.page.locator("body").inner_text(timeout=5000)
                if is_challenge(title, text):
                    raise BrowserCollectionError(POSTS_MANUAL_MESSAGE if url == POSTS_URL else MANUAL_MESSAGE)
                html = self.page.content()
                soup = BeautifulSoup(html, "lxml")
                if soup.select_one("table.stat") or (url == POSTS_URL and soup.select_one(".js-community-posts-list")):
                    RAW_DIR.mkdir(parents=True, exist_ok=True)
                    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
                    with gzip.open(RAW_DIR / f"{stamp}-{raw_name}.html.gz", "wb") as handle:
                        handle.write(html.encode("utf-8"))
                    return soup
                if time.monotonic() >= deadline:
                    raise BrowserCollectionError("RF4-STAT 页面未出现公开渔获表格，请检查采集窗口后再刷新；保留已有缓存。")
                self.page.wait_for_timeout(500)
        except PlaywrightTimeout as exc:
            raise BrowserCollectionError("RF4-STAT 浏览器加载超时，请检查窗口中的页面；保留已有缓存。") from exc
        except Error as exc:
            detail = str(exc).splitlines()[0][:180]
            raise BrowserCollectionError(
                f"RF4-STAT 浏览器导航失败（{detail}）；保留已有缓存。"
            ) from exc
        finally:
            hide_background_browser_windows()

    def visible_posts(self) -> BeautifulSoup:
        if not is_source_url(self.page.url):
            raise BrowserCollectionError('当前窗口不是中文 RF4-STAT 公开页面。')
        if is_challenge(self.page.title(), self.page.locator('body').inner_text(timeout=5000)):
            raise BrowserCollectionError(POSTS_MANUAL_MESSAGE)
        soup = BeautifulSoup(self.page.content(), 'lxml')
        if not soup.select_one('.post-item[data-post-id]'):
            raise BrowserCollectionError('当前窗口尚未出现符合结构的公开帖子，请检查页面。')
        self.can_paginate_posts = posts_pagination_filters(self.page.url) is not None
        return soup

    def posts_page(self, page_number: int) -> BeautifulSoup:
        """Use precisely the site's public community pagination form in this session."""
        from playwright.sync_api import Error
        from .collectors.rf4stat_posts import parse_page_response

        filters = posts_pagination_filters(self.page.url)
        if filters is None:
            raise BrowserCollectionError("当前帖子页面可以读取已显示内容，但无法安全推导分页筛选条件，已停止翻页。")
        if is_challenge(self.page.title(), self.page.locator("body").inner_text(timeout=5000)):
            raise BrowserCollectionError(POSTS_MANUAL_MESSAGE)
        try:
            response = self.page.evaluate('''async ({pageNumber, filters}) => {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), 45000);
                try {
                    const body = new URLSearchParams(filters);
                    body.set('ajax', '1');
                    body.set('page', String(pageNumber));
                    const response = await fetch('/posts/', {
                        method: 'POST', credentials: 'same-origin', redirect: 'error',
                        headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                                  'X-Requested-With': 'XMLHttpRequest'},
                        body,
                        signal: controller.signal
                    });
                    return {status: response.status, html: await response.text()};
                } catch (error) {
                    return {error: error.name === 'AbortError' ? 'timeout' : 'network_or_redirect'};
                } finally { clearTimeout(timer); }
            }''', {'pageNumber': page_number, 'filters': filters})
            if response.get('error'):
                reason = '超时' if response['error'] == 'timeout' else '网络或重定向错误'
                raise BrowserCollectionError(f'浏览器内帖子分页请求{reason}，本批已停止；保留已保存帖子和续采位置。')
            return parse_page_response(response['status'], response['html'], page_number)
        except Error as exc:
            raise BrowserCollectionError("RF4-STAT 帖子分页连接失败或超时；已保存页和旧缓存保留，关闭 VPN 后可再刷新。") from exc
