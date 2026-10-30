from __future__ import annotations

import gzip
import hashlib
import json
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from ..browser_client import browser_get

from ..config import MAPS, RAW_DIR, RF4STAT_BASE
from ..normalization import (
    canonical_bait,
    canonical_fish,
    load_bait_aliases,
    normalize_coordinate,
    normalize_spot_label,
    normalize_map_id,
    refresh_rf4db_fish_aliases,
    refresh_rf4db_fish_catalog,
    rf4db_fish_catalog,
    refresh_rf4db_bait_catalog,
    refresh_rf4db_bait_aliases,
    rf4db_bait_catalog,
    save_bait_aliases,
    save_rf4stat_fish_aliases,
    split_bait_terms,
)
from ..normalization import RU_MAP_ALIASES


RF4MAP_POINTS_URL = "https://rf4map.ru/points"

def _get(url: str, raw_name: str) -> BeautifulSoup:
    response = browser_get(url, timeout=45)
    response.encoding = "utf-8"
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    with gzip.open(RAW_DIR / f"{stamp}-{raw_name}.html.gz", "wb") as handle:
        handle.write(response.content)
    return BeautifulSoup(response.text, "lxml")


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value and value.strip()))


def _clip(text: str) -> str | None:
    match = re.search(r"(?:клипс\w*\s*)?(\d+(?:[.,]\d+)?)\s*м\b", text, re.I)
    return f"{match.group(1).replace(',', '.')}m" if match else None


def collect_rf4map(max_pages: int = 20, request_delay: float = 0.4) -> list[dict]:
    collected = datetime.now().astimezone().replace(microsecond=0)
    parsed: list[dict] = []
    seen_point_ids: set[str] = set()
    for page in range(1, max_pages + 1):
        url = RF4MAP_POINTS_URL if page == 1 else f"{RF4MAP_POINTS_URL}?page={page}"
        soup = _get(url, f"rf4map-points-{page:02d}")
        articles = soup.find_all("article")
        page_point_ids: set[str] = set()
        for article in articles:
            link = article.find("a", href=re.compile(r"^/points/\d+$"))
            if not link:
                continue
            source_record_id = link["href"].rstrip("/").split("/")[-1]
            if source_record_id in seen_point_ids:
                continue
            page_point_ids.add(source_record_id)
            label = link.get("aria-label", "")
            coordinate = normalize_coordinate(label)
            position_raw = label.split("·", 1)[0].strip()
            spot_label = normalize_spot_label(position_raw)
            map_raw = label.split("·", 1)[1].strip() if "·" in label else ""
            map_id = normalize_map_id(map_raw)
            fish_raw = _dedupe([
                image.get("alt", "") for image in article.select('img[src*="/fish/"]')
            ])
            bait_raw = split_bait_terms(_dedupe([
                image.get("alt", "") for image in article.select('img[src*="/bait/"]')
            ]))
            if not fish_raw:
                continue
            text = article.get_text(" ", strip=True)
            catch_match = re.search(r"(\d+)\s*улов", text, re.I)
            parsed.append({
                "source_record_id": source_record_id,
                "source_url": urljoin(RF4MAP_POINTS_URL, link["href"]),
                "map_raw": map_raw,
                "map_id": map_id,
                "coordinate": coordinate,
                "spot_label": spot_label,
                "position_raw": position_raw,
                "fish_raw": fish_raw,
                "bait_raw": bait_raw,
                "clip": _clip(text),
                "catch_count": int(catch_match.group(1)) if catch_match else None,
            })
        seen_point_ids.update(page_point_ids)
        next_href = f"/points?page={page + 1}"
        if not page_point_ids or soup.find("a", href=next_href) is None:
            break
        time.sleep(request_delay)
    if not parsed:
        raise RuntimeError("RF4MAP 未解析到公开点位，保留上次成功缓存")
    fish_aliases = refresh_rf4db_fish_aliases()
    mapped_ids = []
    for item in parsed:
        for fish_raw in item["fish_raw"]:
            source_fish_id = f"rf4map:{hashlib.sha1(fish_raw.encode('utf-8')).hexdigest()[:12]}"
            mapped_ids.append(canonical_fish(fish_raw, source_fish_id, catalog={}, aliases=fish_aliases)[0])
    fish_catalog = refresh_rf4db_fish_catalog(mapped_ids)
    bait_aliases = refresh_rf4db_bait_aliases()
    mapped_bait_ids = [
        canonical_bait(raw, f"rf4map-bait:{raw}", catalog={}, aliases=bait_aliases)[0]
        for item in parsed for raw in item["bait_raw"]
    ]
    bait_catalog = refresh_rf4db_bait_catalog([
        stable_id for stable_id in mapped_bait_ids
        if stable_id and not stable_id.startswith(("rf4map-bait:", "model:", "fish:"))
    ])
    rows: list[dict] = []
    for item in parsed:
        validation = []
        if not item["map_id"]:
            validation.append("地图未映射")
        if not item["coordinate"] and not item["spot_label"]:
            validation.append("坐标无效")
        bait_results = [
            canonical_bait(name, f"rf4map-bait:{name}", catalog=bait_catalog, aliases=bait_aliases)
            for name in item["bait_raw"]
        ]
        baits = [result[1] for result in bait_results]
        if any(not result[2] for result in bait_results):
            validation.append("饵料未映射RF4DB")
        for fish_index, fish_raw in enumerate(item["fish_raw"]):
            source_fish_id = f"rf4map:{hashlib.sha1(fish_raw.encode('utf-8')).hexdigest()[:12]}"
            fish_id, fish_name, fish_mapped = canonical_fish(
                fish_raw, source_fish_id, catalog=fish_catalog, aliases=fish_aliases
            )
            row_validation = list(validation)
            if not fish_mapped:
                row_validation.append("鱼种未映射RF4DB")
            rows.append({
                "source_key": f"rf4map:{item['source_record_id']}:{fish_index}",
                "source_name": "RF4MAP",
                "source_record_id": item["source_record_id"],
                "evidence_type": "community_point",
                "map_id": item["map_id"],
                "map_name_raw": item["map_raw"],
                "coordinate": item["coordinate"],
                "spot_label": item["spot_label"],
                "fish_id": fish_id,
                "fish_name": fish_name,
                "fish_name_raw": fish_raw,
                "baits": " / ".join(baits),
                "rig": None,
                "clip": item["clip"],
                "depth": None,
                "quality_label": "社区近期点位",
                "catch_count": item["catch_count"],
                "reported_at": None,
                "collected_at": collected.isoformat(),
                "validation_status": "；".join(row_validation) if row_validation else "有效",
                "raw_payload": json.dumps(item, ensure_ascii=False),
                "source_url": item["source_url"],
            })
    return rows


