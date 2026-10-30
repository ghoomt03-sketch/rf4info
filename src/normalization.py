from __future__ import annotations

import json
import hashlib
import gzip
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from bs4 import BeautifulSoup

from .browser_client import browser_get
from .catalog import rf4db_fish_catalog
from .config import DATA_DIR, DB_PATH, FISH_ZH, MAPS, RAW_DIR, RF4DB_ZH_BASE


RU_MAP_ALIASES = {
    "дачный пруд": "level_000_home",
    "оз. комариное": "level_001_lake",
    "р. вьюнок": "level_002_kirzah_river",
    "оз. лосиное": "level_019_american_pond",
    "оз. старый острог": "level_004_torfyanoe_ozero",
    "р. белая": "level_010_belaya",
    "оз. куори": "level_003_viiveri_lake",
    "оз. медвежье": "level_007_grass_lake",
    "р. волхов": "level_005_volhov",
    "р. северский донец": "level_013_sev_don",
    "р. сура": "level_008_sura_river",
    "ладожское оз.": "level_006_ladoga",
    "ладожское озеро": "level_006_ladoga",
    "оз. янтарное": "level_014_carp_lake",
    "ладожский архипелаг": "level_011_ladoga_02",
    "р. ахтуба": "level_009_ahtuba",
    "оз. медное": "level_018_moskitnoe",
    "р. нижняя тунгуска": "level_015_tunguska",
    "р. яма": "level_016_yama",
    "норвежское море": "level_012_north_sea",
}

TRANSLATION_PATH = DATA_DIR / "translations_ru_zh.json"
FISH_ALIAS_PATH = DATA_DIR / "fish_aliases_ru_rf4db.json"
RF4DB_RU_FISH_ALIAS_PATH = DATA_DIR / "fish_aliases_rf4db_ru.json"
RF4DB_FISH_CATALOG_PATH = DATA_DIR / "fish_names_rf4db_zh.json"
BAIT_ALIAS_PATH = DATA_DIR / "bait_aliases_ru_rf4db.json"
RF4DB_BAIT_CATALOG_PATH = DATA_DIR / "bait_names_rf4db_zh.json"


def normalize_map_id(raw_name: str | None) -> str | None:
    cleaned = re.sub(r"\s+", " ", str(raw_name or "")).strip().casefold()
    if cleaned in RU_MAP_ALIASES:
        return RU_MAP_ALIASES[cleaned]
    for map_id, (name_zh, name_en, _) in MAPS.items():
        if cleaned in {name_zh.casefold(), name_en.casefold()}:
            return map_id
    return None


def normalize_coordinate(value: str | None) -> str | None:
    match = re.search(r"(-?\d+(?:[.,]\d+)?)\s*[:;]\s*(-?\d+(?:[.,]\d+)?)", str(value or ""))
    if not match:
        return None

    def compact(number: str) -> str:
        parsed = float(number.replace(",", "."))
        return str(int(parsed)) if parsed.is_integer() else f"{parsed:.2f}".rstrip("0").rstrip(".")

    return f"{compact(match.group(1))}:{compact(match.group(2))}"


def normalize_spot_label(value: str | None) -> str | None:
    """保留RF4-STAT单数值坑位编号；它不是坐标、深度或卡线。"""
    match = re.fullmatch(r"\s*(\d{1,3})\s*(?:坑)?\s*", str(value or ""))
    if not match:
        return None
    return f"{int(match.group(1))}坑"


def _translation_cache() -> dict[str, str]:
    try:
        value = json.loads(TRANSLATION_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_translation_cache(value: dict[str, str]) -> None:
    TRANSLATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = TRANSLATION_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(TRANSLATION_PATH)


def translate_ru_terms(values: list[str], request_delay: float = 0.15) -> dict[str, str]:
    """翻译俄语枚举；翻译服务失败时保留旧缓存，由调用方生成中文占位符。"""
    cache = _translation_cache()
    missing = list(dict.fromkeys(value.strip() for value in values if value and value.strip() not in cache))
    for offset in range(0, len(missing), 35):
        batch = missing[offset:offset + 35]
        try:
            response = browser_get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": "ru", "tl": "zh-CN", "dt": "t", "q": "\n".join(batch)},
                timeout=30,
            )
            translated = "".join(part[0] for part in response.json()[0] if part and part[0]).splitlines()
            if len(translated) == len(batch):
                cache.update(zip(batch, translated))
                _save_translation_cache(cache)
        except (RuntimeError, ValueError, TypeError, KeyError):
            break
        time.sleep(request_delay)
    return cache


