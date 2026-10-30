import unittest
from unittest.mock import patch

import pandas as pd

from src.analytics import build_recommendation_detail
from src.habit_evidence import EVIDENCE_COLUMNS, game_time_from_real, rf4stat_habit_records


class HabitEvidenceTests(unittest.TestCase):
    def test_real_minute_maps_to_game_time(self):
        self.assertEqual(game_time_from_real("2026-10-19T19:20:00+08:00"), "08:00")
        self.assertEqual(game_time_from_real("2026-10-19T19:50:00+08:00"), "20:00")
        self.assertEqual(game_time_from_real("2026-10-19T20:10:00+08:00"), "04:00")

    @patch("src.habit_evidence.read_frame")
    def test_rf4stat_time_is_offset_then_matched_to_prior_weather_snapshot(self, mocked_read):
        catches = pd.DataFrame([{
            "source_key": "rf4stat:1", "source_record_id": "1", "map_id": "map-a",
            "coordinate": None, "fish_id": "lampris_g", "fish_name": "斑点月鱼",
            "baits": "饵", "rig": None, "clip": None, "quality_label": "奖杯标记",
            "reported_at": "2026-10-19T19:40:00+08:00",
            "collected_at": "2026-10-19T19:45:00+08:00", "source_url": "https://example.test/1",
        }])
        weather = pd.DataFrame([
            {"map_id": "map-a", "forecast_slot": "2026-10-19T19:00:00+08:00",
             "weather_code": "clear", "forecast_day_temperature": 20,
             "collected_at": "2026-10-19T18:55:00+08:00", "source_url": "https://weather/old"},
            {"map_id": "map-a", "forecast_slot": "2026-10-19T19:00:00+08:00",
             "weather_code": "heavyRain", "forecast_day_temperature": 18,
             "collected_at": "2026-10-19T19:30:00+08:00", "source_url": "https://weather/future"},
        ])
        mocked_read.side_effect = [catches, weather]
        result = rf4stat_habit_records("map-a", "lampris_g").iloc[0]
        # 月亮鱼回推20分钟：19:40上报 -> 19:20中鱼 -> 游戏08:00。
        self.assertEqual(result["game_time"], "08:00")
        self.assertEqual(result["weather_code"], "clear")
        self.assertIn("18:55", result["weather_basis"])
        self.assertTrue(pd.isna(result["catch_temperature"]))

    @patch("src.analytics.all_source_habit_records")
    def test_missing_temperature_only_excludes_temperature_dimension(self, mocked_all):
        row = dict.fromkeys(EVIDENCE_COLUMNS)
        row.update({
            "source_record_key": "RF4-STAT:1", "source_name": "RF4-STAT上鱼记录",
            "fish_id": "fish-a", "fish_name": "鱼A", "fish_name_en": "鱼A", "map_id": "map-a",
            "coordinate": "10:20", "game_time": "08:00", "weather_code": "clear",
            "catch_temperature": None, "fish_grade": "star",
            "caught_at": "2026-10-19T11:20:00Z", "reported_at": "2026-10-19T11:40:00Z",
            "source_url": "https://example.test/1", "time_basis": "上报时间回推20分钟",
            "weather_basis": "历史预报快照",
        })
        mocked_all.return_value = pd.DataFrame([row])
        detail = build_recommendation_detail(pd.DataFrame(), "map-a", "鱼A")
        self.assertEqual(detail["weather_sample_count"], 1)
        self.assertEqual(detail["game_time_sample_count"], 1)
        self.assertEqual(detail["temperature_sample_count"], 0)
        self.assertEqual(detail["point_count"], 1)
        self.assertEqual(detail["preferences"].iloc[0]["出现最多天气"], "晴")
        self.assertEqual(detail["preferences"].iloc[0]["温度结论"], "即时温度样本不足")


if __name__ == "__main__":
    unittest.main()
