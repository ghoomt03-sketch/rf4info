from __future__ import annotations

import gzip
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup

from ..config import DATA_DIR, RAW_DIR, RF4STAT_BASE
from ..db import read_frame, upsert_community_spots
from ..normalization import canonical_bait, canonical_fish, normalize_coordinate, normalize_map_id, normalize_spot_label
from ..normalization import rf4db_bait_catalog, rf4db_fish_catalog


SOURCE_NAME = "RF4-STAT 帖子"
STATE_PATH = DATA_DIR / "rf4stat_posts_state.json"


class PostBatch(list):
    message = ""


class PostsCollectionError(RuntimeError):
    def __init__(self, message: str, rows=(), saved_keys=()):
        super().__init__(message)
        self.saved_keys = set(saved_keys) | {row['source_key'] for row in rows}
        self.saved_rows = len(self.saved_keys)


def _initial_state(now: datetime) -> dict:
    return {'anchor': now.isoformat(), 'cutoff': (now - timedelta(days=90)).isoformat(),
            'next_page': 1, 'history_status': 'pending'}


def load_posts_state() -> dict:
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATE_PATH)


def _epoch(value) -> str | None:
    try:
        result = datetime.fromtimestamp(int(value), timezone.utc)
        return result.isoformat() if 2020 <= result.year <= datetime.now(timezone.utc).year + 1 else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def parse_page_response(status: int, html: str, page: int) -> BeautifulSoup:
    from ..rf4stat_browser import BrowserCollectionError, POSTS_MANUAL_MESSAGE, is_challenge

    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text() if soup.title else ""
    if is_challenge(title, soup.get_text(" ", strip=True)):
        raise BrowserCollectionError(POSTS_MANUAL_MESSAGE)
    if status == 403:
        raise BrowserCollectionError("HTTP 403：RF4-STAT 拒绝帖子分页访问，请关闭 VPN 后在帖子窗口检查；已保存页和旧缓存保留。")
    if status == 429:
        raise BrowserCollectionError("HTTP 429：RF4-STAT 限制频率，停止本批；已保存页和旧缓存保留。")
    if status != 200:
        raise BrowserCollectionError(f"帖子分页返回 HTTP {status}，停止本批；保留续采位置。")
    if soup.select_one('.post-item[data-post-id]'):
        return soup
    # A real empty AJAX fragment is the only unambiguous public pagination end.
    if not html.strip():
        return soup
    raise BrowserCollectionError(f"帖子第 {page} 页未返回公开帖子（可能是验证、登录、Premium 或脚本加载页），未将其视为历史末端。")


