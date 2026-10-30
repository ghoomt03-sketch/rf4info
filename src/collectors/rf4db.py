from __future__ import annotations

import gzip
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..browser_client import browser_get

from ..config import (
    CATCH_PAGES_PER_REFRESH,
    CATCH_RECORD_PAGES_PER_REFRESH,
    MAP_IMAGE_DIR,
    MAPS,
    RAW_DIR,
    RF4DB_BASE,
    RF4DB_ZH_BASE,
    normalize_weather,
)


def _number(text: str | None) -> float | None:
    if not text:
        return None
    match = re.search(r"-?[\d,.]+", text.replace("\xa0", " "))
    return float(match.group(0).replace(",", "")) if match else None


def _get(url: str, raw_name: str) -> BeautifulSoup:
    response = browser_get(url, timeout=45)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    path = RAW_DIR / f"{stamp}-{raw_name}.html.gz"
    with gzip.open(path, "wb") as handle:
        handle.write(response.content)
    return BeautifulSoup(response.text, "lxml")


def collect_weather(request_delay: float = 0.35) -> list[dict]:
    collected = datetime.now().astimezone().replace(microsecond=0)
    hour_base = collected.replace(minute=0, second=0)
    rows: list[dict] = []
    for index, map_id in enumerate(MAPS):
        url = f"{RF4DB_BASE}/weather?map={map_id}"
        soup = _get(url, f"weather-{map_id}")
        for tile in soup.select('ol[class*="timeline"] li'):
            link = tile.find("a", href=True)
            horizon_match = re.search(r"[?&]hour=(\d+)", link.get("href", "") if link else "")
            if not horizon_match:
                continue
            horizon = int(horizon_match.group(1))
            temp_tag = tile.select_one('[class*="air"]')
            wind_tag = tile.select_one('[class*="wind"]')
            rows.append(
                {
                    "map_id": map_id,
                    "forecast_slot": (hour_base + timedelta(hours=horizon)).isoformat(),
                    "horizon": horizon,
                    "weather_code": tile.get("data-weather", "unknown"),
                    "forecast_day_temperature": _number(temp_tag.get_text(" ", strip=True) if temp_tag else None),
                    "wind_speed": _number(wind_tag.get_text(" ", strip=True) if wind_tag else None),
                    "collected_at": collected.isoformat(),
                    "source_url": url,
                }
            )
        if index + 1 < len(MAPS):
            time.sleep(request_delay)
    return rows


def collect_earnings(period: str = "24h", request_delay: float = 0.15) -> list[dict]:
    collected = datetime.now().astimezone().replace(microsecond=0).isoformat()
    url = f"{RF4DB_ZH_BASE}/leaderboard/earnings?period={period}"
    soup = _get(url, f"earnings-{period}")
    rows: list[dict] = []
    verified_rows = soup.select('div[class*="verifiedRow"]')
    for index, row in enumerate(verified_rows):
        anchors = row.find_all("a", href=True)
        map_anchor = next((a for a in anchors if "/maps/" in a.get("href", "")), None)
        detail_anchor = next((a for a in anchors if "/leaderboard/earnings/" in a.get("href", "")), None)
        if not map_anchor or not detail_anchor:
            continue
        map_id = map_anchor["href"].rstrip("/").split("/")[-1]
        rank_tag = row.select_one('[class*="rank"]')
        silver_tag = row.select_one('[class*="relative"]')
        count_tag = row.select_one('[class*="count"]')
        coverage_tag = row.select_one('[class*="coverage"]')
        composition_tag = row.select_one('[class*="fishNames"]')
        detail_url = urljoin(RF4DB_ZH_BASE, detail_anchor["href"])
        detail_soup = _get(detail_url, f"earnings-detail-{period}-{index + 1:03d}")
        map_payload = _earnings_map_payload(detail_soup, map_id)
        coordinates = list(dict.fromkeys(
            item["coordinate"] for item in map_payload["spots"] if item.get("coordinate")
        ))
        if not coordinates:
            coordinates = list(dict.fromkeys(re.findall(r"\b\d{1,4}:\d{1,4}\b", detail_soup.get_text(" ", strip=True))))
        species = _earnings_species(detail_soup)
        rows.append(
            {
                "source_key": detail_url,
                "rank_no": int(_number(rank_tag.get_text(" ", strip=True)) or 0),
                "map_id": map_id,
                "silver_per_hour": _number(silver_tag.get_text(" ", strip=True) if silver_tag else None),
                "catch_count": int(_number(count_tag.get_text(" ", strip=True) if count_tag else None) or 0),
                "coverage_pct": _number(coverage_tag.get_text(" ", strip=True) if coverage_tag else None),
                "composition": composition_tag.get_text(" ", strip=True) if composition_tag else "",
                "species_composition": json.dumps(species, ensure_ascii=False),
                "coordinates": " / ".join(coordinates),
                "spot_details": json.dumps(map_payload["spots"], ensure_ascii=False),
                "map_calibration": json.dumps(map_payload["calibration"], ensure_ascii=False),
                "map_image_path": map_payload["image_path"],
                "period": period,
                "collected_at": collected,
                "source_url": detail_url,
            }
        )
        if index + 1 < len(verified_rows):
            time.sleep(request_delay)
    return rows


