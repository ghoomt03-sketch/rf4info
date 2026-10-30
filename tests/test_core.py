from __future__ import annotations

import unittest
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from bs4 import BeautifulSoup
from PIL import Image

from src.analytics import (
    build_earnings_species_mix,
    build_bait_recommendations,
    build_combined_window_summary,
    build_reason_bait_combinations,
    build_recommendation_detail,
    earnings_point_group,
    filter_catches_by_fish,
    filter_catches_by_selected_fish,
    fish_activity_table,
    point_group,
    recommendation_horizons,
    rf4stat_trophy_update_activity,
    map_temperature_baselines,
    recent_window_evidence,
    recent_six_hour_evidence,
    select_map_render_sample,
)
from src.collectors.rf4db import _earnings_species, _number, collect_catches, collect_map_assets
from src.collectors.official_weekly import _chinese_only
from src.collectors.community import _rf4stat_time
from src.config import MAPS, normalize_weather
from src.map_render import build_interactive_map_figure
from src.normalization import canonical_bait, canonical_fish, normalize_coordinate, normalize_map_id, normalize_spot_label, split_bait_terms


class CoreTests(unittest.TestCase):
    @patch("src.analytics.latest_weather")
    @patch("src.analytics.recommendation_table")
    def test_recommendation_horizons_reuses_evidence_and_replaces_weather(
        self, recommendation_table, latest_weather
    ):
        recommendation_table.return_value = pd.DataFrame([{
            "map_id": "map-a", "map_name": "地图A", "unlock_level": 1,
            "forecast_slot": "2026-10-30T10:00:00+08:00", "horizon": 0,
            "weather_code": "clear", "forecast_day_temperature": 10.0,
            "wind_speed": 1.0, "collected_at": "now", "source_url": "weather-0",
            "天气": "晴", "normal_forecast_temperature": 12.0,
            "temperature_baseline_samples": 20, "temperature_regime": "低温",
            "point_label": "", "target_fish": "", "score": 42.0,
            "reference_income": 100.0, "recommendation_label": "地图A",
        }])
        latest_weather.return_value = pd.DataFrame([{
            "map_id": "map-a", "map_name": "地图A", "unlock_level": 1,
            "forecast_slot": "2026-10-30T11:00:00+08:00", "horizon": 1,
            "weather_code": "cloudy", "forecast_day_temperature": 14.0,
            "wind_speed": 2.0, "collected_at": "now", "source_url": "weather-1",
            "天气": "多云",
        }])

        current, following = recommendation_horizons(34)

        recommendation_table.assert_called_once_with(0, 34, "综合推荐", "", ())
        self.assertEqual(current.iloc[0]["horizon"], 0)
        self.assertEqual(following.iloc[0]["horizon"], 1)
        self.assertEqual(following.iloc[0]["weather_code"], "cloudy")
        self.assertEqual(following.iloc[0]["temperature_regime"], "高温")
        self.assertEqual(following.iloc[0]["score"], 42.0)

    def test_map_asset_collection_is_independent_from_earnings(self):
        soup = BeautifulSoup(
            '<script>{"calibration":{"mapSystemId":"map-a","minX":1,"minY":2,"maxX":101,"maxY":102,'
            '"metersPerUnit":5,"version":1,"confidence":"reference"},'
            '"imageUrl":"/game/paper_maps_webp/map_map-a.webp"}</script>',
            "lxml",
        )
        catalog = {"level_000_home": ("家", "Home", 0), "map-a": ("地图A", "Map A", 1)}
        with patch("src.collectors.rf4db.MAPS", catalog), patch(
            "src.collectors.rf4db._get", return_value=soup
        ), patch("src.collectors.rf4db._cache_map_image", return_value="data/maps/map-a.webp"):
            rows = collect_map_assets(request_delay=0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["map_id"], "map-a")
        self.assertIn('"minX": 1', rows[0]["map_calibration"])
        self.assertEqual(rows[0]["map_image_path"], "data/maps/map-a.webp")

    def test_map_render_prefers_map_asset_and_keeps_earnings_as_fallback(self):
        assets = pd.DataFrame([{
            "map_id": "map-a", "map_image_path": "asset.webp",
            "map_calibration": '{"minX":0,"minY":0,"maxX":100,"maxY":100}',
            "collected_at": "2026-10-30T00:00:00Z",
        }])
        earnings = pd.DataFrame([{
            "map_id": "map-a", "map_image_path": "earnings.webp", "map_calibration": "{}",
            "coordinates": "1:2", "spot_details": "[]", "catch_count": 1, "silver_per_hour": 1,
        }])
        selected = select_map_render_sample(assets, earnings, "map-a")
        self.assertEqual(selected["map_image_path"], "asset.webp")
        self.assertEqual(selected["spot_details"], "[]")
        fallback = select_map_render_sample(pd.DataFrame(), earnings, "map-a")
        self.assertEqual(fallback["map_image_path"], "earnings.webp")

    def test_rf4db_normal_list_preserves_explicit_star_marker(self):
        def card(source_id: str, grade: str = "") -> str:
            ribbon = f'<span data-fish-grade="{grade}"></span>' if grade else ""
            return f'''<article class="catch-card"><h2 class="fish-name"><a href="/zh/fishes/fish-a">鱼A</a>{ribbon}</h2>
            <a href="/zh/maps/map-a">地图A</a><a href="/zh/catches/{source_id}">详情</a>
            <div class="time">2026-10-28 20:01</div></article>'''
        soup = BeautifulSoup(card("star-id", "star") + card("normal-id"), "lxml")
        with patch("src.collectors.rf4db._get", return_value=soup):
            rows = collect_catches(pages=0, record_pages=1, request_delay=0)
        grades = {row["source_id"]: row["fish_grade"] for row in rows}
        self.assertEqual(grades, {"star-id": "star", "normal-id": "normal"})

    @patch("src.analytics.read_frame")
    def test_rf4stat_trophy_updates_use_per_record_reported_time(self, read_frame):
        read_frame.return_value = pd.DataFrame([
            {"source_key": "1", "map_id": "map-a", "fish_id": "fish-a", "fish_name": "鱼A", "reported_at": "2026-10-28T17:30:00+08:00"},
            {"source_key": "2", "map_id": "map-a", "fish_id": "fish-a", "fish_name": "鱼A", "reported_at": "2026-10-28T14:30:00+08:00"},
            {"source_key": "3", "map_id": "map-a", "fish_id": "fish-a", "fish_name": "鱼A", "reported_at": "2026-10-27T18:00:00+08:00"},
        ])
        row = rf4stat_trophy_update_activity("2026-10-28T18:00:00+08:00").iloc[0]
        self.assertEqual(row["rf4stat_trophies_3h"], 1)
        self.assertEqual(row["rf4stat_trophies_6h"], 2)
        self.assertEqual(row["rf4stat_trophies_24h"], 2)
        self.assertEqual(row["rf4stat_trophies_48h"], 3)
        self.assertGreater(row["rf4stat_trophy_score"], 0)

    @patch("src.analytics.read_frame")
    def test_temperature_baseline_deduplicates_forecast_slots(self, read_frame):
        read_frame.return_value = pd.DataFrame([
            {"map_id": "map-a", "forecast_slot": "2026-10-28T10:00:00+08:00", "forecast_day_temperature": 10, "collected_at": "2026-10-28T08:00:00+08:00"},
            {"map_id": "map-a", "forecast_slot": "2026-10-28T10:00:00+08:00", "forecast_day_temperature": 14, "collected_at": "2026-10-28T09:00:00+08:00"},
            {"map_id": "map-a", "forecast_slot": "2026-10-28T11:00:00+08:00", "forecast_day_temperature": 16, "collected_at": "2026-10-28T09:00:00+08:00"},
        ])
        row = map_temperature_baselines().iloc[0]
        self.assertEqual(row["temperature_baseline_samples"], 2)
        self.assertEqual(row["normal_forecast_temperature"], 15)

    def test_rf4db_refresh_rejects_an_empty_required_stream(self):
        with patch("src.collectors.rf4db._get", return_value=BeautifulSoup("", "lxml")):
            with self.assertRaisesRegex(RuntimeError, "普通渔获"):
                collect_catches(pages=1, record_pages=1, request_delay=0)

    def test_selected_fish_uses_exact_standard_name(self):
        frame = pd.DataFrame([
            {"fish_name": "鳕鱼", "fish_name_en": "Cod", "fish_id": "cod"},
            {"fish_name": "大西洋鳕鱼", "fish_name_en": "Atlantic cod", "fish_id": "atlantic_cod"},
        ])
        result = filter_catches_by_selected_fish(frame, "鳕鱼")
        self.assertEqual(result["fish_id"].tolist(), ["cod"])

    def test_map_catalog_has_stable_ids(self):
        self.assertGreaterEqual(len(MAPS), 18)
        self.assertTrue(all(key.startswith("level_") for key in MAPS))

    def test_weather_display_names_are_normalized(self):
        self.assertEqual(normalize_weather("Cloudy"), "cloudy")

    def test_community_source_formats_are_normalized(self):
        self.assertEqual(normalize_map_id("  оз.   Медное "), "level_018_moskitnoe")
        self.assertEqual(normalize_coordinate("точка 67,5 : 59.0"), "67.5:59")
        self.assertIsNone(normalize_coordinate("без координат"))
        self.assertEqual(normalize_spot_label("80"), "80坑")
        self.assertEqual(normalize_spot_label("170坑"), "170坑")
        self.assertIsNone(normalize_spot_label("80:90"))

    def test_community_fish_names_use_rf4db_canonical_ids(self):
        catalog = {"asp": "欧鳡", "ruffe": "梅花鲈"}
        self.assertEqual(
            canonical_fish("Жерех", "asp", catalog=catalog),
            ("asp", "欧鳡", True),
        )
        self.assertEqual(
            canonical_fish("Ерш", "rf4map:abc", catalog=catalog, aliases={"ерш": "ruffe"}),
            ("ruffe", "梅花鲈", True),
        )
        self.assertEqual(
            canonical_fish("Новая рыба", "rf4map:new", catalog=catalog, aliases={}),
            ("rf4map:new", "未收录中文鱼种（rf4map:new）", False),
        )

    def test_baits_use_rf4db_names_and_keep_official_model_names(self):
        catalog = {"crawler": "大蚯蚓", "maggot": "蛆虫"}
        aliases = {"выползок": "crawler", "опарыш": "maggot"}
        self.assertEqual(
            canonical_bait("Выползок", None, catalog=catalog, aliases=aliases),
            ("crawler", "大蚯蚓", True),
        )
        model = canonical_bait("Balsa Walker 65F-005", None, catalog={}, aliases={})
        self.assertEqual(model[1:], ("Balsa Walker 65F-005", True))
        self.assertEqual(
            canonical_bait("Неизвестная наживка", "rf4map-bait:x", catalog={}, aliases={})[1:],
            ("未收录标准饵料（rf4map-bait:x）", False),
        )
        self.assertEqual(
            split_bait_terms(["Свежая малина 26; Малиновый джем 25", "Balsa Walker 65F-005"]),
            ["Свежая малина 26", "Малиновый джем 25", "Balsa Walker 65F-005"],
        )

    def test_rf4stat_partial_date_uses_current_year(self):
        now = datetime.fromisoformat("2026-10-19T11:00:00+08:00")
        self.assertEqual(_rf4stat_time("03:40", "19.10", now), "2026-10-19T03:40:00+08:00")
        self.assertEqual(normalize_weather("Clear (night)"), "clearNight")
        self.assertEqual(normalize_weather("Storm"), "thunderstorm")
        self.assertEqual(normalize_weather("futureWeather"), "futureWeather")

    def test_number_parser(self):
        self.assertEqual(_number("1,620 silver/hour"), 1620.0)
        self.assertEqual(_number("-3.5 °C"), -3.5)
        self.assertIsNone(_number("—"))

    def test_official_translation_never_exposes_english(self):
        self.assertEqual(_chinese_only("Zig 棒黑色 15", "饵料"), "棒黑色 15")
        self.assertEqual(_chinese_only("F1", "鱼种"), "未收录中文鱼种")

    def test_sea_coordinates_use_explicit_tool_grid(self):
        self.assertEqual(point_group("level_012_north_sea", "609:202"), "工具网格 X7-Y3")
        self.assertEqual(point_group("level_018_moskitnoe", "66:59"), "66:59")
        self.assertIsNone(point_group("level_012_north_sea", "—"))
        self.assertEqual(
            earnings_point_group("level_012_north_sea", "609:202 / 650:220"),
            "工具网格 X7-Y3",
        )
        self.assertEqual(
            earnings_point_group("level_012_north_sea", "609:202 / 750:220"),
            "拖钓路线（中心 680:211）",
        )
        self.assertEqual(
            earnings_point_group("level_011_ladoga_02", "107:470 / 300:220"),
            "拖钓路线（中心 204:345）",
        )

    def test_interactive_map_keeps_detail_text_off_the_map(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "map.png"
            Image.new("RGB", (100, 100), "navy").save(image_path)
            sample = {
                "map_image_path": str(image_path),
                "map_calibration": '{"minX":0,"minY":0,"maxX":100,"maxY":100}',
                "spot_details": '[{"coordinate":"20:30","bearingDegrees":262,"clipMeters":15,"depthMin":4.7,"depthMax":4.7}]',
                "coordinates": "20:30",
            }
            figure = build_interactive_map_figure(sample)
            self.assertIsNotNone(figure)
            self.assertEqual(figure.data[-1].mode, "markers")
            self.assertIsNone(figure.data[-1].text)
            self.assertFalse(figure.layout.annotations)

    def test_recommendation_detail_contains_points_and_observations(self):
        rows = [
            {
                "map_id": "map-a", "fish_id": "fish-a", "fish_name": "Test Fish", "fish_name_en": "Test Fish",
                "coordinate": "10:20", "clip": "15m", "game_time": "08:00", "weather_code": "clear",
                "catch_temperature": 12.0, "rig": "Rig A", "baits": "Bait A", "fish_grade": "star",
                "caught_at": "2026-10-16T01:00:00Z", "source_url": "https://example.test/1",
            },
            {
                "map_id": "map-a", "fish_id": "fish-a", "fish_name": "Test Fish", "fish_name_en": "Test Fish",
                "coordinate": "10:20", "clip": "15m", "game_time": "09:00", "weather_code": "clear",
                "catch_temperature": 14.0, "rig": "Rig A", "baits": "Bait B", "fish_grade": "star",
                "caught_at": "2026-10-16T02:00:00Z", "source_url": "https://example.test/2",
            },
        ]
        detail = build_recommendation_detail(pd.DataFrame(rows), "map-a", "Test")
        self.assertEqual(detail["sample_count"], 2)
        self.assertEqual(detail["point_count"], 1)
        self.assertEqual(detail["preferences"].iloc[0]["出现最多天气"], "晴")
        self.assertEqual(detail["points"].iloc[0]["坐标"], "10:20")

    def test_recommendation_detail_counts_named_hole_without_fake_coordinate(self):
        rows = [{
            "source_id": "hole-1", "fish_id": "fish-a", "fish_name": "鱼A", "fish_name_en": "鱼A",
            "map_id": "map-a", "coordinate": None, "spot_label": "80坑", "clip": "20m",
            "game_time": None, "weather_code": "unknown", "catch_temperature": None,
            "rig": "底钓", "baits": "饵A", "fish_grade": "star",
            "caught_at": "2026-10-20T01:00:00Z", "source_url": "https://example.test/hole",
        }]
        detail = build_recommendation_detail(pd.DataFrame(rows), "map-a", "鱼A")
        self.assertEqual(detail["point_count"], 1)
        self.assertEqual(detail["points"].iloc[0]["点位"], "80坑")
        self.assertEqual(detail["points"].iloc[0]["点位口径"], "RF4-STAT坑位")
        self.assertEqual(detail["points"].iloc[0]["坐标"], "")

    def test_recent_bait_recommendations_preserve_combinations(self):
        base = {
            "fish_id": "fish-a", "fish_name": "鱼A", "map_id": "map-a",
            "coordinate": None, "clip": None, "game_time": None, "weather_code": None,
            "catch_temperature": None, "rig": None, "source_url": "https://example.test",
        }
        rows = [
            {**base, "source_record_key": "db:1", "source_name": "RF4DB渔获", "fish_grade": "star",
             "baits": "饵A / 饵B", "rig": "海图钓组", "caught_at": "2026-10-19T01:00:00Z", "reported_at": None},
            {**base, "source_record_key": "stat:1", "source_name": "RF4-STAT上鱼记录", "fish_grade": "star",
             "baits": "饵A / 饵B", "rig": "海图钓组", "caught_at": "2026-10-19T02:00:00Z", "reported_at": None},
            {**base, "source_record_key": "post:1", "source_name": "RF4-STAT 帖子", "fish_grade": "unknown",
             "baits": "饵A / 饵B", "rig": "海图钓组", "caught_at": None, "reported_at": "2026-10-19T03:00:00Z"},
            {**base, "source_record_key": "old:1", "source_name": "RF4DB渔获", "fish_grade": "star",
             "baits": "旧饵", "caught_at": "2026-10-01T01:00:00Z", "reported_at": None},
            {**base, "source_record_key": "normal:1", "source_name": "RF4DB渔获", "fish_grade": "normal",
             "baits": "普通饵", "caught_at": "2026-10-19T04:00:00Z", "reported_at": None},
        ]
        result = build_bait_recommendations(pd.DataFrame(rows), now="2026-10-20T00:00:00Z")
        self.assertEqual(result["鱼饵组合"].tolist(), ["饵A + 饵B"])
        leading = result.iloc[0]
        self.assertEqual(leading["钓组"], "海图钓组")
        self.assertEqual(leading["近期上星记录"], 2)
        self.assertEqual(leading["近期周榜记录"], 0)
        self.assertEqual(leading["近期帖子记录"], 1)
        self.assertEqual(leading["合计来源记录"], 3)
        self.assertEqual(leading["观察状态"], "近期观察")

    def test_detail_prefers_exact_fish_name_over_substring_species(self):
        rows = pd.DataFrame([
            {"fish_name": "镜鲤（白化）", "fish_name_en": "镜鲤（白化）", "fish_id": "m_carp_albino"},
            {"fish_name": "框形镜鲤（白化）", "fish_name_en": "框形镜鲤（白化）", "fish_id": "f_carp_albino"},
        ])
        result = filter_catches_by_fish(rows, "镜鲤（白化）")
        self.assertEqual(result["fish_id"].tolist(), ["m_carp_albino"])

    def test_combined_windows_and_reason_baits_use_unique_source_records(self):
        rows = pd.DataFrame([
            {"source_record_key": "db:1", "source_name": "RF4DB渔获", "fish_grade": "normal",
             "caught_at": "2026-10-19T23:30:00Z", "reported_at": None, "baits": "POP 甜玉米 / 人造玉米", "rig": "发丝钓组"},
            {"source_record_key": "stat:1", "source_name": "RF4-STAT上鱼记录", "fish_grade": "star",
             "caught_at": "2026-10-19T23:20:00Z", "reported_at": None, "baits": "POP 甜玉米 / 人造玉米", "rig": "发丝钓组"},
            {"source_record_key": "post:1", "source_name": "RF4-STAT 帖子", "fish_grade": "unknown",
             "caught_at": None, "reported_at": "2026-10-19T19:00:00Z", "baits": "POP 甜玉米 / 人造玉米", "rig": "发丝钓组"},
            {"source_record_key": "week:1", "source_name": "RF4DB镜像周榜", "fish_grade": "weekly_record",
             "caught_at": None, "reported_at": "2026-10-19T23:40:00Z", "baits": "周榜饵", "rig": None},
        ])
        windows = build_combined_window_summary(rows, now="2026-10-20T00:00:00Z")
        one_hour = windows.iloc[0]
        six_hours = windows[windows["时间窗口"] == "6小时"].iloc[0]
        self.assertEqual((one_hour["普通/帖子样本"], one_hour["上星样本"]), (1, 1))
        self.assertEqual((six_hours["普通/帖子样本"], six_hours["上星样本"]), (2, 1))
        baits = build_reason_bait_combinations(rows, now="2026-10-20T00:00:00Z")
        self.assertEqual(baits.iloc[0]["鱼饵组合"], "POP 甜玉米 + 人造玉米")
        self.assertEqual(baits.iloc[0]["钓组"], "发丝钓组")
        self.assertEqual(baits.iloc[0]["合计样本"], 3)

    def test_complete_weekly_bait_combo_ranks_ahead_of_many_single_item_rows(self):
        base = {
            "fish_id": "m_carp_albino", "fish_name": "镜鲤（白化）", "map_id": "level_014_carp_lake",
            "coordinate": None, "clip": None, "game_time": None, "weather_code": None,
            "catch_temperature": None, "rig": None, "source_url": "https://example.test",
        }
        rows = [{
            **base, "source_record_key": "week:combo", "source_name": "RF4DB镜像·中国区普通周榜",
            "fish_grade": "weekly_record", "baits": "香子兰 20 / 新鲜蔓越莓 26",
            "caught_at": None, "reported_at": "2026-10-20T00:00:00Z",
        }]
        rows.extend({
            **base, "source_record_key": f"db:{index}", "source_name": "RF4DB渔获",
            "fish_grade": "star", "baits": "鲑鱼", "caught_at": "2026-10-19T12:00:00Z", "reported_at": None,
        } for index in range(9))
        result = build_bait_recommendations(pd.DataFrame(rows), now="2026-10-20T01:00:00Z")
        self.assertEqual(result.iloc[0]["鱼饵组合"], "香子兰 20 + 新鲜蔓越莓 26")
        self.assertEqual(result.iloc[0]["组合完整度"], "完整组合")
        self.assertEqual(result.iloc[0]["近期周榜记录"], 1)
        reason = build_reason_bait_combinations(pd.DataFrame(rows), now="2026-10-20T01:00:00Z")
        self.assertEqual(reason.iloc[0]["鱼饵组合"], "香子兰 20 + 新鲜蔓越莓 26")

    def test_habit_model_excludes_missing_weather_or_temperature(self):
        rows = [
            {
                "map_id": "map-a", "fish_id": "fish-a", "fish_name": "鱼A", "fish_name_en": "鱼A",
                "coordinate": None, "clip": None, "game_time": None, "weather_code": "unknown",
                "catch_temperature": None, "rig": "钓组", "baits": "饵料", "fish_grade": "normal",
                "caught_at": "2026-10-16 12:00", "source_url": "https://example.test/normal",
            },
            {
                "map_id": "map-a", "fish_id": "fish-a", "fish_name": "鱼A", "fish_name_en": "鱼A",
                "coordinate": "10:20", "clip": None, "game_time": "08:00", "weather_code": "clear",
                "catch_temperature": 12.0, "rig": "钓组", "baits": "饵料", "fish_grade": "star",
                "caught_at": "2026-10-16T04:00:00Z", "source_url": "https://example.test/star",
            },
        ]
        detail = build_recommendation_detail(pd.DataFrame(rows), "map-a", "鱼A")
        self.assertEqual(detail["sample_count"], 2)
        self.assertEqual(detail["condition_sample_count"], 1)
        self.assertEqual(detail["preferences"].iloc[0]["出现最多天气"], "晴")
        self.assertEqual(detail["preferences"].iloc[0]["温度样本"], 1)

    def test_habit_observation_threshold_is_15_days_and_25_samples(self):
        base = pd.Timestamp("2026-10-01T00:00:00Z")
        rows = []
        for index in range(25):
            caught_at = base + pd.to_timedelta(index % 15, unit="D") + pd.to_timedelta(index // 15, unit="h")
            rows.append({
                "map_id": "map-a", "fish_id": "fish-a", "fish_name": "鱼A", "fish_name_en": "鱼A",
                "coordinate": "10:20", "clip": None, "game_time": "08:00", "weather_code": "clear",
                "catch_temperature": 12.0 + index % 3, "rig": "钓组", "baits": "饵料", "fish_grade": "star",
                "caught_at": caught_at.isoformat(), "source_url": "https://example.test/star",
            })
        detail = build_recommendation_detail(pd.DataFrame(rows), "map-a", "鱼A")
        self.assertEqual(detail["preferences"].iloc[0]["历史覆盖天数"], 15)
        self.assertEqual(detail["preferences"].iloc[0]["结论状态"], "达到条件观察门槛（仍需用户日暴露验证）")

    def test_activity_detects_new_fish_only_with_baseline_coverage(self):
        rows = []
        for minute in (0, 10, 20):
            rows.append({
                "map_id": "map-a", "fish_id": "fish-a", "fish_name": "鱼A", "fish_grade": "normal",
                "caught_at": f"2026-10-16 16:{minute:02d}",
            })
        rows.append({
            "map_id": "map-b", "fish_id": "fish-b", "fish_name": "鱼B", "fish_grade": "normal",
            "caught_at": "2026-10-16 12:00",
        })
        activity = fish_activity_table(pd.DataFrame(rows))
        fish_a = activity[activity["fish_id"] == "fish-a"].iloc[0]
        self.assertEqual(fish_a["activity_status"], "新开始活跃")
        self.assertTrue(fish_a["newly_active"])

    def test_activity_does_not_claim_new_without_old_window(self):
        rows = [
            {"map_id": "map-a", "fish_id": "fish-a", "fish_name": "鱼A", "fish_grade": "normal", "caught_at": f"2026-10-16 16:{minute:02d}"}
            for minute in (0, 10, 20, 30, 40)
        ]
        activity = fish_activity_table(pd.DataFrame(rows))
        self.assertEqual(activity.iloc[0]["activity_status"], "当前活跃（基线积累中）")
        self.assertFalse(activity.iloc[0]["newly_active"])

    def test_six_hour_evidence_counts_normal_stars_and_top_baits(self):
        rows = [
            {"map_id": "map-a", "fish_id": "fish-a", "fish_grade": "normal", "caught_at": "2026-10-16 16:00", "baits": "玉米、甜 / 蚯蚓"},
            {"map_id": "map-a", "fish_id": "fish-a", "fish_grade": "normal", "caught_at": "2026-10-16 16:10", "baits": "蚯蚓 / 玉米、甜"},
            {"map_id": "map-a", "fish_id": "fish-a", "fish_grade": "normal", "caught_at": "2026-10-16 16:20", "baits": "面包"},
            {"map_id": "map-a", "fish_id": "fish-a", "fish_grade": "star", "caught_at": "2026-10-16T08:30:00Z", "baits": "星鱼饵"},
        ]
        evidence = recent_six_hour_evidence(pd.DataFrame(rows)).iloc[0]
        self.assertEqual(evidence["six_hour_catches"], 3)
        self.assertEqual(evidence["six_hour_stars"], 1)
        self.assertEqual(evidence["top3_baits"], "玉米、甜、蚯蚓、面包")

    def test_recent_window_evidence_has_all_windows_and_recency_score(self):
        rows = [
            {"map_id": "map-a", "fish_id": "fish-a", "fish_grade": "normal", "caught_at": "2026-10-16 16:00", "baits": "蚯蚓"},
            {"map_id": "map-a", "fish_id": "fish-a", "fish_grade": "star", "caught_at": "2026-10-16T08:30:00Z", "baits": "星鱼饵"},
            {"map_id": "map-a", "fish_id": "fish-a", "fish_grade": "normal", "caught_at": "2026-10-16 10:00", "baits": "玉米"},
            {"map_id": "map-a", "fish_id": "fish-a", "fish_grade": "normal", "caught_at": "2026-10-15 12:00", "baits": "面包"},
        ]
        evidence = recent_window_evidence(pd.DataFrame(rows)).iloc[0]
        self.assertEqual(evidence["catches_1h"], 1)
        self.assertEqual(evidence["stars_1h"], 1)
        self.assertEqual(evidence["catches_12h"], 2)
        self.assertEqual(evidence["catches_48h"], 3)
        self.assertGreater(evidence["recent_activity_score"], 0)

    def test_earnings_species_parser_reads_main_and_rest_rows(self):
        soup = BeautifulSoup(
            '''<div class="compositionCard"><ul class="earnings-species">
            <li><span class="earnings-species__photo"><i>5</i></span><a href="/en/fishes/m_carp">Mirror Carp</a><strong>30.5%</strong></li>
            </ul><details class="earnings-rest"><ul><li><a href="/en/fishes/carp">Common Carp</a><i>7</i><strong>20%</strong></li></ul></details></div>''',
            "lxml",
        )
        species = _earnings_species(soup)
        self.assertEqual(species[0], {"fish_id": "m_carp", "fish_name_en": "Mirror Carp", "count": 5, "value_pct": 30.5})
        self.assertEqual(species[1]["count"], 7)

    def test_earnings_species_mix_uses_exact_point_sample(self):
        earnings = pd.DataFrame([{
            "map_id": "map-a", "coordinates": "67:59", "catch_count": 12, "silver_per_hour": 530,
            "species_composition": '[{"fish_id":"m_carp","fish_name_en":"Mirror Carp","count":5,"value_pct":40},{"fish_id":"carp","fish_name_en":"Common Carp","count":7,"value_pct":60}]',
            "source_url": "https://example.test/hour",
        }])
        mix = build_earnings_species_mix(earnings, "map-a", "67:59")
        self.assertEqual(mix.iloc[0]["鱼种"], "普通鲤鱼")
        self.assertEqual(mix.iloc[0]["数量占比"], 58.3)


if __name__ == "__main__":
    unittest.main()