def _alias_key(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold().replace("ё", "е")


def _json_dict(path: Path) -> dict[str, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return {str(key): str(item) for key, item in value.items()} if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_json_dict(path: Path, value: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _official_model_name(raw_name: str | None) -> bool:
    raw = str(raw_name or "").strip()
    return bool(raw and re.search(r"[A-Za-z]", raw) and not re.search(r"[\u0400-\u04ff\u4e00-\u9fff]", raw))


def load_bait_aliases() -> dict[str, str]:
    aliases = {_alias_key(key): value for key, value in _json_dict(BAIT_ALIAS_PATH).items()}
    # RF4MAP 偶尔把小鱼/鱼块直接作为饵料名称；复用 RF4DB 鱼种稳定 ID，
    # 避免同一种鱼在“鱼种”和“饵料”两处产生两套译名。
    for raw_name, fish_id in _json_dict(RF4DB_RU_FISH_ALIAS_PATH).items():
        aliases.setdefault(_alias_key(raw_name), f"fish:{fish_id}")
    return aliases


def split_bait_terms(values: list[str] | str | None) -> list[str]:
    """拆开社区投稿中用分号合写的多种饵，保留产品名内部的空格和连字符。"""
    source = [values] if isinstance(values, str) else list(values or [])
    return list(dict.fromkeys(
        part.strip()
        for value in source
        for part in re.split(r"\s*[;；]\s*", str(value or ""))
        if part.strip()
    ))


def save_bait_aliases(pairs: list[tuple[str, str]]) -> None:
    aliases = _json_dict(BAIT_ALIAS_PATH)
    aliases.update({_alias_key(raw): stable_id for raw, stable_id in pairs if raw and stable_id})
    _save_json_dict(BAIT_ALIAS_PATH, aliases)


def refresh_rf4db_bait_aliases(request_delay: float = 0.05) -> dict[str, str]:
    """一次性缓存 RF4DB 俄文鱼饵与拟饵目录，供 RF4MAP 原名归并。"""
    aliases = load_bait_aliases()
    if len(aliases) >= 2500:
        return aliases
    for group in ("store-52", "store-77"):
        for page in range(1, 100):
            try:
                response = browser_get(
                    f"{RF4DB_ZH_BASE.replace('/zh', '/ru')}/gear",
                    params={"group": group, "page": page},
                    timeout=30,
                )
            except RuntimeError:
                break
            soup = BeautifulSoup(response.text, "lxml")
            cards = soup.select('a[href*="/ru/wiki/baits/"], a[href*="/ru/wiki/lures/"]')
            if not cards:
                break
            for card in cards:
                heading = card.find("h2")
                stable_id = card.get("href", "").rstrip("/").split("/")[-1]
                raw_name = heading.get_text(" ", strip=True) if heading else ""
                if stable_id and raw_name:
                    aliases[_alias_key(raw_name)] = stable_id
            next_href = f"/ru/gear?group={group}&page={page + 1}"
            if soup.find("a", href=next_href) is None:
                break
            time.sleep(request_delay)
    _save_json_dict(BAIT_ALIAS_PATH, aliases)
    return aliases


def rf4db_bait_catalog() -> dict[str, str]:
    return _json_dict(RF4DB_BAIT_CATALOG_PATH)


def refresh_rf4db_bait_catalog(stable_ids: list[str], request_delay: float = 0.08) -> dict[str, str]:
    """按稳定 ID 从 RF4DB 中文详情页补齐鱼饵/拟饵官方显示名。"""
    catalog = rf4db_bait_catalog()
    missing = [
        item for item in dict.fromkeys(stable_ids)
        if item and not item.startswith("fish:") and item not in catalog
    ]
    changed = False
    for stable_id in missing:
        for category in ("baits", "lures"):
            try:
                response = browser_get(
                    f"{RF4DB_ZH_BASE}/wiki/{category}/{stable_id}",
                    timeout=30,
                )
                if response.status_code == 404:
                    continue
                heading = BeautifulSoup(response.text, "lxml").find("h1")
                name = heading.get_text(" ", strip=True) if heading else ""
                if name:
                    catalog[stable_id] = name
                    changed = True
                    break
            except RuntimeError:
                continue
        time.sleep(request_delay)
    if changed:
        _save_json_dict(RF4DB_BAIT_CATALOG_PATH, catalog)
    return catalog


def rebuild_bait_catalog_from_raw_cache() -> tuple[int, int]:
    """从本地 RF4DB 中文页和 RF4-STAT 原页重建饵料主表，避免重复联网。"""
    aliases = _json_dict(BAIT_ALIAS_PATH)
    catalog = rf4db_bait_catalog()
    for path in RAW_DIR.glob("*.html.gz"):
        if "catch" not in path.name and "rf4stat" not in path.name:
            continue
        try:
            with gzip.open(path, "rb") as handle:
                soup = BeautifulSoup(handle.read(), "lxml")
        except (OSError, EOFError):
            continue
        for link in soup.select('a[href*="/zh/wiki/baits/"], a[href*="/zh/wiki/lures/"]'):
            stable_id = link.get("href", "").rstrip("/").split("/")[-1]
            name = link.get_text(" ", strip=True) or str(link.get("title") or "").strip()
            if stable_id and (re.search(r"[\u4e00-\u9fff]", name) or _official_model_name(name)):
                catalog[stable_id] = name
        for row in soup.select("tr.load-row"):
            bait_link = row.select_one("a[data-filter='bait']")
            if not bait_link:
                continue
            icon_cell = bait_link.find_parent("td").find_previous_sibling("td")
            icon = icon_cell.find("img", src=True) if icon_cell else None
            stable_id = icon.get("src", "").rsplit("/", 1)[-1].rsplit(".", 1)[0] if icon else ""
            raw_name = bait_link.get_text(" ", strip=True)
            if stable_id and raw_name:
                aliases[_alias_key(raw_name)] = stable_id
    _save_json_dict(BAIT_ALIAS_PATH, aliases)
    _save_json_dict(RF4DB_BAIT_CATALOG_PATH, catalog)
    return len(aliases), len(catalog)


def canonical_bait(
    raw_name: str | None,
    stable_id: str | None = None,
    *,
    catalog: dict[str, str] | None = None,
    aliases: dict[str, str] | None = None,
) -> tuple[str, str, bool]:
    raw = str(raw_name or "").strip()
    bait_aliases = aliases if aliases is not None else load_bait_aliases()
    canonical_id = str(stable_id or bait_aliases.get(_alias_key(raw), ""))
    if canonical_id.startswith(("rf4map-bait:", "rf4stat-bait:")):
        canonical_id = bait_aliases.get(_alias_key(raw), canonical_id)
    bait_catalog = catalog if catalog is not None else rf4db_bait_catalog()
    if canonical_id and canonical_id in bait_catalog:
        return canonical_id, bait_catalog[canonical_id], True
    if canonical_id.startswith("fish:"):
        fish_id = canonical_id.removeprefix("fish:")
        fish_name = rf4db_fish_catalog().get(fish_id)
        if fish_name:
            return canonical_id, fish_name, True
    if _official_model_name(raw):
        return canonical_id or f"model:{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:12]}", raw, True
    safe_id = re.sub(r"[^A-Za-z0-9_.:-]+", "-", canonical_id).strip("-") or "unknown"
    return canonical_id or safe_id, f"未收录标准饵料（{safe_id}）", False


def refresh_rf4db_fish_catalog(stable_ids: list[str], request_delay: float = 0.1) -> dict[str, str]:
    """从 RF4DB 公开中文鱼种页补齐尚未在本地渔获出现的标准名。"""
    catalog = rf4db_fish_catalog()
    missing = [fish_id for fish_id in dict.fromkeys(stable_ids) if fish_id and fish_id not in catalog]
    changed = False
    for fish_id in missing:
        try:
            response = browser_get(
                f"{RF4DB_ZH_BASE}/fishes/{fish_id}",
                timeout=30,
            )
            heading = BeautifulSoup(response.text, "lxml").find("h1")
            name = heading.get_text(" ", strip=True) if heading else ""
            if re.search(r"[\u4e00-\u9fff]", name):
                catalog[fish_id] = name
                changed = True
        except RuntimeError:
            continue
        time.sleep(request_delay)
    if changed:
        persisted = {key: value for key, value in catalog.items() if key not in FISH_ZH or FISH_ZH[key] != value}
        RF4DB_FISH_CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = RF4DB_FISH_CATALOG_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(persisted, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(RF4DB_FISH_CATALOG_PATH)
    return catalog


def load_rf4stat_fish_aliases() -> dict[str, str]:
    try:
        value = json.loads(FISH_ALIAS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        value = {}
    aliases = {_alias_key(key): str(fish_id) for key, fish_id in value.items() if key and fish_id}
    try:
        rf4db_aliases = json.loads(RF4DB_RU_FISH_ALIAS_PATH.read_text(encoding="utf-8"))
        if isinstance(rf4db_aliases, dict):
            aliases.update({_alias_key(key): str(fish_id) for key, fish_id in rf4db_aliases.items()})
    except (OSError, json.JSONDecodeError):
        pass
    if DB_PATH.exists():
        try:
            with closing(sqlite3.connect(DB_PATH)) as conn, conn:
                pairs = conn.execute(
                    """SELECT fish_name_raw, fish_id FROM community_spots
                       WHERE source_name='RF4-STAT' AND fish_name_raw IS NOT NULL AND fish_id IS NOT NULL"""
                ).fetchall()
            aliases.update({_alias_key(raw_name): str(fish_id) for raw_name, fish_id in pairs})
        except sqlite3.Error:
            pass
    return aliases


def refresh_rf4db_fish_aliases(max_pages: int = 20, request_delay: float = 0.1) -> dict[str, str]:
    """从 RF4DB 俄文鱼种目录取得俄文原名→稳定 ID，不依赖机器翻译。"""
    aliases = load_rf4stat_fish_aliases()
    try:
        persisted = json.loads(RF4DB_RU_FISH_ALIAS_PATH.read_text(encoding="utf-8"))
        if isinstance(persisted, dict) and len(persisted) >= 200:
            return aliases
    except (OSError, json.JSONDecodeError):
        pass
    discovered: dict[str, str] = {}
    for page in range(1, max_pages + 1):
        try:
            response = browser_get(
                f"{RF4DB_ZH_BASE.replace('/zh', '/ru')}/fishes",
                params={"page": page},
                timeout=30,
            )
        except RuntimeError:
            break
        soup = BeautifulSoup(response.text, "lxml")
        links = soup.select('a[href*="/ru/fishes/"]')
        if not links:
            break
        for link in links:
            fish_id = link.get("href", "").rstrip("/").split("/")[-1]
            raw_name = link.get_text(" ", strip=True).split("★", 1)[0].strip()
            if fish_id and raw_name:
                discovered[raw_name] = fish_id
        if soup.find("a", href=f"/ru/fishes?page={page + 1}") is None:
            break
        time.sleep(request_delay)
    if discovered:
        RF4DB_RU_FISH_ALIAS_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = RF4DB_RU_FISH_ALIAS_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(discovered, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(RF4DB_RU_FISH_ALIAS_PATH)
        aliases.update({_alias_key(key): value for key, value in discovered.items()})
    return aliases


def save_rf4stat_fish_aliases(pairs: list[tuple[str, str]]) -> None:
    aliases = load_rf4stat_fish_aliases()
    aliases.update({_alias_key(raw_name): fish_id for raw_name, fish_id in pairs if raw_name and fish_id})
    FISH_ALIAS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = FISH_ALIAS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(aliases, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(FISH_ALIAS_PATH)


def canonical_fish(
    raw_name: str | None,
    stable_id: str,
    *,
    catalog: dict[str, str] | None = None,
    aliases: dict[str, str] | None = None,
) -> tuple[str, str, bool]:
    """把外部鱼名归并到 RF4DB 稳定 ID；未知项不采用机器译名。"""
    fish_catalog = catalog if catalog is not None else rf4db_fish_catalog()
    canonical_id = stable_id
    if stable_id.startswith("rf4map:"):
        canonical_id = (aliases if aliases is not None else load_rf4stat_fish_aliases()).get(
            _alias_key(raw_name), stable_id
        )
    if canonical_id in fish_catalog:
        return canonical_id, fish_catalog[canonical_id], True
    safe_id = re.sub(r"[^A-Za-z0-9_.:-]+", "-", canonical_id).strip("-") or "unknown"
    return canonical_id, f"未收录中文鱼种（{safe_id}）", False


def reconcile_community_fish_names() -> int:
    """用当前 RF4DB 主表修正既有社区鱼种与饵料缓存。"""
    if not DB_PATH.exists():
        return 0
    catalog = rf4db_fish_catalog()
    aliases = load_rf4stat_fish_aliases()
    bait_catalog = rf4db_bait_catalog()
    bait_aliases = load_bait_aliases()
    changed = 0
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn, conn:
            rows = conn.execute(
                "SELECT source_key, fish_id, fish_name, fish_name_raw, baits, raw_payload, validation_status FROM community_spots"
            ).fetchall()
            updates = []
            for source_key, fish_id, fish_name, fish_name_raw, baits, raw_payload, validation_status in rows:
                canonical_id, canonical_name, mapped = canonical_fish(
                    fish_name_raw, str(fish_id or "unknown"), catalog=catalog, aliases=aliases
                )
                payload = {}
                try:
                    payload = json.loads(raw_payload or "{}")
                    if not isinstance(payload, dict):
                        payload = {}
                    raw_baits = payload.get("bait_raw", [])
                except (json.JSONDecodeError, TypeError):
                    raw_baits = []
                raw_baits = split_bait_terms(raw_baits)
                bait_results = [
                    canonical_bait(raw, None, catalog=bait_catalog, aliases=bait_aliases)
                    for raw in raw_baits if raw
                ]
                if isinstance(payload.get("baits_raw"), list):
                    bait_results = [
                        canonical_bait(item.get("name"), item.get("id"), catalog=bait_catalog, aliases=bait_aliases)
                        for item in payload["baits_raw"] if isinstance(item, dict) and item.get("name")
                    ]
                canonical_baits = " / ".join(result[1] for result in bait_results) or baits
                statuses = [
                    value for value in str(validation_status or "").split("；")
                    if value and value not in {"有效", "鱼种未映射RF4DB", "饵料未映射RF4DB"}
                ]
                if not mapped:
                    statuses.append("鱼种未映射RF4DB")
                if any(not result[2] for result in bait_results):
                    statuses.append("饵料未映射RF4DB")
                normalized_status = "；".join(statuses) if statuses else "有效"
                if (canonical_id, canonical_name, canonical_baits, normalized_status) != (
                    fish_id, fish_name, baits, validation_status
                ):
                    updates.append((canonical_id, canonical_name, canonical_baits, normalized_status, source_key))
            if updates:
                conn.executemany(
                    "UPDATE community_spots SET fish_id=?, fish_name=?, baits=?, validation_status=? WHERE source_key=?",
                    updates,
                )
                changed = len(updates)
    except sqlite3.Error:
        return 0
    return changed


def reconcile_catch_bait_names() -> int:
    """将 RF4DB 渔获缓存内旧的编号占位替换为 RF4DB 正式物品名。"""
    if not DB_PATH.exists():
        return 0
    catalog = rf4db_bait_catalog()
    placeholder = re.compile(r"未收录中文(?:鱼饵|拟饵)（([^）]+)）")
    updates: list[tuple[str, str]] = []
    try:
        with sqlite3.connect(DB_PATH) as conn:
            for source_id, baits in conn.execute(
                """SELECT source_id, baits FROM catch_records
                   WHERE baits LIKE '%未收录中文%' OR baits = '未收录中文饵料'"""
            ):
                normalized = placeholder.sub(
                    lambda match: catalog.get(match.group(1), match.group(0)), str(baits or "")
                )
                if normalized == "未收录中文饵料":
                    normalized = "未公开饵料（来源未提供）"
                if normalized != baits:
                    updates.append((normalized, source_id))
            if updates:
                conn.executemany("UPDATE catch_records SET baits=? WHERE source_id=?", updates)
    except sqlite3.Error:
        return 0
    return len(updates)


def chinese_name(raw_name: str | None, category: str, stable_id: str, translations: dict[str, str]) -> str:
    raw = str(raw_name or "").strip()
    if category == "鱼种" and stable_id in FISH_ZH:
        return FISH_ZH[stable_id]
    candidate = raw if re.search(r"[\u4e00-\u9fff]", raw) else translations.get(raw, "")
    if re.search(r"[\u4e00-\u9fff]", candidate):
        cleaned = re.sub(r"[A-Za-z]+", "", candidate).strip(" -_/;；")
        if cleaned:
            return cleaned
    safe_id = re.sub(r"[^A-Za-z0-9_.:-]+", "-", stable_id).strip("-") or "unknown"
    return f"未收录中文{category}（{safe_id}）"

