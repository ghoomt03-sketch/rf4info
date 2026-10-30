from __future__ import annotations

import json
import hashlib
import re
from collections import Counter
from datetime import timedelta

import pandas as pd

from .config import FISH_ZH, WEATHER_ZH
from .db import read_frame
from .habit_evidence import all_source_habit_records, rf4stat_habit_records, source_breakdown


def latest_weather() -> pd.DataFrame:
    frame = read_frame(
        """WITH latest AS (
            SELECT map_id, MAX(collected_at) AS collected_at
            FROM weather_forecasts GROUP BY map_id
        )
        SELECT w.*, m.name_zh AS map_name, m.unlock_level
        FROM weather_forecasts w
        JOIN latest l ON w.map_id=l.map_id AND w.collected_at=l.collected_at
        JOIN maps m ON m.map_id=w.map_id
        ORDER BY w.horizon, m.unlock_level, m.name_zh"""
    )
    if not frame.empty:
        frame["天气"] = frame["weather_code"].map(WEATHER_ZH).fillna(frame["weather_code"])
    return frame


def latest_earnings() -> pd.DataFrame:
    frame = read_frame(
        """WITH latest AS (SELECT MAX(collected_at) AS collected_at FROM earnings_samples)
        SELECT e.*, m.name_zh AS map_name, m.unlock_level
        FROM earnings_samples e
        JOIN latest l ON e.collected_at=l.collected_at
        LEFT JOIN maps m ON m.map_id=e.map_id
        ORDER BY e.rank_no"""
    )
    if not frame.empty:
        frame["composition"] = frame["composition"].map(lambda value: _chinese_or_pending(value, "收益构成"))
    return frame


def latest_map_assets() -> pd.DataFrame:
    return read_frame(
        """SELECT a.*, m.name_zh AS map_name, m.unlock_level
        FROM map_assets a LEFT JOIN maps m ON m.map_id=a.map_id
        ORDER BY m.unlock_level, m.name_zh"""
    )


def recent_catches(limit: int = 30000) -> pd.DataFrame:
    frame = read_frame(
        """SELECT c.*, m.name_zh AS map_name, m.unlock_level
        FROM catch_records c LEFT JOIN maps m ON m.map_id=c.map_id
        ORDER BY COALESCE(c.caught_at,c.collected_at) DESC LIMIT ?""",
        (limit,),
    )
    if not frame.empty:
        frame["fish_name"] = frame.apply(
            lambda row: FISH_ZH.get(row["fish_id"], _chinese_or_pending(row["fish_name_en"], "鱼种")), axis=1
        )
        frame["rig"] = frame["rig"].map(lambda value: _chinese_or_pending(value, "钓组"))
        frame["baits"] = frame["baits"].map(lambda value: _localized_list(value, "饵料"))
    return frame


def recent_community_spots() -> pd.DataFrame:
    frame = read_frame(
        """SELECT c.*, m.name_zh AS map_name FROM community_spots c
        LEFT JOIN maps m ON m.map_id=c.map_id
        WHERE c.validation_status='有效' AND c.map_id IS NOT NULL"""
    )
    if frame.empty:
        return frame
    collected = pd.to_datetime(frame["collected_at"], errors="coerce", utc=True)
    reported = pd.to_datetime(frame["reported_at"], errors="coerce", utc=True)
    rf4map_mask = frame["source_name"].eq("RF4MAP")
    rf4map_latest = collected[rf4map_mask].max()
    keep = pd.Series(False, index=frame.index)
    if pd.notna(rf4map_latest):
        keep |= rf4map_mask & collected.eq(rf4map_latest)
    for source in ("RF4-STAT", "RF4-STAT 帖子"):
        source_mask = frame["source_name"].eq(source)
        latest = reported[source_mask].max()
        if pd.notna(latest):
            keep |= source_mask & reported.gt(latest - timedelta(hours=48))
    return frame[keep].copy()


def community_evidence_table() -> pd.DataFrame:
    frame = recent_community_spots()
    columns = [
        "map_id", "target_fish", "community_records", "community_points",
        "community_sources", "community_latest", "community_coordinates", "community_baits", "community_trophy_records",
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    frame["evidence_time"] = pd.to_datetime(frame["reported_at"], errors="coerce", utc=True).fillna(
        pd.to_datetime(frame["collected_at"], errors="coerce", utc=True)
    )
    rows = []
    for (map_id, fish_name), group in frame.groupby(["map_id", "fish_name"]):
        coordinates = _dedupe_text(group["coordinate"])
        spot_labels = group.get("spot_label", pd.Series(index=group.index, dtype="object"))
        point_keys = group["coordinate"].fillna(spot_labels).dropna().astype(str)
        baits = _dedupe_text(group["baits"])
        rows.append({
            "map_id": map_id,
            "target_fish": fish_name,
            "community_records": int(group["source_key"].nunique()),
            "community_trophy_records": int(group.loc[group['source_name'].eq('RF4-STAT') & group['quality_label'].eq('奖杯标记'), 'source_key'].nunique()),
            "community_points": int(point_keys.nunique()),
            "community_sources": " / ".join(sorted(group["source_name"].dropna().unique())),
            "community_latest": group["evidence_time"].max().isoformat(),
            "community_coordinates": " / ".join(_dedupe_text(point_keys)[:4]),
            "community_baits": " / ".join(baits[:4]),
        })
    return pd.DataFrame(rows, columns=columns)


def map_community_evidence_table() -> pd.DataFrame:
    """Count one source post once at map level even if it contains multiple fish."""
    frame = recent_community_spots()
    rows = []
    for map_id, group in frame.groupby("map_id"):
        times = pd.to_datetime(group["reported_at"], errors="coerce", utc=True).fillna(
            pd.to_datetime(group["collected_at"], errors="coerce", utc=True)
        )
        rows.append({
            "map_id": map_id,
            "community_records": len(group.drop_duplicates(["source_name", "source_record_id"])),
            "community_points": group["coordinate"].fillna(
                group.get("spot_label", pd.Series(index=group.index, dtype="object"))
            ).nunique(),
            "community_sources": " / ".join(sorted(group["source_name"].unique())),
            "community_latest": times.max().isoformat(),
        })
    return pd.DataFrame(rows)


def _dedupe_text(series: pd.Series) -> list[str]:
    values: list[str] = []
    for value in series.dropna().astype(str):
        for part in re.split(r"\s*/\s*", value):
            part = part.strip()
            if part and part not in values:
                values.append(part)
    return values


def community_spot_details(map_id: str, fish_name: str = "") -> pd.DataFrame:
    frame = recent_community_spots()
    if frame.empty:
        return frame
    frame = frame[frame["map_id"] == map_id].copy()
    if fish_name:
        frame = frame[frame["fish_name"] == fish_name]
    if frame.empty:
        return frame
    return frame.rename(columns={
        "source_name": "来源", "fish_name": "鱼种", "coordinate": "坐标", "spot_label": "坑位", "baits": "饵料",
        "clip": "卡线", "depth": "深度", "quality_label": "证据类型", "catch_count": "渔获数",
        "reported_at": "发布时间", "collected_at": "采集时间", "source_url": "来源链接",
    })[["来源", "鱼种", "坐标", "坑位", "饵料", "卡线", "深度", "证据类型", "渔获数", "发布时间", "采集时间", "来源链接"]]


def historical_catches(map_id: str, fish_id: str = "", limit: int = 250000) -> pd.DataFrame:
    """为习性观察读取指定地图/鱼种的长期本地样本，避免被实时列表上限截断。"""
    params: tuple = (map_id,)
    fish_sql = ""
    if fish_id:
        fish_sql = " AND c.fish_id=?"
        params += (fish_id,)
    params += (limit,)
    frame = read_frame(
        f"""SELECT c.*, m.name_zh AS map_name, m.unlock_level
        FROM catch_records c LEFT JOIN maps m ON m.map_id=c.map_id
        WHERE c.map_id=?{fish_sql}
        ORDER BY COALESCE(c.caught_at,c.collected_at) DESC LIMIT ?""",
        params,
    )
    if not frame.empty:
        frame["fish_name"] = frame.apply(
            lambda row: FISH_ZH.get(row["fish_id"], _chinese_or_pending(row["fish_name_en"], "鱼种")), axis=1
        )
        frame["rig"] = frame["rig"].map(lambda value: _chinese_or_pending(value, "钓组"))
        frame["baits"] = frame["baits"].map(lambda value: _localized_list(value, "饵料"))
    return frame


def _chinese_or_pending(value: str | None, category: str) -> str:
    text = str(value or "").strip()
    if not text:
        return f"未收录中文{category}"
    if re.search(r"[\u4e00-\u9fff]", text):
        return text
    return f"未收录中文{category}"


def _localized_list(value: str | None, category: str) -> str:
    parts = [part.strip() for part in re.split(r"\s*/\s*", str(value or "")) if part.strip()]
    localized = [
        part for part in parts
        if re.search(r"[\u4e00-\u9fff]", part) or not re.search(r"[\u0400-\u04ff]", part)
    ]
    return " / ".join(localized) if localized else f"未收录中文{category}"


def point_group(map_id: str, coordinate: str | None) -> str | None:
    """普通地图保留坐标；挪威海暂用明确标注的100x100工具聚类网格。"""
    if not coordinate:
        return None
    text = str(coordinate).strip()
    match = re.fullmatch(r"(\d{1,4}):(\d{1,4})", text)
    if not match:
        return None
    if map_id != "level_012_north_sea":
        return text
    x, y = (int(part) for part in match.groups())
    return f"工具网格 X{x // 100 + 1}-Y{y // 100 + 1}"


def earnings_point_group(map_id: str, coordinates: str | None) -> str | None:
    parsed = [
        (int(x), int(y))
        for x, y in re.findall(r"\b(\d{1,4}):(\d{1,4})\b", str(coordinates or ""))
    ]
    if not parsed:
        return None
    if map_id == "level_012_north_sea":
        groups = {point_group(map_id, f"{x}:{y}") for x, y in parsed}
        if len(groups) == 1:
            return next(iter(groups))
        center_x = round(sum(x for x, _ in parsed) / len(parsed))
        center_y = round(sum(y for _, y in parsed) / len(parsed))
        return f"拖钓路线（中心 {center_x}:{center_y}）"
    if len(parsed) == 1:
        return f"{parsed[0][0]}:{parsed[0][1]}"
    xs = [item[0] for item in parsed]
    ys = [item[1] for item in parsed]
    if max(xs) - min(xs) <= 10 and max(ys) - min(ys) <= 10:
        return f"区域 {round(sum(xs) / len(xs))}:{round(sum(ys) / len(ys))}"
    center_x = round(sum(xs) / len(xs))
    center_y = round(sum(ys) / len(ys))
    return f"拖钓路线（中心 {center_x}:{center_y}）"


def filter_catches_by_fish(frame: pd.DataFrame, fish_keyword: str) -> pd.DataFrame:
    if frame.empty or not fish_keyword.strip():
        return frame.copy()
    key = fish_keyword.strip()
    exact = (
        frame["fish_name"].str.casefold().eq(key.casefold())
        | frame["fish_name_en"].str.casefold().eq(key.casefold())
        | frame["fish_id"].str.casefold().eq(key.casefold())
    )
    if exact.any():
        return frame[exact].copy()
    return frame[
        frame["fish_name"].str.contains(key, case=False, na=False)
        | frame["fish_name_en"].str.contains(key, case=False, na=False)
        | frame["fish_id"].str.contains(key, case=False, na=False)
    ].copy()


def filter_catches_by_selected_fish(frame: pd.DataFrame, fish_name: str) -> pd.DataFrame:
    if frame.empty or not fish_name.strip():
        return frame.copy()
    return frame[frame["fish_name"].str.casefold().eq(fish_name.strip().casefold())].copy()


def _normalized_caught_times(frame: pd.DataFrame) -> pd.Series:
    """统一普通流的北京时间文本与星鱼流的带时区 ISO 时间。"""
    values = frame.get("caught_at", pd.Series(index=frame.index, dtype="object")).astype("string")
    aware_mask = values.str.contains(r"T|Z|[+-]\d\d:\d\d$", regex=True, na=False)
    result = pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns, UTC]")
    if aware_mask.any():
        result.loc[aware_mask] = pd.to_datetime(values.loc[aware_mask], errors="coerce", utc=True)
    if (~aware_mask).any():
        naive = pd.to_datetime(values.loc[~aware_mask], errors="coerce")
        result.loc[~aware_mask] = naive.dt.tz_localize(
            "Asia/Shanghai", ambiguous="NaT", nonexistent="NaT"
        ).dt.tz_convert("UTC")
    return result


