from __future__ import annotations

import atexit
import json
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

from .rf4stat_browser import (
    browser_endpoint,
    hide_background_browser_windows,
    is_challenge,
    launch_browser,
)


ALLOWED_HOSTS = {
    "download.rf4db.com",
    "oss.rf4db.com",
    "rf4map.ru",
    "cn.rf4-stat.com",
    "translate.googleapis.com",
}


class BrowserFetchError(RuntimeError):
    def __init__(self, message: str, *, url: str = "", status_code: int | None = None,
                 challenge: bool = False):
        super().__init__(message)
        self.url = url
        self.status_code = status_code
        self.challenge = challenge


@dataclass
class BrowserResponse:
    url: str
    status_code: int
    content: bytes
    headers: dict[str, str]

    @property
    def text(self) -> str:
        content_type = self.headers.get("content-type", "")
        encoding = "utf-8"
        if "charset=" in content_type:
            encoding = content_type.split("charset=", 1)[1].split(";", 1)[0].strip()
        return self.content.decode(encoding or "utf-8", errors="replace")

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise BrowserFetchError(
                f"浏览器访问返回 HTTP {self.status_code}；保留已有缓存。地址：{self.url}",
                url=self.url,
                status_code=self.status_code,
            )


def _validated_url(url: str, params=None) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS or parsed.port not in (None, 443):
        raise BrowserFetchError(f"浏览器采集地址不在 HTTPS 白名单内：{url}", url=url)
    if params:
        query = list(parse_qsl(parsed.query, keep_blank_values=True))
        if hasattr(params, "items"):
            query.extend(params.items())
        else:
            query.extend(params)
        parsed = parsed._replace(query=urlencode(query, doseq=True))
    return urlunparse(parsed)


def _ensure_chrome(entry_url: str) -> str:
    try:
        return browser_endpoint()
    except Exception:
        try:
            return launch_browser(entry_url)
        except Exception as exc:
            raise BrowserFetchError("项目专用 Chrome 未能启动或开放本机连接；保留已有缓存。", url=entry_url) from exc


class BrowserClient:
    """Fetch public HTTPS resources through a real persistent Chrome session."""

    def __init__(self, entry_url: str):
        from playwright.sync_api import sync_playwright

        endpoint = _ensure_chrome(entry_url)
        self.driver = sync_playwright().start()
        try:
            self.browser = self.driver.chromium.connect_over_cdp(endpoint, timeout=15000)
            self.context = self.browser.contexts[0]
            self.page = self.context.new_page()
            hide_background_browser_windows()
        except Exception:
            self.driver.stop()
            raise

    @classmethod
    def from_existing(cls, browser, context):
        client = cls.__new__(cls)
        client.driver = None
        client.browser = browser
        client.context = context
        client.page = context.new_page()
        return client

    def close(self) -> None:
        try:
            self.page.close()
        except Exception:
            pass
        if self.driver is not None:
            try:
                self.driver.stop()
            except Exception:
                pass
        hide_background_browser_windows()

    def get(self, url: str, *, params=None, headers=None, timeout: float = 45) -> BrowserResponse:
        del headers  # Browser owns its normal request headers and persistent session.
        target = _validated_url(url, params)
        timeout_ms = max(1000, int(timeout * 1000))
        hide_background_browser_windows()
        try:
            response = self.page.goto(target, wait_until="domcontentloaded", timeout=timeout_ms)
            hide_background_browser_windows()
            deadline = time.monotonic() + min(20.0, timeout)
            challenge_seen = False
            while True:
                title = self.page.title()
                body_text = self.page.locator("body").inner_text(timeout=5000)
                challenged = is_challenge(title, body_text + "\n" + self.page.content())
                challenge_seen = challenge_seen or challenged
                if not challenged or time.monotonic() >= deadline:
                    break
                self.page.wait_for_timeout(500)
            final_url = self.page.url
            _validated_url(final_url)
            if challenged:
                status = response.status if response is not None else None
                raise BrowserFetchError(
                    "网站在项目专用 Chrome 中仍要求安全/人机验证；程序未操作验证控件，保留已有缓存。",
                    url=final_url,
                    status_code=status,
                    challenge=True,
                )
            self.page.wait_for_timeout(500)
            status = response.status if response is not None else 200
            if challenge_seen and status >= 400:
                status = 200
            content_type = (response.headers.get("content-type", "") if response is not None else "").casefold()
            if "text/html" in content_type or "application/xhtml" in content_type or not content_type:
                content = self.page.content().encode("utf-8")
                headers_out = {"content-type": "text/html; charset=utf-8"}
            else:
                content = response.body() if response is not None else b""
                headers_out = dict(response.headers) if response is not None else {}
            result = BrowserResponse(final_url, status, content, headers_out)
            result.raise_for_status()
            return result
        except BrowserFetchError:
            raise
        except Exception as exc:
            raise BrowserFetchError(
                f"项目专用 Chrome 导航失败或超时；保留已有缓存。{exc}", url=target
            ) from exc
        finally:
            hide_background_browser_windows()


_CLIENT: BrowserClient | None = None
_BORROWED_BROWSER = None
_BORROWED_CONTEXT = None


def browser_get(url: str, *, params=None, headers=None, timeout: float = 45) -> BrowserResponse:
    global _CLIENT
    target = _validated_url(url, params)
    if _CLIENT is None:
        if _BORROWED_BROWSER is not None and _BORROWED_CONTEXT is not None:
            _CLIENT = BrowserClient.from_existing(_BORROWED_BROWSER, _BORROWED_CONTEXT)
        else:
            _CLIENT = BrowserClient(target)
    return _CLIENT.get(url, params=params, headers=headers, timeout=timeout)


def close_browser_client() -> None:
    global _CLIENT
    if _CLIENT is not None:
        _CLIENT.close()
        _CLIENT = None


atexit.register(close_browser_client)
