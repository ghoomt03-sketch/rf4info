from __future__ import annotations

import requests

from .browser_client import BrowserFetchError


SOURCE_LABELS = {
    "rf4db_weather": "RF4DB 天气",
    "rf4db_earnings": "RF4DB 收益",
    "rf4db_map_assets": "RF4DB 地图资源",
    "rf4db_catches": "RF4DB 渔获",
    "rf4stat_activity": "RF4-STAT",
    "rf4stat_posts": "RF4-STAT 社区帖子",
    "rf4map_points": "RF4MAP",
    "official_weekly_cn": "RF4DB镜像·中国区普通周榜",
}


def result_state(results: dict[str, dict]) -> str:
    failures = sum(item.get("status") == "failed" for item in results.values())
    if failures and failures == len(results):
        return "failed"
    return "partial" if failures else "completed"


def failure_message(exc: Exception) -> str:
    """保留诊断信息，同时给出可由响应直接确认的失败原因。"""
    if isinstance(exc, BrowserFetchError):
        if exc.challenge:
            return f"浏览器中仍出现安全/人机验证，未取得新数据；保留已有缓存。地址：{exc.url}"
        if exc.status_code == 403:
            return f"浏览器访问返回 HTTP 403；保留已有缓存。地址：{exc.url}"
        if exc.status_code == 429:
            return "浏览器访问返回 HTTP 429：网站限制请求频率；保留已有缓存，稍后再刷新。"
        return str(exc)
    if isinstance(exc, requests.Timeout):
        return "请求超时，未取得新数据；保留已有缓存。"
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        response = exc.response
        if response.status_code == 403:
            body = response.text.casefold()
            challenge = any(term in body for term in (
                "проверка безопасности", "подтвердите, что вы человек",
                "verify you are human", "cf-chl-", "captcha",
                "安全检查", "请验证您是人类",
            ))
            reason = "网站要求安全/人机验证，程序请求被拒绝" if challenge else "网站拒绝访问，具体限制原因尚未确认"
            return f"HTTP 403：{reason}；保留已有缓存。地址：{response.url}"
        if response.status_code == 429:
            return "HTTP 429：网站限制请求频率；保留已有缓存，稍后再刷新。"
    if isinstance(exc, requests.ConnectionError):
        return f"网络连接失败；保留已有缓存。{exc}"
    return f"{type(exc).__name__}: {exc}"