def fish_activity_table(catches: pd.DataFrame) -> pd.DataFrame:
    """用普通 catch-records 流比较最近1小时与此前3小时的地图+鱼种活跃度。"""
    columns = [
        "map_id", "fish_id", "target_fish", "current_hour_catches", "previous_3h_catches",
        "baseline_per_hour", "activity_ratio", "activity_status", "newly_active",
        "activity_score", "activity_anchor",
    ]
    if catches.empty or "fish_grade" not in catches:
        return pd.DataFrame(columns=columns)
    normal = catches[catches["fish_grade"] == "normal"].copy()
    if normal.empty:
        return pd.DataFrame(columns=columns)
    normal["caught_time"] = _normalized_caught_times(normal)
    normal = normal[normal["caught_time"].notna()]
    if normal.empty:
        return pd.DataFrame(columns=columns)
    anchor = normal["caught_time"].max()
    recent_start = anchor - timedelta(hours=1)
    baseline_start = recent_start - timedelta(hours=3)
    baseline_covered = normal["caught_time"].min() <= recent_start - timedelta(minutes=150)
    normal = normal[normal["caught_time"] > baseline_start]
    rows: list[dict] = []
    for (map_id, fish_id, fish_name), group in normal.groupby(["map_id", "fish_id", "fish_name"]):
        current_count = int((group["caught_time"] > recent_start).sum())
        baseline_count = int(((group["caught_time"] > baseline_start) & (group["caught_time"] <= recent_start)).sum())
        baseline_hourly = baseline_count / 3
        ratio = (current_count + 1) / (baseline_hourly + 1)
        if not baseline_covered:
            status = "当前活跃（基线积累中）" if current_count >= 5 else "基线积累中"
            newly_active = False
        elif current_count >= 3 and (baseline_count == 0 or ratio >= 2.5):
            status = "新开始活跃"
            newly_active = True
        elif current_count >= 5 and ratio >= 0.7:
            status = "持续活跃"
            newly_active = False
        elif current_count >= 3 and ratio >= 1.5:
            status = "活跃升温"
            newly_active = False
        elif baseline_count >= 6 and ratio < 0.5:
            status = "活跃回落"
            newly_active = False
        else:
            status = "样本不足"
            newly_active = False
        activity_score = min(current_count, 20) * 2.0 + min(ratio, 4.0) * 3.0
        if status == "活跃回落":
            activity_score *= 0.4
        rows.append(
            {
                "map_id": map_id,
                "fish_id": fish_id,
                "target_fish": fish_name,
                "current_hour_catches": current_count,
                "previous_3h_catches": baseline_count,
                "baseline_per_hour": round(baseline_hourly, 2),
                "activity_ratio": round(ratio, 2),
                "activity_status": status,
                "newly_active": newly_active,
                "activity_score": round(activity_score, 1),
                "activity_anchor": anchor.isoformat(),
            }
        )
    return pd.DataFrame(rows, columns=columns)


RECENT_WINDOWS = (1, 3, 6, 12, 24, 48)
RECENT_WINDOW_WEIGHTS = {1: 0.35, 3: 0.20, 6: 0.15, 12: 0.12, 24: 0.10, 48: 0.08}
RF4STAT_TROPHY_WINDOWS = (3, 6, 12, 24, 48, 72)
RF4STAT_TROPHY_WINDOW_WEIGHTS = {3: 0.35, 6: 0.22, 12: 0.16, 24: 0.12, 48: 0.09, 72: 0.06}


