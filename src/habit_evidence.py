from __future__ import annotations

from collections import Counter

import pandas as pd

from .config import FISH_ZH
from .db import read_frame


FIGHT_TIME_OFFSETS_MINUTES = {
    "x_gladius": 60,
    "cetorhinus_m": 300,
    "somniosus_m": 90,
    "lampris_g": 20,
    "lamna_n": 20,
}


EVIDENCE_COLUMNS = [
    "source_record_key", "source_name", "fish_id", "fish_name", "fish_name_en",
    "map_id", "coordinate", "spot_label", "clip", "game_time", "weather_code",
    "catch_temperature", "rig", "baits", "fish_grade", "caught_at",
    "reported_at", "source_url", "time_basis", "weather_basis",
]


def game_time_from_real(value) -> str | None:
    """北京时间现实整点=游戏00:00；一个现实小时=一个游戏日。"""
    stamp = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(stamp):
        return None
    seconds = stamp.minute * 60 + stamp.second + stamp.microsecond / 1_000_000
    game_minutes = int(seconds * 24 / 60) % (24 * 60)
    return f"{game_minutes // 60:02d}:{game_minutes % 60:02d}"


def _weather_lookup(map_ids: list[str]) -> dict[tuple[str, pd.Timestamp], pd.DataFrame]:
    if not map_ids:
        return {}
    placeholders = ",".join("?" for _ in map_ids)
    weather = read_frame(
        f"""SELECT map_id,forecast_slot,weather_code,forecast_day_temperature,
                   collected_at,source_url
            FROM weather_forecasts WHERE map_id IN ({placeholders})""",
        tuple(map_ids),
    )
    if weather.empty:
        return {}
    weather["slot_time"] = pd.to_datetime(weather["forecast_slot"], errors="coerce", utc=True)
    weather["snapshot_time"] = pd.to_datetime(weather["collected_at"], errors="coerce", utc=True)
    weather = weather[
        weather["slot_time"].notna()
        & weather["snapshot_time"].notna()
        & weather["weather_code"].notna()
        & ~weather["weather_code"].isin(["", "unknown"])
    ].sort_values("snapshot_time")
    return {
        (map_id, slot): group
        for (map_id, slot), group in weather.groupby(["map_id", "slot_time"], sort=False)
    }


def _match_weather(
    lookup: dict[tuple[str, pd.Timestamp], pd.DataFrame], map_id: str, hooked_at: pd.Timestamp
) -> tuple[str | None, str | None]:
    if not map_id or pd.isna(hooked_at):
        return None, None
    candidates = lookup.get((map_id, hooked_at.floor("h")))
    if candidates is None or candidates.empty:
        return None, None
    eligible = candidates[candidates["snapshot_time"] <= hooked_at]
    if eligible.empty:
        return None, None
    matched = eligible.iloc[-1]
    return str(matched["weather_code"]), f"历史预报快照 {matched['collected_at']}"


def _empty_evidence() -> pd.DataFrame:
    return pd.DataFrame(columns=EVIDENCE_COLUMNS)


def rf4stat_habit_records(map_id: str = "", fish_id: str = "") -> pd.DataFrame:
    """把有上鱼时间的RF4-STAT渔获回推为游戏时间并匹配当时已有天气快照。"""
    clauses = [
        "source_name='RF4-STAT'",
        "evidence_type='public_catch_activity'",
        "validation_status='有效'",
    ]
    params: list[str] = []
    if map_id:
        clauses.append("map_id=?")
        params.append(map_id)
    if fish_id:
        clauses.append("fish_id=?")
        params.append(fish_id)
    frame = read_frame(
        f"""SELECT source_key,source_record_id,map_id,coordinate,spot_label,fish_id,fish_name,
                   baits,rig,clip,quality_label,reported_at,collected_at,source_url
            FROM community_spots WHERE {' AND '.join(clauses)}""",
        tuple(params),
    )
    if frame.empty:
        return _empty_evidence()
    frame = frame.drop_duplicates("source_key").copy()
    if "spot_label" not in frame:
        frame["spot_label"] = None
    frame["reported_time"] = pd.to_datetime(frame["reported_at"], errors="coerce", utc=True)
    frame["fight_offset_minutes"] = frame["fish_id"].map(FIGHT_TIME_OFFSETS_MINUTES).fillna(0)
    frame["hooked_time"] = frame["reported_time"] - pd.to_timedelta(
        frame["fight_offset_minutes"], unit="m"
    )
    lookup = _weather_lookup(frame["map_id"].dropna().astype(str).unique().tolist())
    matched = [
        _match_weather(lookup, str(row.map_id or ""), row.hooked_time)
        for row in frame.itertuples()
    ]
    frame["weather_code"] = [item[0] for item in matched]
    frame["weather_basis"] = [item[1] for item in matched]
    frame["source_record_key"] = "RF4-STAT:" + frame["source_key"].astype(str)
    frame["source_name"] = "RF4-STAT上鱼记录"
    frame["fish_name"] = frame.apply(
        lambda row: FISH_ZH.get(row["fish_id"], row["fish_name"]), axis=1
    )
    frame["fish_name_en"] = frame["fish_name"]
    frame["game_time"] = frame["hooked_time"].map(game_time_from_real)
    frame["catch_temperature"] = None
    frame["fish_grade"] = frame["quality_label"].map(
        lambda value: "star" if str(value or "").strip() == "奖杯标记" else "unknown"
    )
    frame["caught_at"] = frame["hooked_time"].map(
        lambda value: value.isoformat() if pd.notna(value) else None
    )
    frame["time_basis"] = frame["fight_offset_minutes"].map(
        lambda minutes: f"上报时间回推{int(minutes)}分钟" if minutes else "上报时间（未设搏鱼回推）"
    )
    return frame[EVIDENCE_COLUMNS]