def collect_map_assets(request_delay: float = 0.25) -> list[dict]:
    """Collect one reusable basemap and coordinate calibration per public map."""
    collected = datetime.now().astimezone().replace(microsecond=0).isoformat()
    map_ids = [map_id for map_id in MAPS if map_id != "level_000_home"]
    rows: list[dict] = []
    for index, map_id in enumerate(map_ids):
        url = f"{RF4DB_ZH_BASE}/maps/{map_id}"
        soup = _get(url, f"map-asset-{map_id}")
        html = str(soup)
        calibration = _extract_map_calibration(html)
        image_match = re.search(r'\\?"imageUrl\\?":\\?"([^"\\]+)', html)
        image_url = urljoin("https://oss.rf4db.com", image_match.group(1)) if image_match else ""
        if calibration.get("mapSystemId") != map_id:
            raise ValueError(f"RF4DB地图图鉴缺少{map_id}的有效坐标校准；保留已有地图缓存。")
        if not image_url:
            raise ValueError(f"RF4DB地图图鉴缺少{map_id}的公开底图地址；保留已有地图缓存。")
        image_path = _cache_map_image(map_id, image_url)
        if not image_path:
            raise ValueError(f"RF4DB地图图鉴未能缓存{map_id}底图；保留已有地图缓存。")
        rows.append({
            "map_id": map_id,
            "map_calibration": json.dumps(calibration, ensure_ascii=False),
            "map_image_path": image_path,
            "image_url": image_url,
            "collected_at": collected,
            "source_url": url,
        })
        if index + 1 < len(map_ids):
            time.sleep(request_delay)
    if len(rows) != len(map_ids):
        raise ValueError("RF4DB地图图鉴没有返回全部可推荐地图资源；保留已有地图缓存。")
    return rows


def _cache_map_image(map_id: str, image_url: str | None) -> str:
    if not image_url:
        return ""
    MAP_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    path = MAP_IMAGE_DIR / f"{map_id}.webp"
    if not path.exists() or path.stat().st_size < 1024:
        response = browser_get(image_url, timeout=45)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(response.content)
        temporary.replace(path)
    return str(path)


def _extract_map_calibration(html: str) -> dict:
    calibration_match = re.search(
        r'\{\\?"mapSystemId\\?":\\?"[^\"]+\\?",\\?"minX\\?":.*?\\?"confidence\\?":\\?"[^\"]+\\?"\}',
        html,
    )
    if not calibration_match:
        return {}
    try:
        return json.loads(calibration_match.group(0).replace('\\"', '"'))
    except json.JSONDecodeError:
        return {}


def _earnings_map_payload(soup: BeautifulSoup, map_id: str) -> dict:
    """读取收益详情内嵌的地图校准、点位方向和卡线数据。"""
    html = str(soup)
    spots: list[dict] = []
    for encoded in re.findall(r'\{\\"catches\\":.*?\\"bearingDegrees\\":(?:null|-?[\d.]+)\}', html):
        try:
            item = json.loads(encoded.replace('\\"', '"'))
        except json.JSONDecodeError:
            continue
        if re.fullmatch(r"\d{1,4}:\d{1,4}", str(item.get("coordinate") or "")):
            spots.append(item)
    calibration = _extract_map_calibration(html)
    image_tag = soup.select_one('img.game-map__fallback[src]')
    image_url = image_tag.get("src") if image_tag else None
    if image_url:
        image_url = urljoin("https://oss.rf4db.com", image_url)
    return {
        "spots": spots,
        "calibration": calibration,
        "image_path": _cache_map_image(map_id, image_url),
    }


