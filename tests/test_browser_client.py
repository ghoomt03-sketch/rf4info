import unittest

from src.browser_client import BrowserFetchError, BrowserResponse, _validated_url


class BrowserClientTests(unittest.TestCase):
    def test_public_source_query_is_built_for_browser_navigation(self):
        url = _validated_url(
            "https://download.rf4db.com/zh/catch-records",
            {"page": 2, "fish": ["a", "b"]},
        )
        self.assertIn("page=2", url)
        self.assertIn("fish=a", url)
        self.assertIn("fish=b", url)

    def test_non_https_and_unknown_hosts_are_rejected(self):
        for url in ("http://download.rf4db.com/zh", "https://example.com/"):
            with self.assertRaises(BrowserFetchError):
                _validated_url(url)

    def test_browser_response_raises_browser_error(self):
        response = BrowserResponse("https://rf4map.ru/points", 403, b"denied", {})
        with self.assertRaises(BrowserFetchError) as caught:
            response.raise_for_status()
        self.assertEqual(caught.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