def all_source_habit_records(catches: pd.DataFrame, map_id: str) -> pd.DataFrame:
    """汇集详情可用的全部来源；每个字段独立准入，不跨记录补字段。"""
    blocks: list[pd.DataFrame] = []
    if not catches.empty:
        rf4db = catches[catches["map_id"].eq(map_id)].copy()
        if not rf4db.empty:
            for column in EVIDENCE_COLUMNS:
                if column not in rf4db:
                    rf4db[column] = None
            rf4db["source_record_key"] = "RF4DB:" + rf4db.get(
                "source_id", pd.Series(rf4db.index, index=rf4db.index)
            ).astype(str)
            rf4db["source_name"] = "RF4DB渔获"
            rf4db["reported_at"] = rf4db["caught_at"]
            rf4db["time_basis"] = "来源捕获游戏时间"
            rf4db["weather_basis"] = "来源捕获天气"
            blocks.append(rf4db[EVIDENCE_COLUMNS])

    community = read_frame(
        """SELECT source_key,source_name,fish_id,fish_name,map_id,coordinate,spot_label,clip,
                  rig,baits,quality_label,reported_at,source_url,evidence_type
           FROM community_spots
           WHERE validation_status='有效' AND map_id=? AND source_name!='RF4-STAT'""",
        (map_id,),
    )
    if not community.empty:
        for column in EVIDENCE_COLUMNS:
            if column not in community:
                community[column] = None
        community["source_record_key"] = (
            community["source_name"].astype(str) + ":" + community["source_key"].astype(str)
        )
        community["fish_name"] = community.apply(
            lambda row: FISH_ZH.get(row["fish_id"], row["fish_name"]), axis=1
        )
        community["fish_name_en"] = community["fish_name"]
        community["fish_grade"] = community["quality_label"].map(
            lambda value: "star" if str(value or "").strip() == "奖杯标记" else "unknown"
        )
        blocks.append(community[EVIDENCE_COLUMNS])

    rf4stat = rf4stat_habit_records(map_id=map_id)
    if not rf4stat.empty:
        blocks.append(rf4stat)

    weekly = read_frame(
        """SELECT source_key,source_name,fish_id,fish_name,map_id,bait_name,record_date,
                  source_url FROM official_weekly_records WHERE map_id=?""",
        (map_id,),
    )
    if not weekly.empty:
        for column in EVIDENCE_COLUMNS:
            if column not in weekly:
                weekly[column] = None
        weekly["source_record_key"] = "周榜:" + weekly["source_key"].astype(str)
        weekly["source_name"] = weekly["source_name"].fillna("RF4DB镜像周榜")
        weekly["fish_name"] = weekly.apply(
            lambda row: FISH_ZH.get(row["fish_id"], row["fish_name"]), axis=1
        )
        weekly["fish_name_en"] = weekly["fish_name"]
        weekly["baits"] = weekly["bait_name"]
        weekly["reported_at"] = weekly["record_date"]
        weekly["fish_grade"] = "weekly_record"
        blocks.append(weekly[EVIDENCE_COLUMNS])

    if not blocks:
        return _empty_evidence()
    result = pd.concat(
        [block.dropna(axis=1, how="all") for block in blocks], ignore_index=True, sort=False
    ).reindex(columns=EVIDENCE_COLUMNS)
    return result.drop_duplicates("source_record_key", keep="last")


def source_breakdown(frame: pd.DataFrame) -> str:
    if frame.empty or "source_name" not in frame:
        return "暂无"
    counts = Counter(frame["source_name"].dropna().astype(str))
    return " / ".join(f"{name} {count}" for name, count in counts.most_common()) or "暂无"