def _rf4stat_time(time_text: str, date_text: str, now: datetime) -> str | None:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", time_text.strip())
    date_match = re.fullmatch(r"(\d{1,2})\.(\d{1,2})", date_text.strip())
    if not match or not date_match:
        return None
    candidate = now.replace(
        month=int(date_match.group(2)), day=int(date_match.group(1)),
        hour=int(match.group(1)), minute=int(match.group(2)), second=0, microsecond=0,
    )
    if candidate > now + timedelta(days=1):
        candidate = candidate.replace(year=candidate.year - 1)
    return candidate.isoformat()


def collect_rf4stat(request_delay: float = 0.25) -> list[dict]:
    return _collect_rf4stat(_get, request_delay)


def _collect_rf4stat(get_page, request_delay: float) -> list[dict]:
    collected = datetime.now().astimezone().replace(microsecond=0)
    parsed: list[dict] = []
    locations = list(dict.fromkeys(RU_MAP_ALIASES.keys()))
    for location_index, location in enumerate(locations):
        url = f"{RF4STAT_BASE}/fishing/?{urlencode({'location': location})}"
        soup = get_page(url, f"rf4stat-{location_index + 1:02d}")
        for row in soup.select("table.stat tr.load-row"):
            _parse_rf4stat_row(row, collected, parsed)
        if location_index + 1 < len(locations):
            time.sleep(request_delay)
    if not parsed:
        raise RuntimeError("RF4-STAT 未解析到公开渔获；若站点限制访问则保留上次成功缓存")
    return normalize_rf4stat_rows(parsed, collected, refresh_missing=True)


