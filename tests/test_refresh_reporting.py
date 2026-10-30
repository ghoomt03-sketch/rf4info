from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import refresh_data
from src.refresh_reporting import failure_message, result_state


class RefreshReportingTests(unittest.TestCase):
    def test_partial_failure_is_persisted_and_does_not_stop_other_sources(self):
        results = {
            "rf4db_weather": {"status": "updated", "rows": 144},
            "rf4stat_activity": {"status": "failed", "rows": 0, "message": "HTTP 403"},
        }
        with tempfile.TemporaryDirectory() as directory:
            status_path = Path(directory) / "status.json"
            with patch("sys.argv", ["refresh_data.py", "--status-file", str(status_path)]), patch(
                "refresh_data.refresh_all", return_value=results
            ), patch("refresh_data.collection_lock") as collection_lock, patch("builtins.print"):
                collection_lock.return_value.__enter__.return_value = True
                self.assertEqual(refresh_data.main(), 0)
            state = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(state["state"], "partial")
        self.assertFalse(state["running"])
        self.assertEqual(state["results"], results)
        self.assertIn("部分", state["stage"])
        self.assertEqual(result_state({"a": {"status": "failed"}}), "failed")
        self.assertEqual(result_state({"a": {"status": "updated"}}), "completed")

    def test_403_reason_uses_response_evidence(self):
        response = requests.Response()
        response.status_code = 403
        response.url = "https://www.rf4-stat.ru/fishing/13956247"
        response.encoding = "utf-8"
        response._content = "Проверка безопасности".encode("utf-8")
        error = requests.HTTPError(response=response)
        self.assertIn("安全/人机验证", failure_message(error))
        response._content = b"Forbidden"
        self.assertIn("具体限制原因尚未确认", failure_message(error))
        self.assertIn("请求超时", failure_message(requests.Timeout()))


if __name__ == "__main__":
    unittest.main()
