from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

from ..browser_client import BrowserFetchError, browser_get

from ..config import DATA_DIR, MAPS, RF4DB_ZH_BASE
from ..normalization import rf4db_fish_catalog

WEEKLY_URL = RF4DB_ZH_BASE + "/ratings?category=records&period=weekly&region=CN"
SOURCE_NAME = "RF4DB镜像·中国区普通周榜"
THRESHOLD_PATH = DATA_DIR / "weekly_trophy_thresholds.json"


class WeeklyBatch(list):
    def __init__(self, rows: list[dict], pages: int):
        super().__init__(rows)
        known = sum(row['trophy_threshold_kg'] is not None for row in rows)
        self.message = f'{SOURCE_NAME}；公开分页{pages}页、去重{len(rows)}条；门槛已知{known}条、未知{len(rows)-known}条；非全服渔获量'


def _chinese_only(value: str, category: str) -> str:
    cleaned = re.sub(r"[A-Za-z]+", "", str(value or "")).strip(" -_/;；")
    return cleaned if re.search(r"[\u4e00-\u9fff]", cleaned) else f"未收录中文{category}"


def parse_weight(value: str) -> float | None:
    match = re.fullmatch(r"\s*([\d\s\u00a0\u202f.,]+)\s*(kg|g)\s*", value, re.I)
    if not match:
        return None
    raw = re.sub(r"[\s\u00a0\u202f]", "", match[1])
    if ',' in raw and '.' in raw:
        if not re.fullmatch(r'\d{1,3}(?:,\d{3})+\.\d+', raw):
            return None
        raw = raw.replace(',', '')
    else:
        raw = raw.replace(',', '.')
    if not re.fullmatch(r'\d+(?:\.\d+)?', raw):
        return None
    number = float(raw)
    return number / (1000 if match[2].lower() == "g" else 1) if number > 0 else None


def public_soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "lxml")
    # Relocate Next.js public rendered fragments; do not execute scripts.
    for source, target in re.findall(r'\$RS\("(S:[a-zA-Z0-9]+)","(P:[a-zA-Z0-9]+)"\)', html):
        fragment, placeholder = soup.find(id=source), soup.find(id=target)
        if fragment is not None and placeholder is not None:
            for child in list(fragment.contents):
                placeholder.insert_before(child.extract())
            placeholder.decompose()
            fragment.decompose()
    return soup


def parse_ratings(html: str, source_url: str) -> tuple[list[dict], str | None]:
    soup = public_soup(html)
    for field, expected in [('region', 'CN'), ('period', 'weekly'), ('category', 'records')]:
        element = soup.select_one(f'input[name="{field}"]')
        if element is None or element.get('value') != expected:
            raise RuntimeError('周榜地区/期间/分类未确认；保留缓存')
    catalog = rf4db_fish_catalog()
    rows = []
    for group in soup.select('.rating-fish-group'):
        fish = group.select_one('h2')
        stable = group.select_one('header small')
        link = group.select_one('h2 a[href*="/fishes/"]')
        fish_id = link['href'].rstrip('/').split('/')[-1] if link else stable.get_text(strip=True) if stable else ''
        if fish is None or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', fish_id) or '..' in fish_id:
            raise RuntimeError('周榜鱼种结构变化；保留缓存')
        records = group.select('.rating-record:not(.rating-record--head)')
        declared = group.select_one('header p')
        count = re.search(r'\d+', declared.get_text()) if declared else None
        if count and int(count[0]) != len(records):
            raise RuntimeError('周榜鱼种条目不完整；保留缓存')
        for record in records:
            weight = record.select_one('.rating-record__weight')
            location = record.select_one('a[href*="/maps/"]')
            player = record.select_one('.rating-record__player')
            date = record.select_one('time[datetime]')
            kg = parse_weight(weight.get_text(' ', strip=True)) if weight else None
            if kg is None or location is None or player is None or date is None:
                raise RuntimeError('周榜存在不完整重量/地图/玩家/日期，拒绝覆盖缓存')
            record_date = datetime.strptime(date['datetime'], '%Y-%m-%d').date().isoformat()
            map_id = location['href'].rstrip('/').split('/')[-1]
            baits = record.select('.rating-bait')
            rows.append(dict(fish_id=fish_id, fish_name=catalog.get(fish_id, fish.get_text(' ', strip=True)),
                             map_id=map_id, map_name=MAPS.get(map_id, (location.get_text(' ', strip=True),))[0],
                             weight_kg=kg, player_name=player.get_text(' ', strip=True), record_date=record_date,
                             bait_name=' / '.join(x.get_text(' ', strip=True) for x in baits) or '未公开饵料',
                             bait_ids=json.dumps([x['href'].rstrip('/').split('/')[-1] for x in baits]),
                             source_url=source_url))
    if not rows:
        raise RuntimeError('周榜无有效记录；保留缓存')
    next_link = next((a for a in soup.select('a[href]') if '下一页' in a.get_text()), None)
    return rows, urljoin(source_url, next_link['href']) if next_link else None