def normalize_rf4stat_rows(
    parsed: list[dict],
    collected: datetime,
    *,
    refresh_missing: bool,
    payload_metadata: dict | None = None,
) -> list[dict]:
    """将RF4-STAT公开列表解析结果规范化。"""
    unique_parsed = {item["source_record_id"]: item for item in parsed}
    parsed = list(unique_parsed.values())

    def bait_pairs(item: dict) -> list[tuple[str, str]]:
        values = item.get("baits_raw")
        if isinstance(values, list):
            pairs = [
                (str(value.get("name") or "").strip(), str(value.get("id") or "").strip())
                for value in values if isinstance(value, dict)
            ]
            pairs = [(name, stable_id) for name, stable_id in pairs if name]
            if pairs:
                return pairs
        name = str(item.get("bait_raw") or "").strip()
        return [(name, str(item.get("bait_id") or "").strip())] if name else []

    save_rf4stat_fish_aliases([(item["fish_raw"], item["fish_id"]) for item in parsed])
    save_bait_aliases([
        pair for item in parsed for pair in bait_pairs(item) if pair[0] and pair[1]
    ])
    fish_ids = list(dict.fromkeys(item["fish_id"] for item in parsed))
    fish_catalog = refresh_rf4db_fish_catalog(fish_ids) if refresh_missing else rf4db_fish_catalog()
    bait_aliases = load_bait_aliases()
    bait_ids = list(dict.fromkeys(
        stable_id for item in parsed for _, stable_id in bait_pairs(item) if stable_id
    ))
    bait_catalog = refresh_rf4db_bait_catalog(bait_ids) if refresh_missing else rf4db_bait_catalog()
    rows: list[dict] = []
    for item in parsed:
        validation = []
        if not item["map_id"]:
            validation.append("地图未映射")
        if not item["reported_at"]:
            validation.append("时间无效")
        fish_id, fish_name, fish_mapped = canonical_fish(
            item["fish_raw"], item["fish_id"], catalog=fish_catalog
        )
        if not fish_mapped:
            validation.append("鱼种未映射RF4DB")
        bait_names = []
        for raw_name, stable_id in bait_pairs(item):
            _, bait_name, bait_mapped = canonical_bait(
                raw_name, stable_id, catalog=bait_catalog, aliases=bait_aliases
            )
            if bait_name and bait_name not in bait_names:
                bait_names.append(bait_name)
            if not bait_mapped:
                validation.append("饵料未映射RF4DB")
        raw_payload = dict(item)
        if payload_metadata:
            raw_payload.update(payload_metadata)
        rows.append({
            "source_key": f"rf4stat:{item['source_record_id']}",
            "source_name": "RF4-STAT",
            "source_record_id": item["source_record_id"],
            "evidence_type": "public_catch_activity",
            "map_id": item["map_id"],
            "map_name_raw": item["map_raw"],
            "coordinate": item["coordinate"],
            "spot_label": item.get("spot_label"),
            "fish_id": fish_id,
            "fish_name": fish_name,
            "fish_name_raw": item["fish_raw"],
            "baits": " / ".join(bait_names) or None,
            "rig": None,
            "clip": item["clip"],
            "depth": None,
            "quality_label": item["quality_label"],
            "catch_count": 1,
            "reported_at": item["reported_at"],
            "collected_at": collected.isoformat(),
            "validation_status": "；".join(validation) if validation else "有效",
            "raw_payload": json.dumps(raw_payload, ensure_ascii=False),
            "source_url": item["source_url"],
        })
    return rows


def _parse_rf4stat_row(row, collected: datetime, parsed: list[dict]) -> None:
    share = row.select_one("a.share[href*='fishing/']:not([href$='.png'])")
    fish_link = row.select_one("td.fish a[data-filter='fish']")
    map_link = row.select_one("td.mobile-location a[data-filter='location']")
    if not share or not fish_link or not map_link:
        return
    source_record_id = share.get_text(" ", strip=True).lstrip("#")
    fish_href = (row.select_one("td.fish-icon a[href]") or {}).get("href", "")
    fish_id = fish_href.rstrip("/").split("/")[-1] if fish_href else f"rf4stat:{source_record_id}"
    fish_raw = fish_link.get_text(" ", strip=True)
    baits_raw = []
    for bait_link in row.select("a[data-filter='bait']"):
        bait_raw = bait_link.get_text(" ", strip=True)
        bait_id = ""
        icon_cell = bait_link.find_parent("td").find_previous_sibling("td")
        bait_icon = icon_cell.find("img", src=True) if icon_cell else None
        if bait_icon:
            bait_id = bait_icon.get("src", "").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if bait_raw:
            baits_raw.append({"name": bait_raw, "id": bait_id})
    bait_raw = baits_raw[0]["name"] if baits_raw else ""
    bait_id = baits_raw[0]["id"] if baits_raw else ""
    map_raw = map_link.get_text(" ", strip=True)
    position = row.select_one("td.list-position")
    position_text = position.get_text(" ", strip=True) if position else ""
    time_cell = row.select_one("td.time")
    time_div = time_cell.find("div") if time_cell else None
    date_small = time_cell.find("small") if time_cell else None
    classes = set(row.get("class", []))
    parsed.append({
        "source_record_id": source_record_id,
        "source_url": urljoin(f"{RF4STAT_BASE}/", share.get("href", "")),
        "map_raw": map_raw,
        "map_id": normalize_map_id(map_raw),
        "coordinate": normalize_coordinate(position_text),
        "spot_label": normalize_spot_label(position_text),
        "position_raw": position_text,
        "fish_id": fish_id,
        "fish_raw": fish_raw,
        "bait_raw": bait_raw,
        "bait_id": bait_id,
        "baits_raw": baits_raw,
        "clip": _clip(position_text),
        "quality_label": "奖杯标记" if any("troff" in value for value in classes) else "公开渔获记录",
        "reported_at": _rf4stat_time(
            time_div.get_text(" ", strip=True) if time_div else "",
            date_small.get_text(" ", strip=True) if date_small else "",
            collected,
        ),
    })