def _earnings_species(soup: BeautifulSoup) -> list[dict]:
    """读取个人收益详情中去重后的鱼种尾数与价值占比。"""
    card = soup.select_one('div[class*="compositionCard"]')
    if card is None:
        return []
    rows: list[dict] = []
    seen: set[str] = set()
    for item in card.select("ul.earnings-species > li, details.earnings-rest li"):
        anchor = item.select_one('a[href*="/fishes/"]')
        if anchor is None:
            continue
        fish_id = anchor.get("href", "").rstrip("/").split("/")[-1]
        if not fish_id or fish_id in seen:
            continue
        count_tag = item.select_one("span.earnings-species__photo i")
        if count_tag is None:
            count_tag = item.find("i", recursive=False)
        percent_tag = item.find("strong")
        rows.append(
            {
                "fish_id": fish_id,
                "fish_name_en": anchor.get_text(" ", strip=True),
                "count": int(_number(count_tag.get_text(" ", strip=True) if count_tag else None) or 0),
                "value_pct": _number(percent_tag.get_text(" ", strip=True) if percent_tag else None),
            }
        )
        seen.add(fish_id)
    return rows


def _localized_baits(card) -> list[str]:
    baits: list[str] = []
    for bait_anchor in card.select('a[href*="/wiki/baits/"], a[href*="/wiki/lures/"]'):
        bait_name = bait_anchor.get_text(" ", strip=True) or bait_anchor.get("title")
        bait_href = bait_anchor.get("href", "")
        if bait_name and not re.search(r"[\u4e00-\u9fff]", bait_name):
            if re.search(r"[\u0400-\u04ff]", bait_name) or not re.search(r"[A-Za-z]", bait_name):
                stable_id = bait_href.rstrip("/").split("/")[-1] or "unknown"
                category = "拟饵" if "/lures/" in bait_href else "鱼饵"
                bait_name = f"未收录中文{category}（{stable_id}）"
        if bait_name and bait_name not in baits:
            baits.append(bait_name)
    return baits