def rf4stat_trophy_update_activity(now=None) -> pd.DataFrame:
    """按RF4-STAT逐条上报时间统计本周上星近期更新，不用周榜快照时间代替。"""
    count_columns = [f"rf4stat_trophies_{hours}h" for hours in RF4STAT_TROPHY_WINDOWS]
    columns = ["map_id", "fish_id", "target_fish", *count_columns,
               "rf4stat_trophy_hourly_rate", "rf4stat_trophy_score", "rf4stat_latest_trophy"]
    frame = read_frame(
        """SELECT source_key,map_id,fish_id,fish_name,reported_at
           FROM community_spots
           WHERE source_name='RF4-STAT' AND quality_label='奖杯标记'
           AND validation_status='有效' AND map_id IS NOT NULL AND fish_id IS NOT NULL
           AND reported_at IS NOT NULL"""
    )
    if frame.empty:
        return pd.DataFrame(columns=columns)
    frame = frame.drop_duplicates("source_key").copy()
    frame["reported_time"] = pd.to_datetime(
        frame["reported_at"], format="mixed", errors="coerce", utc=True
    )
    anchor = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    anchor = anchor.tz_localize("UTC") if anchor.tzinfo is None else anchor.tz_convert("UTC")
    frame = frame[
        frame["reported_time"].notna()
        & frame["reported_time"].le(anchor + timedelta(minutes=5))
        & frame["reported_time"].gt(anchor - timedelta(hours=max(RF4STAT_TROPHY_WINDOWS)))
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for (map_id, fish_id), group in frame.groupby(["map_id", "fish_id"]):
        row = {
            "map_id": map_id,
            "fish_id": fish_id,
            "target_fish": FISH_ZH.get(
                fish_id,
                group["fish_name"].dropna().astype(str).iloc[0]
                if group["fish_name"].notna().any() else f"未收录中文鱼种（{fish_id}）",
            ),
            "rf4stat_latest_trophy": group["reported_time"].max().isoformat(),
        }
        weighted_hourly_rate = 0.0
        for hours in RF4STAT_TROPHY_WINDOWS:
            count = int((group["reported_time"] > anchor - timedelta(hours=hours)).sum())
            row[f"rf4stat_trophies_{hours}h"] = count
            weighted_hourly_rate += count / hours * RF4STAT_TROPHY_WINDOW_WEIGHTS[hours]
        row["rf4stat_trophy_hourly_rate"] = round(weighted_hourly_rate, 4)
        row["rf4stat_trophy_score"] = round(
            65 * weighted_hourly_rate / (weighted_hourly_rate + 0.35), 2
        ) if weighted_hourly_rate > 0 else 0.0
        rows.append(row)
    return pd.DataFrame(rows, columns=columns)


def map_temperature_baselines() -> pd.DataFrame:
    """按地图+预报时点去重后的预报参考温度中位数定义地图常温基线。"""
    columns = ["map_id", "normal_forecast_temperature", "temperature_baseline_samples"]
    frame = read_frame(
        """SELECT map_id,forecast_slot,forecast_day_temperature,collected_at
           FROM weather_forecasts WHERE forecast_day_temperature IS NOT NULL"""
    )
    if frame.empty:
        return pd.DataFrame(columns=columns)
    frame["collected_time"] = pd.to_datetime(
        frame["collected_at"], format="mixed", errors="coerce", utc=True
    )
    frame["forecast_day_temperature"] = pd.to_numeric(
        frame["forecast_day_temperature"], errors="coerce"
    )
    frame = frame.dropna(subset=["map_id", "forecast_slot", "forecast_day_temperature"])
    frame = frame.sort_values("collected_time").drop_duplicates(
        ["map_id", "forecast_slot"], keep="last"
    )
    if frame.empty:
        return pd.DataFrame(columns=columns)
    return frame.groupby("map_id", as_index=False).agg(
        normal_forecast_temperature=("forecast_day_temperature", "median"),
        temperature_baseline_samples=("forecast_slot", "nunique"),
    )


def recent_window_evidence(catches: pd.DataFrame, by_fish: bool = True) -> pd.DataFrame:
    """统计1/3/6/12/24/48小时普通渔获、星鱼及近期加权活跃度。"""
    keys = ["map_id", "fish_id"] if by_fish else ["map_id"]
    count_columns = [f"catches_{hours}h" for hours in RECENT_WINDOWS]
    star_columns = [f"stars_{hours}h" for hours in RECENT_WINDOWS]
    columns = keys + count_columns + star_columns + ["recent_activity_score", "top3_baits"]
    if catches.empty:
        return pd.DataFrame(columns=columns)
    frame = catches.copy()
    frame["caught_time"] = _normalized_caught_times(frame)
    frame = frame[frame["caught_time"].notna()]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    anchor = frame["caught_time"].max()
    frame = frame[frame["caught_time"] > anchor - timedelta(hours=max(RECENT_WINDOWS))]
    rows: list[dict] = []
    group_key = keys[0] if len(keys) == 1 else keys
    for group_values, group in frame.groupby(group_key):
        values = (group_values,) if len(keys) == 1 else group_values
        normal = group[group["fish_grade"] == "normal"]
        six_hour_normal = normal[normal["caught_time"] > anchor - timedelta(hours=6)]
        bait_counts: Counter[str] = Counter()
        for bait_list in six_hour_normal.get("baits", pd.Series(dtype="object")).dropna().astype(str):
            for bait in re.split(r"\s*/\s*", bait_list):
                bait = bait.strip()
                if bait and not any(marker in bait for marker in ("待补充", "待翻译", "未知")):
                    bait_counts[bait] += 1
        row = dict(zip(keys, values))
        weighted_hourly_rate = 0.0
        for hours in RECENT_WINDOWS:
            window = group[group["caught_time"] > anchor - timedelta(hours=hours)]
            normal_count = int((window["fish_grade"] == "normal").sum())
            row[f"catches_{hours}h"] = normal_count
            row[f"stars_{hours}h"] = int((window["fish_grade"] != "normal").sum())
            weighted_hourly_rate += normal_count / hours * RECENT_WINDOW_WEIGHTS[hours]
        row["recent_activity_score"] = round(weighted_hourly_rate, 2)
        row["top3_baits"] = "、".join(name for name, _ in bait_counts.most_common(3)) or "暂无公开饵料"
        rows.append(row)
    return pd.DataFrame(rows, columns=columns)


def recent_six_hour_evidence(catches: pd.DataFrame, by_fish: bool = True) -> pd.DataFrame:
    """兼容旧调用：返回旧版6小时字段。"""
    frame = recent_window_evidence(catches, by_fish=by_fish)
    keys = ["map_id", "fish_id"] if by_fish else ["map_id"]
    if frame.empty:
        return pd.DataFrame(columns=keys + ["six_hour_catches", "six_hour_stars", "top3_baits"])
    return frame.rename(columns={"catches_6h": "six_hour_catches", "stars_6h": "six_hour_stars"})[
        keys + ["six_hour_catches", "six_hour_stars", "top3_baits"]
    ]


def _timed_recent_evidence(evidence: pd.DataFrame, *, days: int | None = None, now=None) -> pd.DataFrame:
    if evidence.empty:
        return evidence.copy()
    frame = evidence.copy()
    frame["evidence_time"] = pd.to_datetime(
        frame["caught_at"].fillna(frame["reported_at"]), format="mixed", errors="coerce", utc=True
    )
    anchor = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    anchor = anchor.tz_localize("UTC") if anchor.tzinfo is None else anchor.tz_convert("UTC")
    eligible = frame["evidence_time"].notna() & frame["evidence_time"].le(anchor + timedelta(minutes=5))
    if days is not None:
        eligible &= frame["evidence_time"].gt(anchor - timedelta(days=days))
    return frame[eligible].copy()


def build_combined_window_summary(evidence: pd.DataFrame, now=None) -> pd.DataFrame:
    """跨来源合并近期普通/帖子与明确上星样本；周榜不进入小时窗口。"""
    columns = ["时间窗口", "普通/帖子样本", "上星样本", "合计样本", "来源构成"]
    frame = _timed_recent_evidence(evidence, now=now)
    if frame.empty:
        return pd.DataFrame(columns=columns)
    frame = frame[frame["fish_grade"].ne("weekly_record")].drop_duplicates("source_record_key")
    anchor = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    anchor = anchor.tz_localize("UTC") if anchor.tzinfo is None else anchor.tz_convert("UTC")
    rows = []
    for hours in RECENT_WINDOWS:
        group = frame[frame["evidence_time"] > anchor - timedelta(hours=hours)]
        stars = int(group["fish_grade"].eq("star").sum())
        ordinary = int(len(group) - stars)
        rows.append({
            "时间窗口": f"{hours}小时",
            "普通/帖子样本": ordinary,
            "上星样本": stars,
            "合计样本": len(group),
            "来源构成": source_breakdown(group),
        })
    return pd.DataFrame(rows, columns=columns)


def _clean_combination(value) -> str:
    if value is None or (not isinstance(value, (list, tuple, dict)) and pd.isna(value)):
        return ""
    text = re.sub(r"\s*(?:/|\+)\s*", " + ", str(value or "").strip())
    return re.sub(r"\s+", " ", text).strip()


def _bait_component_count(value) -> int:
    combo = _clean_combination(value)
    return len([part for part in combo.split(" + ") if part.strip()]) if combo else 0


def build_reason_bait_combinations(evidence: pd.DataFrame, now=None, limit: int = 3) -> pd.DataFrame:
    """按完整饵料组合和钓组统计推荐原因中的近期样本 Top N。"""
    columns = ["推荐顺序", "鱼饵组合", "钓组", "组合完整度", "合计样本", "上星样本", "周榜样本", "最近证据时间", "来源构成"]
    frame = _timed_recent_evidence(evidence, days=7, now=now)
    if frame.empty:
        return pd.DataFrame(columns=columns)
    excluded = ("待补充", "待翻译", "未知", "未收录")
    frame["bait_combo"] = frame["baits"].map(_clean_combination)
    frame["rig_combo"] = frame["rig"].map(_clean_combination)
    frame["complete_combo"] = frame["bait_combo"].map(_bait_component_count).ge(2)
    frame = frame[
        frame["bait_combo"].ne("")
        & ~frame["bait_combo"].map(lambda value: any(marker in value for marker in excluded))
    ].drop_duplicates(["source_record_key", "bait_combo", "rig_combo"])
    if frame.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for (bait, rig), group in frame.groupby(["bait_combo", "rig_combo"], dropna=False, sort=False):
        rows.append({
            "鱼饵组合": bait,
            "钓组": rig or "未提供",
            "组合完整度": "完整组合" if bool(group["complete_combo"].any()) else "来源单项",
            "合计样本": int(group["source_record_key"].nunique()),
            "上星样本": int(group.loc[group["fish_grade"].eq("star"), "source_record_key"].nunique()),
            "周榜样本": int(group.loc[group["fish_grade"].eq("weekly_record"), "source_record_key"].nunique()),
            "最近证据时间": group["evidence_time"].max().isoformat(),
            "来源构成": source_breakdown(group),
        })
    result = pd.DataFrame(rows)
    result["_完整组合"] = result["组合完整度"].eq("完整组合")
    result = result.sort_values(
        ["_完整组合", "合计样本", "最近证据时间", "鱼饵组合"], ascending=[False, False, False, True]
    ).head(max(1, limit)).reset_index(drop=True)
    result.insert(0, "推荐顺序", result.index + 1)
    return result[columns]


def build_bait_recommendations(
    evidence: pd.DataFrame,
    fish_names: list[str] | None = None,
    now=None,
    per_fish: int = 3,
) -> pd.DataFrame:
    """按最近7天完整组合优先，汇总上星、周榜和帖子鱼饵观察。"""
    columns = [
        "鱼种", "推荐顺序", "鱼饵组合", "钓组", "组合完整度", "近期上星记录", "近期周榜记录", "近期帖子记录",
        "合计来源记录", "来源构成", "最近证据时间", "观察状态",
    ]
    if evidence.empty or "baits" not in evidence:
        return pd.DataFrame(columns=columns)
    frame = evidence.copy()
    trophy_mask = (
        frame["fish_grade"].eq("star")
        & frame["source_name"].isin(["RF4DB渔获", "RF4-STAT上鱼记录"])
    )
    post_mask = frame["source_name"].eq("RF4-STAT 帖子")
    weekly_mask = frame["fish_grade"].eq("weekly_record")
    frame = frame[trophy_mask | post_mask | weekly_mask].copy()
    if fish_names:
        frame = frame[frame["fish_name"].isin(fish_names)]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    frame = _timed_recent_evidence(frame, days=7, now=now)
    entries: list[dict] = []
    excluded_markers = ("待补充", "待翻译", "未知", "未收录")
    for row in frame.itertuples():
        bait = _clean_combination(row.baits)
        rig = _clean_combination(row.rig)
        if bait and not any(marker in bait for marker in excluded_markers):
            entries.append({
                "fish_name": row.fish_name,
                "bait": bait,
                "rig": rig,
                "source_record_key": row.source_record_key,
                "source_name": row.source_name,
                "is_trophy": row.source_name in {"RF4DB渔获", "RF4-STAT上鱼记录"},
                "is_weekly": row.fish_grade == "weekly_record",
                "complete_combo": _bait_component_count(bait) >= 2,
                "evidence_time": row.evidence_time,
            })
    if not entries:
        return pd.DataFrame(columns=columns)
    expanded = pd.DataFrame(entries).drop_duplicates(["source_record_key", "bait", "rig"])
    rows: list[dict] = []
    for (fish_name, bait, rig), group in expanded.groupby(["fish_name", "bait", "rig"], dropna=False, sort=False):
        trophy_count = int(group.loc[group["is_trophy"], "source_record_key"].nunique())
        weekly_count = int(group.loc[group["is_weekly"], "source_record_key"].nunique())
        post_count = int(group.loc[~group["is_trophy"] & ~group["is_weekly"], "source_record_key"].nunique())
        total = int(group["source_record_key"].nunique())
        source_count = group["source_name"].nunique()
        status = (
            "跨源观察较强" if total >= 5 and source_count >= 2
            else "近期观察" if total >= 3
            else "低样本观察"
        )
        rows.append({
            "鱼种": fish_name,
            "鱼饵组合": bait,
            "钓组": rig or "未提供",
            "组合完整度": "完整组合" if bool(group["complete_combo"].any()) else "来源单项",
            "近期上星记录": trophy_count,
            "近期周榜记录": weekly_count,
            "近期帖子记录": post_count,
            "合计来源记录": total,
            "来源构成": source_breakdown(group),
            "最近证据时间": group["evidence_time"].max().isoformat(),
            "观察状态": status,
        })
    result = pd.DataFrame(rows)
    result["_完整组合"] = result["组合完整度"].eq("完整组合")
    result = result.sort_values(
        ["鱼种", "_完整组合", "近期上星记录", "近期周榜记录", "近期帖子记录", "最近证据时间", "鱼饵组合"],
        ascending=[True, False, False, False, False, False, True],
    )
    result["推荐顺序"] = result.groupby("鱼种").cumcount() + 1
    result = result[result["推荐顺序"] <= per_fish]
    return result[columns].reset_index(drop=True)


def build_recommendation_detail(
    catches: pd.DataFrame,
    map_id: str,
    fish_keyword: str = "",
    selected_point: str = "",
    earnings: pd.DataFrame | None = None,
) -> dict[str, pd.DataFrame | int]:
    """按字段汇集全部本地公开来源，不把出现频次解释为已确认喜好。"""
    earnings_species = build_earnings_species_mix(earnings, map_id, selected_point) if selected_point else pd.DataFrame()
    map_catches = all_source_habit_records(catches, map_id)
    target_catches = filter_catches_by_fish(map_catches, fish_keyword)
    bait_fish_names = None
    if not fish_keyword.strip() and not earnings_species.empty:
        bait_fish_names = earnings_species["鱼种"].dropna().astype(str).head(5).tolist()
    bait_recommendations = build_bait_recommendations(target_catches, bait_fish_names, per_fish=8)
    if target_catches.empty:
        empty = pd.DataFrame()
        return {
            "sample_count": 0, "condition_sample_count": 0, "weather_sample_count": 0,
            "game_time_sample_count": 0, "temperature_sample_count": 0,
            "source_breakdown": "暂无", "point_count": 0, "preferences": empty,
            "points": empty, "catches": empty, "point_species": empty,
            "earnings_species": earnings_species, "bait_recommendations": bait_recommendations,
            "window_summary": empty, "reason_baits": empty, "reason_points": empty,
            "reason_scope": "候选点位公开证据" if selected_point else "同地图目标鱼种公开证据",
        }

    point_target_catches = target_catches.copy()
    if selected_point:
        point_target_catches["point_group"] = point_target_catches["coordinate"].map(
            lambda value: point_group(map_id, value)
        )
        point_target_catches = point_target_catches[
            point_target_catches["point_group"] == selected_point
        ].copy()

    point_species = (
        point_target_catches.groupby(["fish_id", "fish_name"], as_index=False)
        .agg(样本数=("fish_id", "size"))
        .sort_values("样本数", ascending=False)
    )
    if not point_species.empty:
        point_species["样本占比"] = (
            point_species["样本数"] / point_species["样本数"].sum() * 100
        ).round(1)
        point_species = point_species.rename(columns={"fish_name": "鱼种"})[
            ["鱼种", "样本数", "样本占比"]
        ]

    valid_map_weather = map_catches[
        map_catches["weather_code"].notna()
        & ~map_catches["weather_code"].isin(["unknown", ""])
    ].copy()
    valid_target_weather = target_catches[
        target_catches["weather_code"].notna()
        & ~target_catches["weather_code"].isin(["unknown", ""])
    ].copy()
    target_temperatures = pd.to_numeric(target_catches["catch_temperature"], errors="coerce")
    valid_target_temperature = target_catches[target_temperatures.notna()].copy()
    valid_target_time = target_catches[
        target_catches["game_time"].astype("string").str.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", na=False)
    ].copy()
    valid_target_conditions = valid_target_weather[
        pd.to_numeric(valid_target_weather["catch_temperature"], errors="coerce").notna()
    ]
    preference_rows: list[dict] = []
    background_weather = valid_map_weather["weather_code"].value_counts(normalize=True)
    fish_order = target_catches["fish_name"].value_counts().index[:3]
    for fish_name in fish_order:
        group = target_catches[target_catches["fish_name"] == fish_name]
        weather_group = valid_target_weather[valid_target_weather["fish_name"] == fish_name]
        temperature_group = valid_target_temperature[valid_target_temperature["fish_name"] == fish_name]
        time_group = valid_target_time[valid_target_time["fish_name"] == fish_name]
        weather_counts = weather_group["weather_code"].value_counts()
        top_weather_code = weather_counts.index[0] if not weather_counts.empty else None
        temperatures = pd.to_numeric(temperature_group["catch_temperature"], errors="coerce").dropna()
        temperature_range = (
            f"{temperatures.quantile(0.25):.1f}～{temperatures.quantile(0.75):.1f}°C"
            if not temperatures.empty else "暂无即时温度样本"
        )
        top_share = float(weather_counts.iloc[0] / weather_counts.sum()) if not weather_counts.empty else 0.0
        background_share = float(background_weather.get(top_weather_code, 0)) if top_weather_code else 0.0
        relative_index = top_share / background_share if background_share > 0 else None
        caught_times = pd.to_datetime(group["caught_at"], format="mixed", errors="coerce", utc=True).dropna()
        coverage_days = (
            max(1, int((caught_times.max() - caught_times.min()).total_seconds() / 86400) + 1)
            if len(caught_times) >= 2 else 0
        )
        weather_status = (
            "达到天气观察门槛（仍需用户日暴露验证）"
            if coverage_days >= 15 and weather_counts.sum() >= 25 and relative_index is not None
            else "有效天气样本不足"
        )
        temperature_status = (
            "达到温度观察门槛（仍需温度校准）"
            if coverage_days >= 15 and len(temperatures) >= 25
            else "即时温度样本不足"
        )
        if weather_status.startswith("达到") and temperature_status.startswith("达到"):
            conclusion = "达到条件观察门槛（仍需用户日暴露验证）"
        else:
            conclusion = f"{weather_status}；{temperature_status}"
        preference_rows.append(
            {
                "鱼种": fish_name,
                "公开渔获样本": len(group),
                "公开证据记录": len(group),
                "来源构成": source_breakdown(group),
                "出现最多天气": WEATHER_ZH.get(top_weather_code, top_weather_code) if top_weather_code else "暂无有效天气样本",
                "weather_code": top_weather_code,
                "该天气样本": int(weather_counts.iloc[0]) if not weather_counts.empty else 0,
                "有效天气样本": int(weather_counts.sum()),
                "相对地图背景指数": round(relative_index, 2) if relative_index is not None else None,
                "即时温度常见范围（P25-P75）": temperature_range,
                "温度样本": len(temperatures),
                "游戏时间样本": len(time_group),
                "历史覆盖天数": coverage_days,
                "天气结论": weather_status,
                "温度结论": temperature_status,
                "结论状态": conclusion,
            }
        )
    preferences = pd.DataFrame(preference_rows)

    spot_labels = point_target_catches.get(
        "spot_label", pd.Series(index=point_target_catches.index, dtype="object")
    )
    point_target_catches["point_key"] = point_target_catches["coordinate"].fillna(spot_labels)
    valid_points = point_target_catches[
        point_target_catches["point_key"].notna()
        & ~point_target_catches["point_key"].astype(str).str.strip().isin(["", "��", "—", "-"])
    ].copy()
    point_rows: list[dict] = []
    for point_key, group in valid_points.groupby("point_key", sort=False):
        weather_counts = group.loc[~group["weather_code"].isin(["unknown", ""]), "weather_code"].dropna().value_counts()
        top_weather = weather_counts.index[0] if not weather_counts.empty else "unknown"
        temperatures = pd.to_numeric(group["catch_temperature"], errors="coerce").dropna()
        coordinates = group["coordinate"].dropna().astype(str)
        coordinate = coordinates.iloc[0] if not coordinates.empty else ""
        point_basis = "公开坐标" if coordinate else "RF4-STAT坑位"
        point_rows.append(
            {
                "点位": point_key,
                "坐标": coordinate,
                "点位口径": point_basis,
                "卡线": " / ".join(group["clip"].dropna().astype(str).replace("��", "").loc[lambda s: s != ""].unique()[:3]),
                "鱼种": " / ".join(group["fish_name"].dropna().astype(str).unique()[:3]),
                "样本": len(group),
                "游戏时间": " / ".join(group["game_time"].dropna().astype(str).unique()[:4]),
                "样本天气": WEATHER_ZH.get(top_weather, top_weather),
                "即时温度": f"{temperatures.min():.1f}～{temperatures.max():.1f}°C" if not temperatures.empty else "暂无",
                "钓组": " / ".join(group["rig"].dropna().astype(str).unique()[:3]),
                "饵料": " / ".join(group["baits"].dropna().astype(str).unique()[:3]),
                "最近捕获": group["caught_at"].dropna().astype(str).max() if group["caught_at"].notna().any() else "",
                "来源构成": source_breakdown(group),
                "来源": group["source_url"].dropna().astype(str).iloc[0] if group["source_url"].notna().any() else "",
            }
        )
    points = pd.DataFrame(point_rows)
    if not points.empty:
        points = points.sort_values(["样本", "最近捕获"], ascending=[False, False])

    reason_evidence = point_target_catches if selected_point else target_catches
    window_summary = build_combined_window_summary(reason_evidence)
    reason_baits = build_reason_bait_combinations(reason_evidence, limit=3)
    reason_points = points.head(3).copy() if not points.empty else pd.DataFrame()

    catch_view = target_catches.copy()
    catch_view["天气"] = catch_view["weather_code"].map(WEATHER_ZH).fillna(catch_view["weather_code"])
    catch_view = catch_view.rename(
        columns={
            "source_name": "数据来源",
            "fish_name": "鱼种",
            "coordinate": "坐标",
            "spot_label": "坑位",
            "clip": "卡线",
            "game_time": "游戏时间",
            "catch_temperature": "即时温度",
            "rig": "钓组",
            "baits": "饵料",
            "fish_grade": "等级",
            "caught_at": "捕获时间",
            "time_basis": "时间口径",
            "weather_basis": "天气口径",
            "source_url": "来源",
        }
    )
    catch_columns = ["数据来源", "鱼种", "坐标", "坑位", "卡线", "游戏时间", "时间口径", "天气", "天气口径", "即时温度", "钓组", "饵料", "等级", "捕获时间", "来源"]
    catch_view = catch_view[[column for column in catch_columns if column in catch_view]].head(30)

    return {
        "sample_count": len(target_catches),
        "condition_sample_count": len(valid_target_conditions),
        "weather_sample_count": len(valid_target_weather),
        "game_time_sample_count": len(valid_target_time),
        "temperature_sample_count": len(valid_target_temperature),
        "source_breakdown": source_breakdown(target_catches),
        "point_count": len(points),
        "preferences": preferences,
        "points": points,
        "catches": catch_view,
        "point_species": point_species,
        "earnings_species": earnings_species,
        "bait_recommendations": bait_recommendations,
        "window_summary": window_summary,
        "reason_baits": reason_baits,
        "reason_points": reason_points,
        "reason_scope": "候选点位公开证据" if selected_point else "同地图目标鱼种公开证据",
    }


def build_earnings_species_mix(
    earnings: pd.DataFrame | None,
    map_id: str,
    selected_point: str,
) -> pd.DataFrame:
    """选择与候选点位一致的最新收益小时样本并展开鱼种构成。"""
    columns = ["鱼种", "尾数", "数量占比", "价值占比", "样本总尾数", "收益参考", "来源"]
    if earnings is None or earnings.empty or not selected_point:
        return pd.DataFrame(columns=columns)
    candidates = earnings[earnings["map_id"] == map_id].copy()
    if candidates.empty or "species_composition" not in candidates:
        return pd.DataFrame(columns=columns)
    candidates["point_group"] = candidates.apply(
        lambda row: earnings_point_group(row["map_id"], row.get("coordinates")), axis=1
    )
    candidates = candidates[
        (candidates["point_group"] == selected_point)
        & candidates["species_composition"].notna()
        & (candidates["species_composition"].astype(str) != "[]")
    ].copy()
    if candidates.empty:
        return pd.DataFrame(columns=columns)
    candidates["exact_point"] = candidates["coordinates"].astype(str).str.strip().eq(selected_point)
    sample = candidates.sort_values(
        ["exact_point", "catch_count", "silver_per_hour"], ascending=[False, False, False]
    ).iloc[0]
    try:
        species = json.loads(sample["species_composition"])
    except (TypeError, json.JSONDecodeError):
        return pd.DataFrame(columns=columns)
    total = sum(int(item.get("count") or 0) for item in species)
    rows = []
    for item in species:
        count = int(item.get("count") or 0)
        fish_id = str(item.get("fish_id") or "")
        fish_name = FISH_ZH.get(fish_id, _chinese_or_pending(item.get("fish_name_en"), "鱼种"))
        rows.append(
            {
                "鱼种": fish_name,
                "尾数": count,
                "数量占比": round(count / total * 100, 1) if total else 0,
                "价值占比": item.get("value_pct"),
                "样本总尾数": total,
                "收益参考": sample.get("silver_per_hour"),
                "来源": sample.get("source_url"),
            }
        )
    return pd.DataFrame(rows, columns=columns).sort_values("尾数", ascending=False)


def select_earnings_sample(
    earnings: pd.DataFrame | None,
    map_id: str,
    selected_point: str = "",
):
    """返回候选点位对应的收益明细；仅需底图时回退到该地图任一样本。"""
    if earnings is None or earnings.empty:
        return None
    candidates = earnings[earnings["map_id"] == map_id].copy()
    if candidates.empty:
        return None
    if selected_point:
        candidates["point_group"] = candidates.apply(
            lambda row: earnings_point_group(row["map_id"], row.get("coordinates")), axis=1
        )
        matched = candidates[candidates["point_group"] == selected_point]
        if not matched.empty:
            candidates = matched
    return candidates.sort_values(["catch_count", "silver_per_hour"], ascending=False).iloc[0]


def select_map_render_sample(
    map_assets: pd.DataFrame | None,
    earnings: pd.DataFrame | None,
    map_id: str,
):
    """Return the map-level render resource, with legacy earnings assets as fallback."""
    if map_assets is not None and not map_assets.empty:
        candidates = map_assets[map_assets["map_id"] == map_id]
        if not candidates.empty:
            sample = candidates.sort_values("collected_at", ascending=False).iloc[0].copy()
            sample["spot_details"] = "[]"
            sample["coordinates"] = ""
            return sample
    return select_earnings_sample(earnings, map_id)


def recommendation_table(
    horizon: int,
    player_level: int,
    objective: str = "综合推荐",
    fish_keyword: str = "",
    blocked_fish: tuple[str, ...] = (),
) -> pd.DataFrame:
    weather = latest_weather()
    if weather.empty:
        return weather
    weather = weather[(weather["horizon"] == horizon) & (weather["unlock_level"] <= player_level)].copy()
    earnings = latest_earnings()
    catches = recent_catches()
    activity = fish_activity_table(catches)
    window_evidence = recent_window_evidence(catches)
    rf4stat_trophy_activity = rf4stat_trophy_update_activity()
    weekly_activity = weekly_fish_activity()
    community = community_evidence_table()
    is_farm = objective in ("搬砖收益", "搬砖（地图+点位）")
    is_trophy = objective in ("蹲星", "蹲星（地图+鱼种）")
    if is_trophy and blocked_fish and not catches.empty:
        catches = catches[~catches["fish_name"].isin(blocked_fish)].copy()
    if objective == "指定鱼种" and fish_keyword.strip() and not catches.empty:
        catches = filter_catches_by_selected_fish(catches, fish_keyword)
    if is_trophy and blocked_fish and not community.empty:
        community = community[~community["target_fish"].isin(blocked_fish)].copy()
    if is_trophy and blocked_fish and not rf4stat_trophy_activity.empty:
        rf4stat_trophy_activity = rf4stat_trophy_activity[
            ~rf4stat_trophy_activity["target_fish"].isin(blocked_fish)
        ].copy()
    if objective == "指定鱼种" and fish_keyword.strip() and not community.empty:
        community = community[
            community["target_fish"].str.casefold().eq(fish_keyword.strip().casefold())
        ].copy()
    if objective == "指定鱼种" and fish_keyword.strip() and not rf4stat_trophy_activity.empty:
        rf4stat_trophy_activity = rf4stat_trophy_activity[
            rf4stat_trophy_activity["target_fish"].str.casefold().eq(fish_keyword.strip().casefold())
        ].copy()

    if is_farm and not earnings.empty:
        point_rows = []
        for _, sample in earnings.iterrows():
            label = earnings_point_group(sample["map_id"], sample.get("coordinates"))
            if not label:
                continue
            try:
                species = json.loads(sample.get("species_composition") or "[]")
            except (TypeError, json.JSONDecodeError):
                species = []
            species_counts = {str(item.get("fish_id") or ""): int(item.get("count") or 0) for item in species}
            dominant_id = max(species_counts, key=species_counts.get) if species_counts else ""
            dominant_item = next((item for item in species if item.get("fish_id") == dominant_id), {})
            point_rows.append(
                {
                    "map_id": sample["map_id"],
                    "point_label": label,
                    "fish_id": dominant_id,
                    "target_fish": FISH_ZH.get(
                        dominant_id, _chinese_or_pending(dominant_item.get("fish_name_en"), "鱼种")
                    ) if dominant_id else "",
                    "recent_catches": int(sample.get("catch_count") or 0),
                    "recent_stars": 0,
                    "last_catch": sample.get("collected_at"),
                    "representative_coordinates": str(sample.get("coordinates") or ""),
                    "point_shared_species": len(species),
                }
            )
        candidates = pd.DataFrame(point_rows)
        if not candidates.empty:
            candidates = candidates.sort_values("recent_catches", ascending=False).drop_duplicates(
                ["map_id", "point_label"], keep="first"
            )
            if not activity.empty:
                map_activity = activity.groupby("map_id", as_index=False).agg(
                    current_hour_catches=("current_hour_catches", "sum"),
                    previous_3h_catches=("previous_3h_catches", "sum"),
                    activity_score=("activity_score", "sum"),
                )
                map_activity["activity_status"] = "地图普通流活跃背景"
                candidates = candidates.merge(map_activity, on="map_id", how="left")
            map_windows = recent_window_evidence(catches, by_fish=False)
            if not map_windows.empty:
                map_windows = map_windows.drop(columns=[f"stars_{hours}h" for hours in RECENT_WINDOWS])
                candidates = candidates.merge(map_windows, on="map_id", how="left")
            for hours in RECENT_WINDOWS:
                candidates[f"stars_{hours}h"] = 0
            candidates["top3_baits"] = "见点位收益详情"
            if not community.empty:
                map_community = map_community_evidence_table()
                candidates = candidates.merge(map_community, on="map_id", how="left")
        weather = weather.merge(candidates, on="map_id", how="inner") if not candidates.empty else weather.iloc[0:0].copy()
    elif is_trophy or objective == "指定鱼种":
        grouped_rows = []
        if not catches.empty:
            for (map_id, fish_id, fish_name), group in catches.groupby(["map_id", "fish_id", "fish_name"]):
                stars = int((group["fish_grade"] != "normal").sum())
                grouped_rows.append(
                    {
                        "map_id": map_id,
                        "fish_id": fish_id,
                        "target_fish": fish_name,
                        "point_label": "",
                        "recent_catches": len(group),
                        "recent_stars": stars,
                        "last_catch": group["caught_at"].max(),
                        "representative_coordinates": " / ".join(group["coordinate"].dropna().astype(str).unique()[:4]),
                    }
                )
        candidates = pd.DataFrame(grouped_rows)
        if not rf4stat_trophy_activity.empty:
            existing_ids = set(zip(candidates.get("map_id", []), candidates.get("fish_id", [])))
            update_rows = []
            for _, evidence in rf4stat_trophy_activity.iterrows():
                key = (evidence["map_id"], evidence["fish_id"])
                if key in existing_ids:
                    continue
                update_rows.append({
                    "map_id": evidence["map_id"],
                    "fish_id": evidence["fish_id"],
                    "target_fish": evidence["target_fish"],
                    "point_label": "",
                    "recent_catches": 0,
                    "recent_stars": 0,
                    "last_catch": evidence["rf4stat_latest_trophy"],
                    "representative_coordinates": "",
                })
            if update_rows:
                candidates = pd.concat([candidates, pd.DataFrame(update_rows)], ignore_index=True)
        if not community.empty:
            existing = set(zip(candidates.get("map_id", []), candidates.get("target_fish", [])))
            community_rows = []
            community_raw = recent_community_spots()
            community_only = community[
                (community["community_records"] >= 2) | (community["community_points"] >= 1)
            ]
            for _, evidence in community_only.iterrows():
                key = (evidence["map_id"], evidence["target_fish"])
                if key in existing:
                    continue
                community_rows.append({
                    "map_id": evidence["map_id"],
                    "fish_id": next((str(r['fish_id']) for _, r in community_raw.iterrows()
                                     if r['map_id'] == evidence['map_id'] and r['fish_name'] == evidence['target_fish']),
                                    f"community:{hashlib.sha1(evidence['target_fish'].encode('utf-8')).hexdigest()[:12]}"),
                    "target_fish": evidence["target_fish"],
                    "point_label": "",
                    "recent_catches": 0,
                    "recent_stars": 0,
                    "last_catch": evidence["community_latest"],
                    "representative_coordinates": evidence["community_coordinates"],
                })
            if community_rows:
                candidates = pd.concat([candidates, pd.DataFrame(community_rows)], ignore_index=True)
        if not candidates.empty and not activity.empty:
            candidates = candidates.merge(
                activity.drop(columns=["target_fish"]), on=["map_id", "fish_id"], how="left"
            )
        if not candidates.empty and not window_evidence.empty:
            candidates = candidates.merge(window_evidence, on=["map_id", "fish_id"], how="left")
        if not candidates.empty and not rf4stat_trophy_activity.empty:
            candidates = candidates.merge(
                rf4stat_trophy_activity.drop(columns=["target_fish"]),
                on=["map_id", "fish_id"], how="left",
            )
        if not candidates.empty and not weekly_activity.empty:
            candidates = candidates.merge(weekly_activity.drop(columns=['target_fish']), on=["map_id", "fish_id"], how="left")
        if not candidates.empty and not community.empty:
            candidates = candidates.merge(community, on=["map_id", "target_fish"], how="left")
        weather = weather.merge(candidates, on="map_id", how="inner") if not candidates.empty else weather.iloc[0:0].copy()
    elif not catches.empty:
        catch_stats = catches.groupby("map_id", as_index=False).agg(
            recent_catches=("source_id", "count"),
            recent_stars=("fish_grade", lambda s: int((s != "normal").sum())),
            last_catch=("caught_at", "max"),
        )
        weather = weather.merge(catch_stats, on="map_id", how="left")
        weather["point_label"] = ""
        weather["target_fish"] = ""
        weather["representative_coordinates"] = ""
        if not activity.empty:
            map_activity = activity.groupby("map_id", as_index=False).agg(
                current_hour_catches=("current_hour_catches", "sum"),
                previous_3h_catches=("previous_3h_catches", "sum"),
                activity_score=("activity_score", "sum"),
                newly_active_fish=("newly_active", "sum"),
            )
            weather = weather.merge(map_activity, on="map_id", how="left")
        map_windows = recent_window_evidence(catches, by_fish=False)
        if not map_windows.empty:
            weather = weather.merge(map_windows, on="map_id", how="left")
    else:
        weather[["recent_catches", "recent_stars", "last_catch"]] = [0, 0, None]
        weather["point_label"] = ""
        weather["target_fish"] = ""
        weather["representative_coordinates"] = ""
    if not (is_trophy or objective == "指定鱼种") and not is_farm and not community.empty:
        map_community = map_community_evidence_table()
        weather = weather.merge(map_community, on="map_id", how="left")
    if not earnings.empty:
        map_income = earnings.groupby("map_id", as_index=False).agg(
            map_reference_income=("silver_per_hour", "max"),
            map_income_samples=("source_key", "count"),
        )
        if is_farm:
            point_income_rows = []
            for _, sample in earnings.iterrows():
                label = earnings_point_group(sample["map_id"], sample.get("coordinates"))
                if label:
                    point_income_rows.append(
                        {
                            "map_id": sample["map_id"],
                            "point_label": label,
                            "point_reference_income": sample["silver_per_hour"],
                            "source_key": sample["source_key"],
                        }
                    )
            point_income = pd.DataFrame(point_income_rows)
            if not point_income.empty:
                point_income = point_income.groupby(["map_id", "point_label"], as_index=False).agg(
                    point_reference_income=("point_reference_income", "max"),
                    point_income_samples=("source_key", "count"),
                )
                weather = weather.merge(point_income, on=["map_id", "point_label"], how="left")
            else:
                weather["point_reference_income"] = None
                weather["point_income_samples"] = 0
            weather = weather.merge(map_income, on="map_id", how="left")
            weather["reference_income"] = weather["point_reference_income"].fillna(weather["map_reference_income"])
            weather["income_samples"] = weather["point_income_samples"].fillna(0)
            weather["income_scope"] = weather["point_reference_income"].notna().map(
                {True: "点位小时样本", False: "仅地图背景"}
            )
        else:
            weather = weather.merge(map_income, on="map_id", how="left")
            weather["reference_income"] = weather["map_reference_income"]
            weather["income_samples"] = weather["map_income_samples"]
            weather["income_scope"] = "地图级样本"
    else:
        weather[["reference_income", "income_samples"]] = [None, 0]
        weather["income_scope"] = "暂无收益样本"
    temperature_baselines = map_temperature_baselines()
    if not temperature_baselines.empty:
        weather = weather.merge(temperature_baselines, on="map_id", how="left")
    if "normal_forecast_temperature" not in weather:
        weather["normal_forecast_temperature"] = None
    if "temperature_baseline_samples" not in weather:
        weather["temperature_baseline_samples"] = 0
    weather["temperature_baseline_samples"] = weather["temperature_baseline_samples"].fillna(0)
    temperature_delta = pd.to_numeric(weather["forecast_day_temperature"], errors="coerce") - pd.to_numeric(
        weather["normal_forecast_temperature"], errors="coerce"
    )
    weather["temperature_regime"] = "暂无常温基线"
    baseline_ready = weather["temperature_baseline_samples"].ge(15) & temperature_delta.notna()
    weather.loc[baseline_ready & temperature_delta.le(-2), "temperature_regime"] = "低温"
    weather.loc[baseline_ready & temperature_delta.ge(2), "temperature_regime"] = "高温"
    weather.loc[baseline_ready & temperature_delta.gt(-2) & temperature_delta.lt(2), "temperature_regime"] = "常温"
    for column in ("recent_catches", "recent_stars", "income_samples"):
        weather[column] = weather[column].fillna(0)
    for column in ("current_hour_catches", "previous_3h_catches", "activity_score"):
        if column not in weather:
            weather[column] = 0
        weather[column] = weather[column].fillna(0)
    for hours in RECENT_WINDOWS:
        for prefix in ("catches", "stars"):
            column = f"{prefix}_{hours}h"
            if column not in weather:
                weather[column] = 0
            weather[column] = weather[column].fillna(0)
    for hours in RF4STAT_TROPHY_WINDOWS:
        column = f"rf4stat_trophies_{hours}h"
        if column not in weather:
            weather[column] = 0
        weather[column] = weather[column].fillna(0)
    for column in ("rf4stat_trophy_hourly_rate", "rf4stat_trophy_score"):
        if column not in weather:
            weather[column] = 0
        weather[column] = weather[column].fillna(0)
    if "recent_activity_score" not in weather:
        weather["recent_activity_score"] = 0
    weather["recent_activity_score"] = weather["recent_activity_score"].fillna(0)
    for column in ("community_records", "community_points", "community_trophy_records", "weekly_threshold_known", "weekly_inferred_trophies"):
        if column not in weather:
            weather[column] = 0
        weather[column] = weather[column].fillna(0)
    for column in ("community_sources", "community_latest", "community_coordinates", "community_baits"):
        if column not in weather:
            weather[column] = ""
        weather[column] = weather[column].fillna("")
    if "weekly_record_count" not in weather:
        weather["weekly_record_count"] = 0
    weather["weekly_record_count"] = weather["weekly_record_count"].fillna(0)
    if "top3_baits" not in weather:
        weather["top3_baits"] = "暂无公开饵料"
    weather["top3_baits"] = weather["top3_baits"].fillna("暂无公开饵料")
    if "activity_status" not in weather:
        weather["activity_status"] = "样本不足"
    weather["activity_status"] = weather["activity_status"].fillna("样本不足")
    if "newly_active" not in weather:
        weather["newly_active"] = False
    weather["newly_active"] = weather["newly_active"].eq(True)
    weather["reference_income"] = weather["reference_income"].fillna(0)
    income_score = (weather["reference_income"] / 40).clip(upper=60) + weather["income_samples"].clip(upper=10)
    if is_farm:
        income_score = income_score.where(weather["income_scope"] == "点位小时样本", income_score * 0.35)
    sighting_score = weather["recent_stars"].clip(upper=15) * 4 + weather["recent_catches"].clip(upper=20)
    community_score = weather["community_records"].clip(upper=10) * 0.5 + weather["community_points"].clip(upper=5) * 1.0
    if is_farm:
        weather["score"] = (
            income_score
            + weather["recent_catches"].clip(upper=100) * 0.15
            + weather["activity_score"].clip(upper=40) * 0.2
            + weather["recent_activity_score"].clip(upper=30) * 0.15
            + community_score * 0.25
        )
    elif is_trophy or objective == "指定鱼种":
        weather["point_evidence_score"] = 10 * weather["community_points"] / (weather["community_points"] + 2)
        weather["weekly_background_score"] = (
            10 * weather["weekly_inferred_trophies"] / (weather["weekly_inferred_trophies"] + 3)
        )
        weather["ordinary_background_score"] = (
            5 * weather["recent_activity_score"] / (weather["recent_activity_score"] + 2)
        )
        # 正式天气/温度偏好仍缺用户日暴露与温度校准；未达门槛时必须保持0分。
        weather["condition_match_score"] = 0.0
        weather["score"] = (
            weather["rf4stat_trophy_score"]
            + weather["point_evidence_score"]
            + weather["weekly_background_score"]
            + weather["ordinary_background_score"]
            + weather["condition_match_score"]
        ).clip(upper=100)
    else:
        weather["score"] = (
            income_score * 0.5
            + sighting_score * 0.15
            + weather["activity_score"] * 0.2
            + weather["recent_activity_score"].clip(upper=30) * 0.75
            + community_score * 0.4
        )
    weather["score"] = weather["score"].round(1)
    weather["score_basis"] = {
        "搬砖收益": "点位分享收益小时 + 该小时渔获构成 + 普通流地图活跃背景",
        "搬砖（地图+点位）": "点位分享收益小时 + 该小时渔获构成 + 普通流地图活跃背景",
        "蹲星": "RF4-STAT本周上星3/6/12/24/48/72小时更新 + 点位 + RF4DB周榜背景 + 少量普通鱼背景",
        "蹲星（地图+鱼种）": "RF4-STAT本周上星3/6/12/24/48/72小时更新 + 点位 + RF4DB周榜背景 + 少量普通鱼背景",
        "指定鱼种": "RF4-STAT本周上星3/6/12/24/48/72小时更新 + 点位 + RF4DB周榜背景 + 少量普通鱼背景",
    }.get(objective, "地图收益、普通流多窗口近期活跃度与公开星鱼证据")
    if is_farm:
        weather.loc[weather["income_scope"] == "点位小时样本", "score_basis"] = "同点位分享收益小时 + 鱼种构成 + 地图普通流活跃背景"
    weather["recommendation_label"] = weather["map_name"]
    if is_farm:
        weather["recommendation_label"] = weather["map_name"] + " · " + weather["point_label"]
    elif is_trophy or objective == "指定鱼种":
        weather["recommendation_label"] = (
            weather["map_name"] + " · " + weather["target_fish"]
            + "（" + weather["temperature_regime"] + "）"
        )
    weather["candidate_id"] = (
        weather["map_id"].astype(str)
        + "|" + weather["point_label"].fillna("").astype(str)
        + "|" + weather["target_fish"].fillna("").astype(str)
    )
    if is_trophy or objective == "指定鱼种":
        weather["confidence"] = weather.apply(
            lambda r: "中（公开上星观察）"
            if r.get("rf4stat_trophies_72h", 0) >= 5 and r.get("community_points", 0) >= 1
            else "低（积累期）", axis=1
        )
    else:
        weather["confidence"] = weather.apply(
            lambda r: "中" if r["recent_catches"] >= 5 and r["income_samples"] >= 1 else "低（积累期）", axis=1
        )
    return weather.sort_values(["score", "reference_income"], ascending=False)


def refresh_status() -> pd.DataFrame:
    return read_frame(
        """SELECT r.*,
        (SELECT MAX(s.finished_at) FROM refresh_runs s
         WHERE s.source=r.source AND s.success=1) AS last_success_at
        FROM refresh_runs r
        JOIN (SELECT source, MAX(id) AS max_id FROM refresh_runs
              WHERE source NOT IN ('rf4stat_manual_import','rf4stat_trophy_manual_import')
              GROUP BY source) x
        ON r.id=x.max_id ORDER BY r.source"""
    )


def refresh_coverage_status() -> pd.DataFrame:
    return read_frame(
        """SELECT stream,source,status,previous_success_at,earliest_fetched_at,
        latest_fetched_at,fetched_rows,overlap_rows,note,updated_at
        FROM refresh_coverage ORDER BY source,stream"""
    )


def history_coverage() -> pd.DataFrame:
    return read_frame(
        """SELECT fish_grade AS data_stream, COUNT(*) AS sample_count,
        MIN(caught_at) AS oldest_record, MAX(caught_at) AS newest_record,
        COUNT(DISTINCT map_id) AS map_count, COUNT(DISTINCT fish_id) AS fish_count
        FROM catch_records GROUP BY fish_grade ORDER BY fish_grade"""
    )


def condition_history_coverage() -> pd.DataFrame:
    rf4db = read_frame(
        """SELECT 'RF4DB:' || source_id AS source_record_key, 'RF4DB渔获' AS source_name,
                  map_id,fish_id,game_time,weather_code,catch_temperature,caught_at
           FROM catch_records"""
    )
    rf4db["evidence_time"] = _normalized_caught_times(rf4db)
    rf4stat = rf4stat_habit_records()
    if not rf4stat.empty:
        rf4stat = rf4stat[
            ["source_record_key", "source_name", "map_id", "fish_id", "game_time",
             "weather_code", "catch_temperature", "caught_at"]
        ]
        rf4stat["evidence_time"] = pd.to_datetime(
            rf4stat["caught_at"], format="mixed", errors="coerce", utc=True
        )
    evidence = pd.concat(
        [block.dropna(axis=1, how="all") for block in (rf4db, rf4stat)],
        ignore_index=True,
        sort=False,
    )
    if evidence.empty:
        return pd.DataFrame([{
            "valid_weather_samples": 0, "valid_temperature_samples": 0,
            "valid_game_time_samples": 0, "oldest_evidence_record": None,
            "newest_evidence_record": None, "weather_covered_dates": 0,
            "temperature_covered_dates": 0, "map_fish_groups": 0,
            "weather_groups_ge_25": 0, "temperature_groups_ge_25": 0,
            "weather_ready_groups": 0, "temperature_ready_groups": 0,
            "source_breakdown": "暂无",
        }])
    evidence = evidence.drop_duplicates("source_record_key")
    weather_mask = evidence["weather_code"].notna() & ~evidence["weather_code"].isin(["", "unknown"])
    temperature_mask = pd.to_numeric(evidence["catch_temperature"], errors="coerce").notna()
    game_time_mask = evidence["game_time"].astype("string").str.fullmatch(
        r"(?:[01]\d|2[0-3]):[0-5]\d", na=False
    )

    def grouped_ready(mask: pd.Series) -> tuple[int, int, int]:
        valid = evidence[mask & evidence["map_id"].notna() & evidence["fish_id"].notna()].copy()
        if valid.empty:
            return 0, 0, 0
        valid["date"] = valid["evidence_time"].dt.date
        grouped = valid.groupby(["map_id", "fish_id"]).agg(
            samples=("source_record_key", "nunique"), covered_days=("date", "nunique")
        )
        return len(grouped), int((grouped["samples"] >= 25).sum()), int(
            ((grouped["samples"] >= 25) & (grouped["covered_days"] >= 15)).sum()
        )

    weather_groups, weather_ge_25, weather_ready = grouped_ready(weather_mask)
    temperature_groups, temperature_ge_25, temperature_ready = grouped_ready(temperature_mask)
    valid_times = evidence["evidence_time"].dropna()
    return pd.DataFrame([{
        "valid_weather_samples": int(weather_mask.sum()),
        "valid_temperature_samples": int(temperature_mask.sum()),
        "valid_game_time_samples": int(game_time_mask.sum()),
        "oldest_evidence_record": valid_times.min().isoformat() if not valid_times.empty else None,
        "newest_evidence_record": valid_times.max().isoformat() if not valid_times.empty else None,
        "weather_covered_dates": int(evidence.loc[weather_mask, "evidence_time"].dt.date.nunique()),
        "temperature_covered_dates": int(evidence.loc[temperature_mask, "evidence_time"].dt.date.nunique()),
        "map_fish_groups": max(weather_groups, temperature_groups),
        "weather_groups_ge_25": weather_ge_25,
        "temperature_groups_ge_25": temperature_ge_25,
        "weather_ready_groups": weather_ready,
        "temperature_ready_groups": temperature_ready,
        "source_breakdown": source_breakdown(evidence),
    }])


def data_source_inventory() -> pd.DataFrame:
    """返回真实接入状态和本地覆盖，避免把计划来源展示成已接入。"""
    rf4db = read_frame(
        """SELECT
        (SELECT COUNT(*) FROM weather_forecasts) AS weather_rows,
        (SELECT COUNT(*) FROM earnings_samples) AS earnings_rows,
        (SELECT COUNT(*) FROM map_assets) AS map_asset_rows,
        (SELECT COUNT(*) FROM catch_records) AS catch_rows,
        (SELECT MIN(caught_at) FROM catch_records) AS oldest_record,
        (SELECT MAX(caught_at) FROM catch_records) AS newest_record"""
    ).iloc[0]
    official = read_frame(
        """SELECT COUNT(*) AS record_rows, COUNT(DISTINCT week_key) AS week_count,
        MIN(record_date) AS oldest_record, MAX(record_date) AS newest_record
        FROM official_weekly_records WHERE region='CN' AND category='records'
        AND fish_id IS NOT NULL AND map_id IS NOT NULL"""
    ).iloc[0]
    community_rows = read_frame(
        """SELECT source_name, COUNT(*) AS row_count,
        MIN(COALESCE(reported_at,collected_at)) AS oldest_record,
        MAX(COALESCE(reported_at,collected_at)) AS newest_record,
        SUM(CASE WHEN validation_status='有效' THEN 1 ELSE 0 END) AS valid_rows
        FROM community_spots GROUP BY source_name"""
    )
    community_stats = {row["source_name"]: row for _, row in community_rows.iterrows()}

    def community_inventory(source_name: str, usage: str) -> dict:
        stats = community_stats.get(source_name)
        if stats is None:
            return {
                "source": source_name, "integration_status": "已接入（尚无成功缓存）", "local_rows": 0,
                "coverage": "等待首次成功刷新", "usage": usage,
            }
        return {
            "source": source_name,
            "integration_status": "已接入",
            "local_rows": int(stats["row_count"] or 0),
            "coverage": f"有效 {int(stats['valid_rows'] or 0)} 条；{stats['oldest_record'] or '暂无'} ～ {stats['newest_record'] or '暂无'}",
            "usage": usage,
        }
    return pd.DataFrame(
        [
            {
                "source": "RF4DB",
                "integration_status": "已接入",
                "local_rows": int(rf4db["weather_rows"] + rf4db["earnings_rows"] + rf4db["map_asset_rows"] + rf4db["catch_rows"]),
                "coverage": f"地图资源 {int(rf4db['map_asset_rows'] or 0)} 张；渔获 {rf4db['oldest_record'] or '暂无'} ～ {rf4db['newest_record'] or '暂无'}",
                "usage": "地图底图与坐标校准、天气、收益、普通活跃流、带条件星鱼与点位",
            },
            {
                "source": "RF4DB镜像·中国区普通周榜",
                "integration_status": "已接入（逐周累积）",
                "local_rows": int(official["record_rows"] or 0),
                "coverage": f"{int(official['week_count'] or 0)} 个周次；记录日期 {official['oldest_record'] or '暂无'} ～ {official['newest_record'] or '暂无'}",
                "usage": "近期高质量鱼种/地图/饵料活跃证据；不进入天气温度模型",
            },
            community_inventory("RF4-STAT", "公开上鱼时间、点位和饵料；按鱼种回推中鱼时间并匹配当时已缓存天气，不提供即时温度"),
            community_inventory("RF4-STAT 帖子", "RF4-STAT公开帖子坐标与饵料；仅近期48小时参与推荐，不作收益和习性样本"),
            community_inventory("RF4MAP", "社区点位、坐标、鱼种、饵料和卡线交叉证据"),
        ]
    )


def weekly_fish_activity() -> pd.DataFrame:
    return read_frame(
        """WITH latest AS (SELECT MAX(week_key) AS week_key FROM official_weekly_records)
        SELECT map_id, fish_id, MAX(fish_name) AS target_fish, COUNT(*) AS weekly_record_count,
        COUNT(trophy_threshold_kg) AS weekly_threshold_known,
        SUM(CASE WHEN trophy_threshold_kg IS NOT NULL AND weight_kg >= trophy_threshold_kg THEN 1 ELSE 0 END) AS weekly_inferred_trophies,
        MAX(record_date) AS latest_weekly_record
        FROM official_weekly_records JOIN latest USING(week_key)
        WHERE region='CN' AND category='records' AND map_id IS NOT NULL AND fish_id IS NOT NULL
        GROUP BY map_id,fish_id"""
    )


def weekly_evidence_details(map_id: str, fish_id: str) -> pd.DataFrame:
    rows = read_frame(
        """SELECT fish_name AS 鱼种, map_name AS 地图, weight_kg AS 重量kg,
        trophy_threshold_kg AS 目录奖杯线kg,
        CASE WHEN trophy_threshold_kg IS NULL THEN '门槛未知'
             WHEN weight_kg >= trophy_threshold_kg THEN '按重量推定达标'
             ELSE '未达目录奖杯线' END AS 达标判定,
        bait_name AS 饵料, record_date AS 记录日期, source_name AS 来源,
        source_url AS 来源链接, threshold_source_url AS 门槛来源, threshold_checked_at AS 门槛检查时间,
        collected_at AS 采集时间
        FROM official_weekly_records WHERE region='CN' AND category='records'
        AND week_key=(SELECT MAX(week_key) FROM official_weekly_records)
        AND map_id=? AND fish_id=? ORDER BY weight_kg DESC""", (map_id, fish_id)
    )
    return rows