def parse_posts(soup: BeautifulSoup, collected: datetime, fish_catalog=None, bait_catalog=None,
                source_page_url: str | None = None) -> list[dict]:
    fish_catalog = rf4db_fish_catalog() if fish_catalog is None else fish_catalog
    bait_catalog = rf4db_bait_catalog() if bait_catalog is None else bait_catalog
    result = []
    community_id = None
    if source_page_url:
        from urllib.parse import parse_qs, urlparse
        parsed_source = urlparse(source_page_url)
        path_match = re.fullmatch(r'/community/(\d+)', parsed_source.path.rstrip('/'))
        query_communities = parse_qs(parsed_source.query, keep_blank_values=True).get('community', [])
        if path_match:
            community_id = path_match.group(1)
        elif len(query_communities) == 1 and re.fullmatch(r'\d+', query_communities[0]):
            community_id = query_communities[0]
    for post in soup.select('.post-item[data-post-id]'):
        post_id = str(post.get('data-post-id', ''))
        if not post_id.isdigit():
            continue
        created = _epoch(post.get('data-created-at'))
        published = _epoch(post.get('data-published-at'))
        reported = published or created
        location = post.select_one('.location [data-filter=location]')
        map_raw = location.get_text(' ', strip=True) if location else ''
        map_id = normalize_map_id(map_raw)
        position = post.select_one('[data-position]')
        raw_position = position.get('data-position', '') if position else ''
        if not raw_position:
            node = post.select_one('.position')
            raw_position = node.get_text(' ', strip=True) if node else ''
        coordinate = normalize_coordinate(raw_position)
        spot_label = normalize_spot_label(raw_position)
        clip_node = post.select_one('.clip')
        clip = clip_node.get_text(' ', strip=True) if clip_node else None
        depth_node = post.select_one('.post-extra-depth-val')
        depth_raw = depth_node.get_text(' ', strip=True) if depth_node else None
        rig = None
        for image in post.select('.spot-col img[title]'):
            match = re.fullmatch(r'钓法[:：]\s*(.+)', image.get('title', ''))
            if match:
                rig = match.group(1)
        if rig is None and post.select_one('.post-depth-bobber'):
            rig = '浮钓'
        bait_pairs = []
        for tooltip in post.select('.baits .bait-icon .tooltip'):
            image = tooltip.select_one('img[src]')
            text = tooltip.select_one('.tooltip-data')
            if image and text:
                bait_pairs.append((text.get_text(' ', strip=True), image['src'].rsplit('/', 1)[-1].rsplit('.', 1)[0]))
        if not bait_pairs:
            bait_pairs = [(n.get_text(' ', strip=True), None) for n in post.select('.baits [data-filter=bait]')]
        baits = [canonical_bait(raw, stable_id, catalog=bait_catalog) for raw, stable_id in bait_pairs]
        bait_names = list(dict.fromkeys(item[1] for item in baits))
        fish_items = {}
        for icon in post.select('.fish-list .fish-icon'):
            link = icon.select_one('a[href^="fish/"]')
            name = icon.select_one('[data-filter=fish]')
            if not link or not name:
                continue
            fish_id = link['href'].rstrip('/').rsplit('/', 1)[-1]
            if not re.fullmatch(r'[A-Za-z0-9_-]+', fish_id):
                continue
            fish_items[fish_id] = name.get_text(' ', strip=True)
        for fish_id, fish_raw in fish_items.items():
            _, fish_name, fish_ok = canonical_fish(fish_raw, fish_id, catalog=fish_catalog)
            validation = []
            if not map_id:
                validation.append('地图未映射')
            if not coordinate and not spot_label and raw_position:
                validation.append('坐标无效')
            if not reported:
                validation.append('时间无效')
            if not fish_ok:
                validation.append('鱼种未映射RF4DB')
            if any(not item[2] for item in baits):
                validation.append('饵料未映射RF4DB')
            payload = {
                'community_id': community_id, 'post_id': post_id, 'map_name_raw': map_raw,
                'position_raw': raw_position, 'clip_raw': clip, 'depth_raw_unit_unconfirmed': depth_raw,
                'created_at': created, 'published_at': published,
                'time_basis': 'published_at' if published else 'created_at',
                'baits_raw': [{'name': name, 'id': stable_id} for name, stable_id in bait_pairs],
                'weather_raw': [n.get_text(' ', strip=True) for n in post.select('.post-weather')],
                'fish_id': fish_id, 'fish_name_raw': fish_raw,
            }
            result.append({
                'source_key': f'rf4stat-post:{post_id}:{fish_id}', 'source_name': SOURCE_NAME,
                'source_record_id': post_id, 'evidence_type': 'community_post',
                'map_id': map_id, 'map_name_raw': map_raw, 'coordinate': coordinate,
                'spot_label': spot_label,
                'fish_id': fish_id, 'fish_name': fish_name, 'fish_name_raw': fish_raw,
                'baits': ' / '.join(bait_names) or None, 'rig': rig, 'clip': clip, 'depth': None,
                'quality_label': '社区帖子（非独立渔获）', 'catch_count': None,
                'reported_at': reported, 'collected_at': collected.isoformat(),
                'validation_status': '；'.join(validation) if validation else '有效',
                'raw_payload': json.dumps(payload, ensure_ascii=False),
                'source_url': f'{RF4STAT_BASE}/posts/{post_id}',
            })
    return result


def collect_pages(fetch, max_pages=10, request_delay=3.0, writer=None) -> PostBatch:
    """Commit complete pages before advancing the persistent historical cursor."""
    writer = upsert_community_spots if writer is None else writer
    max_pages = max(1, min(10, max_pages))
    now = datetime.now(timezone.utc)
    state = load_posts_state()
    if not state:
        state = _initial_state(now)
    cutoff = datetime.fromisoformat(state['cutoff'])
    state.update(last_error='', batch_pages=0, batch_rows=0, phase='recent')
    saved = PostBatch()
    fingerprints = set()
    seen_pages = set()

    def process(page: int, historical: bool) -> bool:
        if seen_pages:
            time.sleep(max(3.0, request_delay))
        state.update(current_page=page, phase='history' if historical else 'recent')
        _save_state(state)
        soup = fetch(page)
        posts = soup.select('.post-item[data-post-id]')
        if not posts:
            state.update(history_status='source_end', stop_reason='公开分页返回空页，尚不能保证覆盖90天')
            _save_state(state)
            return False
        ids = sorted({str(p.get('data-post-id')) for p in posts})
        fingerprint = hashlib.sha256(','.join(ids).encode()).hexdigest()
        if fingerprint in fingerprints:
            raise RuntimeError('帖子分页重复返回相同内容，已停止；不标记90天完成。')
        fingerprints.add(fingerprint)
        rows = parse_posts(soup, now)
        if not rows:
            raise RuntimeError(f'帖子第{page}页无法解析鱼种证据，保留当前页码。')
        dates = [datetime.fromisoformat(r['reported_at']) for r in rows if r['reported_at']]
        oldest = min(dates) if dates else None
        next_before = int(state.get('next_page', 1))
        previous_oldest = state.get('last_history_oldest')
        if historical and oldest and previous_oldest and page >= next_before:
            stalled = oldest >= datetime.fromisoformat(previous_oldest)
            state['stalled_pages'] = int(state.get('stalled_pages', 0)) + 1 if stalled else 0
            if state['stalled_pages'] >= 2:
                raise RuntimeError('帖子连续两页未向更早时间推进，已停止；不标记90天完成。')
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        with gzip.open(RAW_DIR / f'{stamp}-rf4stat-posts-{page:05d}.html.gz', 'wb') as handle:
            handle.write(str(soup).encode('utf-8'))
        writer(rows)
        saved.extend(rows)
        seen_pages.add(page)
        state.update(batch_pages=len(seen_pages), batch_rows=len(saved), last_page_ids=ids,
                     next_page=max(next_before, page + 1))
        if dates:
            prior_min = datetime.fromisoformat(state.get('oldest_at') or oldest.isoformat())
            prior_max = datetime.fromisoformat(state.get('latest_at') or max(dates).isoformat())
            state.update(oldest_at=min(prior_min, oldest).isoformat(), latest_at=max(prior_max, max(dates)).isoformat())
            if historical or page >= next_before:
                state['last_history_oldest'] = oldest.isoformat()
        if oldest and oldest <= cutoff:
            state.update(history_status='target_boundary', stop_reason='已到首次补采锚点前90天边界；不保证分页无缺漏')
            _save_state(state)
            return False
        _save_state(state)
        return True

    try:
        for page in range(1, min(3, max_pages) + 1):
            if not process(page, False):
                break
        else:
            if state.get('history_status') not in ('target_boundary', 'source_end'):
                start = max(4, int(state['next_page']) - 1)
                for page in range(start, start + max_pages - len(seen_pages)):
                    if not process(page, True):
                        break
                else:
                    state.update(history_status='pending', stop_reason='本批页数上限已到，下次刷新继续历史补采')
        state['phase'] = 'idle'
        _save_state(state)
    except Exception as exc:
        state.update(phase='failed', last_error=str(exc), stop_reason='采集中断，保留已提交页和续采位置')
        _save_state(state)
        raise PostsCollectionError(f'{exc} 本批已保存{len(seen_pages)}个新请求页、{len(saved)}条帖子鱼种证据；下一历史页{state["next_page"]}。', saved) from exc
    saved.message = f'本批{len(seen_pages)}页、{len(saved)}条帖子鱼种证据；{state.get("stop_reason", "")}；下一历史页{state["next_page"]}'
    return saved