def collect_catches(
    pages: int = CATCH_PAGES_PER_REFRESH,
    record_pages: int = CATCH_RECORD_PAGES_PER_REFRESH,
    record_start_page: int = 1,
    star_start_page: int = 1,
    request_delay: float = 0.2,
    stop_record_ids: set[str] | None = None,
    stop_star_ids: set[str] | None = None,
) -> list[dict]:
    collected = datetime.now().astimezone().replace(microsecond=0).isoformat()
    rows: list[dict] = []

    # 普通公开渔获流：数量大，但列表通常没有坐标、天气和即时温度。
    record_end_page = record_start_page + max(0, record_pages)
    for page in range(max(1, record_start_page), record_end_page):
        url = f"{RF4DB_ZH_BASE}/catch-records?page={page}"
        soup = _get(url, f"catch-records-zh-page-{page:03d}")
        cards = soup.select("article.catch-card")
        if not cards:
            break
        page_source_ids: set[str] = set()
        for card in cards:
            fish_anchor = card.select_one('h2.fish-name a[href*="/fishes/"]')
            map_anchor = card.select_one('a[href*="/maps/"]')
            detail_anchor = card.select_one('a[href*="/catches/"]')
            if not fish_anchor or not map_anchor or not detail_anchor:
                continue
            fish_id = fish_anchor["href"].rstrip("/").split("/")[-1]
            map_id = map_anchor["href"].rstrip("/").split("/")[-1]
            source_id = detail_anchor["href"].rstrip("/").split("/")[-1]
            page_source_ids.add(source_id)
            caught_tag = card.select_one('div[class*="time"]')
            baits = _localized_baits(card)
            grade = card.select_one("[data-fish-grade]")
            rows.append(
                {
                    "source_id": source_id,
                    "fish_id": fish_id,
                    "fish_name_en": fish_anchor.get("title") or fish_anchor.get_text(" ", strip=True),
                    "map_id": map_id,
                    "coordinate": None,
                    "clip": None,
                    "game_time": None,
                    "depth": None,
                    "weather_code": "unknown",
                    "catch_temperature": None,
                    "rig": "待补充：普通渔获列表未公开钓组",
                    "baits": " / ".join(baits) or "待补充：普通渔获列表未公开饵料",
                    "fish_grade": grade.get("data-fish-grade") if grade else "normal",
                    "caught_at": caught_tag.get_text(" ", strip=True) if caught_tag else collected,
                    "collected_at": collected,
                    "source_url": urljoin(RF4DB_ZH_BASE, detail_anchor["href"]),
                }
            )
        if stop_record_ids and page_source_ids.intersection(stop_record_ids):
            break
        if page + 1 < record_end_page:
            time.sleep(request_delay)

    # 星鱼公开流：量较少，但提供坐标、游戏时间、天气、温度、钓组和饵料。
    star_end_page = star_start_page + max(0, pages)
    previous_star_page: tuple[str, ...] | None = None
    for page in range(max(1, star_start_page), star_end_page):
        url = f"{RF4DB_ZH_BASE}/catches?page={page}"
        soup = _get(url, f"catches-zh-page-{page:03d}")
        cards = soup.select("article.catch-card")
        if not cards:
            break
        page_signature = tuple(
            anchor["href"].rstrip("/").split("/")[-1]
            for card in cards
            if (anchor := card.select_one('a.catch-card__time[href*="/catches/"]')) is not None
        )
        if page_signature and page_signature == previous_star_page:
            break
        previous_star_page = page_signature
        for card in cards:
            fish_anchor = card.select_one('h2.fish-name a[href*="/fishes/"]')
            map_anchor = card.select_one('.catch-card__place a[href*="/maps/"]')
            detail_anchor = card.select_one('a.catch-card__time[href*="/catches/"]')
            if not fish_anchor or not map_anchor or not detail_anchor:
                continue
            fish_id = fish_anchor["href"].rstrip("/").split("/")[-1]
            map_id = map_anchor["href"].rstrip("/").split("/")[-1]
            source_id = detail_anchor["href"].rstrip("/").split("/")[-1]
            metrics = {}
            for metric in card.select(".catch-card__metric[data-kind]"):
                value = metric.find("b")
                metrics[metric.get("data-kind")] = value.get_text(" ", strip=True) if value else None
            grade = card.select_one("[data-fish-grade]")
            time_tag = detail_anchor.find("time", datetime=True)
            baits = _localized_baits(card)
            rig_tag = card.select_one(".catch-card__place strong")
            rows.append(
                {
                    "source_id": source_id,
                    "fish_id": fish_id,
                    "fish_name_en": fish_anchor.get("title") or fish_anchor.get_text(" ", strip=True),
                    "map_id": map_id,
                    "coordinate": metrics.get("coordinate"),
                    "clip": metrics.get("clip"),
                    "game_time": metrics.get("gameTime"),
                    "depth": metrics.get("depth"),
                    "weather_code": normalize_weather(metrics.get("weather")),
                    "catch_temperature": _number(metrics.get("temperature")),
                    "rig": _localized_text(rig_tag.get_text(" ", strip=True) if rig_tag else None, "钓组"),
                    "baits": " / ".join(filter(None, baits)) or "未公开饵料（来源未提供）",
                    "fish_grade": grade.get("data-fish-grade") if grade else "normal",
                    "caught_at": time_tag.get("datetime") if time_tag else collected,
                    "collected_at": collected,
                    "source_url": urljoin(RF4DB_ZH_BASE, detail_anchor["href"]),
                }
            )
        if stop_star_ids and set(page_signature).intersection(stop_star_ids):
            break
        if page + 1 < star_end_page:
            time.sleep(request_delay)
    if record_pages > 0 and not any(row.get("fish_grade") == "normal" for row in rows):
        raise RuntimeError("RF4DB 普通渔获页面未解析到有效记录；保留旧缓存")
    if pages > 0 and not any(row.get("fish_grade") != "normal" for row in rows):
        raise RuntimeError("RF4DB 星鱼页面未解析到有效记录；保留旧缓存")
    return rows


def _localized_text(value: str | None, category: str) -> str:
    text = str(value or "").strip()
    if text and re.search(r"[\u4e00-\u9fff]", text):
        return text
    return f"未收录中文{category}"

