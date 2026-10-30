from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from src.coverage import assess_refresh


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.previous = datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc)

    def test_rf4db_streams_are_assessed_separately(self):
        rows = [
            {"source_id": "new-normal", "fish_grade": "normal", "caught_at": "2026-10-26T01:00:00Z"},
            {"source_id": "old-star", "fish_grade": "star", "caught_at": "2026-10-26T01:00:00Z"},
        ]
        with patch("src.coverage.existing_record_keys", return_value={"old-star"}):
            result = assess_refresh("rf4db_catches", rows, self.previous)
        by_stream = {item["stream"]: item for item in result}
        self.assertEqual(by_stream["rf4db_catches_normal"]["status"], "gap")
        self.assertEqual(by_stream["rf4db_catches_star"]["status"], "complete")

    def test_snapshot_never_claims_interval_complete(self):
        rows = [{"map_id": "m", "collected_at": "2026-10-26T01:00:00Z"}]
        with patch("src.coverage.existing_record_keys", return_value=set()):
            result = assess_refresh("rf4db_weather", rows, self.previous)
        self.assertEqual(result[0]["status"], "snapshot")

    def test_rf4stat_requires_overlap_for_every_map(self):
        rows = [
            {"source_key": "a-old", "map_id": "a", "reported_at": "2026-10-26T01:00:00Z"},
            {"source_key": "b-new", "map_id": "b", "reported_at": "2026-10-26T01:00:00Z"},
        ]
        with patch("src.coverage.existing_record_keys", return_value={"a-old"}):
            result = assess_refresh("rf4stat_activity", rows, self.previous)
        self.assertEqual(result[0]["status"], "gap")
        self.assertIn("1/2", result[0]["note"])


if __name__ == "__main__":
    unittest.main()