def collect_reader(reader, max_pages=10) -> PostBatch:
    """Retain already rendered public posts even when the next request is blocked."""
    now = datetime.now(timezone.utc)
    soup = reader.visible_posts()
    visible_source_url = getattr(getattr(reader, 'page', None), 'url', None)
    visible_rows = parse_posts(
        soup, now,
        source_page_url=visible_source_url if isinstance(visible_source_url, str) else None,
    )
    visible_ids = {row['source_record_id'] for row in visible_rows}
    if visible_rows:
        for row in visible_rows:
            payload = json.loads(row['raw_payload'])
            payload.update(acquisition='browser_visible_dom', dom_read_at=now.isoformat())
            row['raw_payload'] = json.dumps(payload, ensure_ascii=False)
        upsert_community_spots(visible_rows)
        state = load_posts_state() or _initial_state(now)
        state.update(visible_import_at=now.isoformat(), visible_posts=len(visible_ids),
                     visible_rows=len(visible_rows), visible_dom_posts=len(soup.select('.post-item[data-post-id]')))
        _save_state(state)
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        with gzip.open(RAW_DIR / f'{stamp}-rf4stat-posts-visible.html.gz', 'wb') as handle:
            handle.write(str(soup).encode('utf-8'))
    if getattr(reader, 'can_paginate_posts', None) is False:
        combined = PostBatch({row['source_key']: row for row in visible_rows}.values())
        combined.message = (
            f'窗口现有内容保存{len(visible_ids)}篇帖子；当前页面无法安全推导分页条件，'
            '本次只采集已呈现内容。'
        )
        return combined
    try:
        batch = collect_pages(reader.posts_page, max_pages=max_pages)
    except PostsCollectionError as exc:
        raise PostsCollectionError(
            f'已从窗口现有内容保存{len(visible_ids)}篇帖子、{len(visible_rows)}条鱼种证据（不代表刚从网络刷新）。{exc}',
            visible_rows, exc.saved_keys,
        ) from exc
    combined = PostBatch({row['source_key']: row for row in [*visible_rows, *batch]}.values())
    combined.message = f'窗口现有内容保存{len(visible_ids)}篇帖子；{batch.message}'
    return combined


def collect_rf4stat_posts() -> PostBatch:
    from ..rf4stat_browser import BrowserReader, POSTS_URL, browser_lock

    with browser_lock(), BrowserReader(POSTS_URL) as reader:
        if not reader.page.locator('.post-item[data-post-id]').count():
            reader.get(POSTS_URL, 'rf4stat-posts')
            time.sleep(3.0)
        return collect_reader(reader)


def posts_progress() -> dict:
    state = load_posts_state()
    frame = read_frame('SELECT COUNT(DISTINCT source_record_id) AS posts, MIN(reported_at) AS oldest, MAX(reported_at) AS latest FROM community_spots WHERE source_name=?', (SOURCE_NAME,))
    state['inventory'] = frame.iloc[0].to_dict()
    return state
