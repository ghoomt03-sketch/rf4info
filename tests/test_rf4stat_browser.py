from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bs4 import BeautifulSoup

from src import rf4stat_browser as browser
from src.collectors.community import _collect_rf4stat, collect_rf4stat
from src.refresh import refresh_all


class BrowserTests(unittest.TestCase):
    def test_posts_use_the_page_network_and_do_not_retry_challenges(self):
        reader = browser.BrowserReader(browser.POSTS_URL)
        reader.page = MagicMock()
        reader.page.url = browser.POSTS_URL
        reader.page.title.return_value = '社区帖子'
        reader.page.locator.return_value.inner_text.return_value = '公开帖子'
        reader.page.evaluate.return_value = {'status': 403, 'html': '<title>安全检查</title>'}
        reader.browser = MagicMock()
        with self.assertRaisesRegex(browser.BrowserCollectionError, '帖子分页'):
            reader.posts_page(2)
        reader.page.evaluate.assert_called_once()
        self.assertEqual(reader.page.evaluate.call_args.args[1]['pageNumber'], 2)
        self.assertEqual(reader.page.evaluate.call_args.args[1]['filters'], [])
        reader.browser.contexts.__getitem__.return_value.request.post.assert_not_called()

    def test_post_pagination_filters_follow_current_public_page(self):
        self.assertEqual(browser.posts_pagination_filters('https://cn.rf4-stat.com/posts/'), [])
        self.assertEqual(
            browser.posts_pagination_filters('https://cn.rf4-stat.com/posts/?community=12&fish=a&fish=b&page=9'),
            [('community', '12'), ('fish', 'a'), ('fish', 'b')],
        )
        self.assertEqual(
            browser.posts_pagination_filters('https://cn.rf4-stat.com/community/777'),
            [('community', '777')],
        )
        self.assertIsNone(browser.posts_pagination_filters('https://cn.rf4-stat.com/posts/123'))
        self.assertIsNone(browser.posts_pagination_filters('https://evil.test/posts/'))

    def test_posts_network_error_does_not_become_empty_history(self):
        reader = browser.BrowserReader(browser.POSTS_URL)
        reader.page = MagicMock()
        reader.page.url = browser.POSTS_URL
        reader.page.title.return_value = '社区帖子'
        reader.page.locator.return_value.inner_text.return_value = '公开帖子'
        reader.page.evaluate.return_value = {'error': 'timeout'}
        with self.assertRaisesRegex(browser.BrowserCollectionError, '超时'):
            reader.posts_page(1)

    def test_challenge_detection_uses_visible_evidence(self):
        for title in ("Проверка безопасности | RF4-STAT", "安全检查 | RF4-STAT", "Just a moment..."):
            self.assertTrue(browser.is_challenge(title))
        self.assertTrue(browser.is_challenge("RF4-STAT", "请验证您是人类以访问网站"))
        self.assertFalse(browser.is_challenge("渔获 | RF4-STAT", "公开渔获数据"))

    def test_source_allowlist_rejects_other_hosts_and_ports(self):
        self.assertEqual(browser.START_URL, "https://cn.rf4-stat.com/fishing/")
        self.assertTrue(browser.is_source_url(browser.START_URL))
        for url in ("https://www.rf4-stat.ru/fishing/", "https://cn.rf4-stat.com.evil.test/fishing/", "http://cn.rf4-stat.com/",
                    "https://evil.test/?url=https://www.rf4-stat.ru", "https://www.rf4-stat.ru:9000/"):
            self.assertFalse(browser.is_source_url(url))

    def test_missing_browser_fails_without_launching_or_network(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(browser, "PROFILE_DIR", Path(directory)), patch.object(browser, "_local_json") as request:
            with self.assertRaisesRegex(browser.BrowserCollectionError, "尚未启动"):
                browser.browser_endpoint()
            request.assert_not_called()

    def test_scheduled_browser_uses_isolated_background_profile(self):
        with patch.dict(os.environ, {"RF4_BACKGROUND": "1"}):
            self.assertTrue(browser._background_mode())
            self.assertEqual(browser._profile_dir(), browser.BACKGROUND_PROFILE_DIR)
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(browser._background_mode())
            self.assertEqual(browser._profile_dir(), browser.PROFILE_DIR)

    def test_same_browser_cannot_be_used_by_two_collectors(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(browser, "DATA_DIR", Path(directory)):
            with browser.browser_lock():
                with self.assertRaisesRegex(browser.BrowserCollectionError, "另一个"):
                    with browser.browser_lock():
                        self.fail("second lock must fail")
            with browser.browser_lock():
                pass

    def test_challenge_stops_navigation_and_does_not_close_chrome(self):
        reader = browser.BrowserReader()
        reader.driver = MagicMock()
        reader.page = MagicMock()
        reader.page.goto.return_value.status = 403
        with self.assertRaisesRegex(browser.BrowserCollectionError, "验证"):
            reader.get(browser.START_URL, "test")
        reader.page.goto.assert_called_once()
        reader.__exit__()
        reader.driver.stop.assert_called_once()
        reader.page.close.assert_not_called()

    def test_challenge_with_http_200_is_not_saved_as_data(self):
        reader = browser.BrowserReader()
        reader.page = MagicMock()
        reader.page.goto.return_value.status = 200
        reader.page.url = browser.START_URL
        reader.page.title.return_value = "安全检查 | RF4-STAT"
        reader.page.locator.return_value.inner_text.return_value = "请验证您是人类"
        with self.assertRaisesRegex(browser.BrowserCollectionError, "人机验证"):
            reader.get(browser.START_URL, "test")
        reader.page.content.assert_not_called()

    def test_rate_limit_stops_without_retry(self):
        reader = browser.BrowserReader()
        reader.page = MagicMock()
        reader.page.goto.return_value.status = 429
        with self.assertRaisesRegex(browser.BrowserCollectionError, "429"):
            reader.get(browser.START_URL, "test")
        reader.page.goto.assert_called_once()

    def test_valid_dom_is_cached_and_returned(self):
        reader = browser.BrowserReader()
        reader.page = MagicMock()
        reader.page.goto.return_value.status = 200
        reader.page.url = browser.START_URL
        reader.page.title.return_value = "Fishing | RF4-STAT"
        reader.page.locator.return_value.inner_text.return_value = "Public catches"
        reader.page.content.return_value = '<table class="stat"><tr class="load-row"></tr></table>'
        with tempfile.TemporaryDirectory() as directory, patch("src.config.RAW_DIR", Path(directory)):
            soup = reader.get(browser.START_URL, "rf4stat-test")
            self.assertEqual(len(soup.select("tr.load-row")), 1)
            self.assertEqual(len(list(Path(directory).glob("*.html.gz"))), 1)

    def test_rf4stat_activity_always_uses_unified_browser_adapter(self):
        with patch("src.collectors.community._get") as get_page, patch("src.collectors.community._collect_rf4stat", return_value=[{"id": 1}]) as collect:
            self.assertEqual(collect_rf4stat(), [{"id": 1}])
            collect.assert_called_once_with(get_page, 0.25)

    def test_verification_failure_preserves_cache_and_other_sources_continue(self):
        writer = MagicMock()
        sources = {
            "rf4stat_activity": (MagicMock(side_effect=browser.BrowserCollectionError(browser.MANUAL_MESSAGE)), writer, None),
            "other": (lambda: [{"ok": True}], MagicMock(return_value=1), None),
        }
        with patch("src.refresh.SOURCES", sources), patch("src.refresh.last_success", return_value=None), \
                patch("src.refresh.log_refresh"), patch("src.refresh.assess_refresh", return_value=[]), \
                patch("src.refresh.upsert_refresh_coverage"):
            result = refresh_all(if_stale=False)
        self.assertEqual(result["rf4stat_activity"]["status"], "failed")
        self.assertEqual(result["other"]["status"], "updated")
        writer.assert_not_called()

    def test_parsed_browser_rows_keep_stable_ids_and_dedupe(self):
        html = '''<table class="stat"><tr class="load-row">
          <td><a class="share" href="/fishing/13956247">#13956247</a></td>
          <td class="fish-icon"><a href="/fishes/crucian"></a></td>
          <td class="fish"><a data-filter="fish">银鲫</a></td>
          <td class="mobile-location"><a data-filter="location">埃尔克湖</a></td>
          <td class="list-position">87:84</td>
          <td class="time"><div>14:08</div><small>19.10</small></td>
        </tr></table>'''
        soup = BeautifulSoup(html, "lxml")
        requested_urls = []

        def get_page(url, _):
            requested_urls.append(url)
            return soup

        with patch("src.collectors.community.RU_MAP_ALIASES", {"a": "x", "b": "y"}), patch("src.collectors.community.time.sleep"), patch("src.collectors.community.save_rf4stat_fish_aliases"), patch("src.collectors.community.save_bait_aliases"), patch("src.collectors.community.refresh_rf4db_fish_catalog", return_value={"crucian": "银鲫"}), patch("src.collectors.community.load_bait_aliases", return_value={}), patch("src.collectors.community.refresh_rf4db_bait_catalog", return_value={}):
            rows = _collect_rf4stat(get_page, 3)
        self.assertTrue(all(url.startswith("https://cn.rf4-stat.com/fishing/?") for url in requested_urls))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_key"], "rf4stat:13956247")
        self.assertEqual(rows[0]["map_id"], "level_019_american_pond")
        self.assertEqual(rows[0]["fish_name"], "银鲫")
        self.assertEqual(rows[0]["coordinate"], "87:84")
        self.assertEqual(rows[0]["source_url"], "https://cn.rf4-stat.com/fishing/13956247")


if __name__ == "__main__":
    unittest.main()