def _get(url: str) -> str:
    response = browser_get(url, timeout=45)
    return response.text


def trophy_thresholds(fish_ids: set[str]) -> dict:
    try:
        cache = json.loads(THRESHOLD_PATH.read_text(encoding='utf-8'))
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError):
        cache = {}
    now = datetime.now().astimezone()
    for index, fish_id in enumerate(sorted(fish_ids)[:300]):
        old = cache.get(fish_id, {})
        try:
            if now - datetime.fromisoformat(old.get('checked_at', '')) < timedelta(days=7):
                continue
        except (ValueError, TypeError):
            pass
        url = RF4DB_ZH_BASE + '/fishes/' + fish_id
        try:
            soup = public_soup(_get(url))
            label = next((x for x in soup.select('dt') if x.get_text(strip=True) == '奖杯线'), None)
            value = label.find_next_sibling('dd') if label else None
            kg = parse_weight(value.get_text(' ', strip=True)) if value else None
        except BrowserFetchError as exc:
            if exc.challenge or exc.status_code in (401, 403, 429):
                break
            kg = None
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in (401, 403, 429):
                break
            kg = None
        except requests.RequestException:
            kg = None
        cache[fish_id] = dict(kg=kg, source_url=url, checked_at=now.isoformat())
        THRESHOLD_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = THRESHOLD_PATH.with_suffix('.tmp')
        temporary.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(THRESHOLD_PATH)
        if index % 25 == 0:
            print(f'周榜奖杯门槛：已检查 {index+1}/{len(fish_ids)} 种', flush=True)
        time.sleep(1)
    # Never infer using a stale threshold after refresh failure or access denial.
    return {k: v for k, v in cache.items() if now - datetime.fromisoformat(v['checked_at']) < timedelta(days=7)}


def collect_official_weekly() -> list[dict]:
    url, rows, visited, signatures = WEEKLY_URL, [], set(), set()
    for _ in range(20):
        if url in visited:
            raise RuntimeError('周榜分页重复；保留缓存')
        visited.add(url)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if parsed.scheme != 'https' or parsed.netloc != 'download.rf4db.com' or parsed.path != '/zh/ratings' or query.get('region', ['CN']) != ['CN'] or query.get('period', ['weekly']) != ['weekly'] or query.get('category', ['records']) != ['records']:
            raise RuntimeError('周榜分页地址超出采集范围；保留缓存')
        batch, next_url = parse_ratings(_get(url), url)
        print(f'周榜公开页 {len(visited)}：{len(batch)} 条', flush=True)
        signature = repr([(x['fish_id'], x['map_id'], x['weight_kg'], x['player_name'], x['record_date']) for x in batch])
        if signature in signatures:
            raise RuntimeError('周榜返回重复内容；保留缓存')
        signatures.add(signature)
        rows.extend(batch)
        if not next_url:
            break
        url = next_url
        time.sleep(1)
    else:
        raise RuntimeError('周榜超过20页上限；保留缓存')
    if len(rows) < 100:
        raise RuntimeError('周榜记录不足100条；保留缓存')
    thresholds = trophy_thresholds({x['fish_id'] for x in rows})
    now = datetime.now().astimezone()
    week = (now.date() - timedelta(days=now.weekday())).isoformat()
    unique = {}
    for row in rows:
        threshold = thresholds.get(row['fish_id'], {})
        identity = [week, 'CN', 'records', *(row[k] for k in ('fish_id', 'map_id', 'weight_kg', 'player_name', 'record_date', 'bait_ids'))]
        key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        unique[key] = dict(row, source_key=key, week_key=week, region='CN', category='records', source_name=SOURCE_NAME,
                           collected_at=now.isoformat(), trophy_threshold_kg=threshold.get('kg'),
                           threshold_source_url=threshold.get('source_url'), threshold_checked_at=threshold.get('checked_at'))
    return WeeklyBatch(list(unique.values()), len(visited))
