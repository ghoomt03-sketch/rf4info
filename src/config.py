from __future__ import annotations

import os
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
MAP_IMAGE_DIR = DATA_DIR / "maps"
USER_SETTINGS_PATH = DATA_DIR / "user_settings.json"
APP_MODE = os.environ.get("RF4_APP_MODE", "local").strip().lower()
PUBLIC_MODE = APP_MODE == "public"
DB_PATH = Path(os.environ.get("RF4_DB_PATH", str(DATA_DIR / "rf4.sqlite3")))
if not DB_PATH.is_absolute():
    DB_PATH = ROOT_DIR / DB_PATH

RF4DB_BASE = "https://download.rf4db.com/en"
RF4DB_ZH_BASE = "https://download.rf4db.com/zh"
RF4STAT_BASE = "https://cn.rf4-stat.com"
USER_AGENT = "RF4LocalResearchTool/0.1 (+local Streamlit cache; public pages only)"
CATCH_PAGES_PER_REFRESH = 20
CATCH_RECORD_PAGES_PER_REFRESH = 20

# stable_id: (中文名, 英文名, 解锁等级)
MAPS: dict[str, tuple[str, str, int]] = {
    "level_000_home": ("别墅池塘", "Cottage Pond", 0),
    "level_001_lake": ("克马羚诺也湖", "Mosquito Lake", 1),
    "level_002_kirzah_river": ("惟有诺克河", "Winding Rivulet", 1),
    "level_019_american_pond": ("埃尔克湖", "Elk Lake", 1),
    "level_004_torfyanoe_ozero": ("旧奥斯特罗格湖", "Old Burg Lake", 12),
    "level_010_belaya": ("白河", "Belaya River", 12),
    "level_003_viiveri_lake": ("廓里湖", "Kuori Lake", 16),
    "level_007_grass_lake": ("梅德韦杰湖", "Bear Lake", 18),
    "level_005_volhov": ("沃尔霍夫河", "Volkhov River", 20),
    "level_013_sev_don": ("北顿涅茨河", "Seversky Donets River", 22),
    "level_008_sura_river": ("苏拉河", "Sura River", 24),
    "level_006_ladoga": ("拉多加湖", "Ladoga Lake", 26),
    "level_014_carp_lake": ("琥珀湖", "The Amber Lake", 26),
    "level_011_ladoga_02": ("拉多加湖群岛", "Ladoga archipelago", 27),
    "level_009_ahtuba": ("阿赫图巴河", "Akhtuba River", 28),
    "level_018_moskitnoe": ("铜湖", "Copper Lake", 29),
    "level_015_tunguska": ("下通古斯卡河", "Lower Tunguska River", 30),
    "level_016_yama": ("亚马河", "Yama River", 32),
    "level_012_north_sea": ("挪威海", "Norwegian Sea", 34),
}

WEATHER_ZH = {
    "clear": "晴",
    "clearNight": "晴夜",
    "partlyCloudy": "晴间多云",
    "cloudy": "多云",
    "showers": "阵雨",
    "rain": "雨",
    "heavyRain": "大雨",
    "thunderstorm": "雷雨",
    "snow": "雪",
}

WEATHER_ALIASES = {
    "clear": "clear",
    "clear (night)": "clearNight",
    "clear night": "clearNight",
    "partly cloudy": "partlyCloudy",
    "cloudy": "cloudy",
    "showers": "showers",
    "rain": "rain",
    "heavy rain": "heavyRain",
    "storm": "thunderstorm",
    "thunderstorm": "thunderstorm",
    "snow": "snow",
    "晴": "clear",
    "晴夜": "clearNight",
    "晴(夜间)": "clearNight",
    "晴间多云": "partlyCloudy",
    "多云": "cloudy",
    "阵雨": "showers",
    "雨": "rain",
    "大雨": "heavyRain",
    "雷雨": "thunderstorm",
    "雷暴": "thunderstorm",
    "雪": "snow",
}


def normalize_weather(value: str | None) -> str:
    """将页面显示名统一为天气稳定代码；未知值原样保留。"""
    if not value:
        return "unknown"
    cleaned = value.strip()
    return WEATHER_ALIASES.get(cleaned.casefold(), cleaned)

FISH_ZH = {
    "crucian": "银鲫",
    "carassius": "金鲫",
    "c.carp": "金鲫",
    "carp": "普通鲤鱼",
    "m_carp": "镜鲤",
    "n_carp": "无鳞鲤",
    "karasekarp": "鲤鲫",
    "tench": "丁鱥",
    "kw_tench": "库尔斯多夫丁鱥",
    "din_m_carp": "丁克斯镜鲤",
    "din_l_carp": "丁克斯带形鲤",
    "din_n_carp": "丁克斯无鳞鲤",
    "dace": "雅罗鱼",
    "makrel": "大西洋鲭",
    "coalfish": "绿青鳕",
    "s_saurus": "竹刀鱼",
    "poutassou": "蓝鳕",
    "sardina_p": "沙丁鱼",
    "m_scorpius": "短角大杜父鱼",
}

