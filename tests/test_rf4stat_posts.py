from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from bs4 import BeautifulSoup

from src.collectors import rf4stat_posts as posts
from src.rf4stat_browser import BrowserCollectionError


def post_html(post_id=161505, age=0, position='87:84', published=True):
    timestamp = int((datetime.now(timezone.utc) - timedelta(days=age)).timestamp())
    pub = str(timestamp + 60) if published else 'bad'
    return f'''<div class="post-item" data-post-id="{post_id}" data-created-at="{timestamp}" data-published-at="{pub}">
      <div class="location"><a data-filter="location">埃尔克湖</a></div>
      <div class="spot-col"><button data-position="{position}"></button><span class="clip">012</span>
      <img class="post-depth-bobber"><span class="post-extra-depth-val">190</span></div>
      <div class="fish-list">
      <div class="fish-icon"><a href="fish/black_crappie"></a><a data-filter="fish">来源译名</a></div>
      <div class="fish-icon"><a href="fish/black_crappie"></a><a data-filter="fish">来源译名</a></div>
      <div class="fish-icon"><a href="fish/warm"></a><a data-filter="fish">太阳鱼</a></div>
      <div class="fish-icon"><a href="posts/{post_id}">16</a></div></div>
      <div class="baits"><div class="bait-icon">
      <span class="tooltip"><img src="/images/vypolzok_01.png"><span class="tooltip-data">地龙</span></span>
      <span class="tooltip"><img src="/images/cricket.png"><span class="tooltip-data">蟋蟀</span></span>
      </div><a data-filter="bait">地龙 + 蟋蟀</a></div>
      <div class="post-weather">17° 天气根据帖子时间确定：可能与实际不同</div>
      <div class="comment">作者评论仅在 Premium 中可用</div>
      </div>'''


class PostsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        for target, value in (
            ('STATE_PATH', root / 'state.json'), ('RAW_DIR', root / 'raw'),
            ('rf4db_fish_catalog', lambda: {'black_crappie': '黑斑刺盖太阳鱼', 'warm': '太阳鱼'}),
            ('rf4db_bait_catalog', lambda: {'vypolzok_01': '地龙', 'cricket': '蟋蟀'}),
        ):
            patcher = patch.object(posts, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(posts.time, 'sleep')
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def fetch(self, page):
        return BeautifulSoup(post_html(post_id=200000-page, age=page), 'lxml')

    def test_parser_keeps_coordinates_multiple_baits_and_stable_ids(self):
        rows = posts.parse_posts(BeautifulSoup(post_html(), 'lxml'), datetime.now(timezone.utc))
        self.assertEqual(len(rows), 2)  # Three displayed fish, two species, one post.
        row = rows[0]
        self.assertEqual(row['source_key'], 'rf4stat-post:161505:black_crappie')
        self.assertEqual(row['fish_name'], '黑斑刺盖太阳鱼')
        self.assertEqual(row['coordinate'], '87:84')
        self.assertEqual(row['baits'], '地龙 / 蟋蟀')
        self.assertEqual(row['rig'], '浮钓')
        self.assertEqual(row['clip'], '012')
        self.assertIsNone(row['depth'])
        self.assertIsNone(row['catch_count'])
        self.assertEqual(row['validation_status'], '有效')
        payload = json.loads(row['raw_payload'])
        self.assertEqual(payload['depth_raw_unit_unconfirmed'], '190')
        self.assertEqual(payload['time_basis'], 'published_at')
        self.assertNotIn('catch_temperature', row)
        self.assertNotIn('silver_per_hour', row)
        self.assertNotIn('Premium', row['raw_payload'])

    def test_named_hole_is_preserved_without_fake_coordinate_and_created_time_fallback(self):
        rows = posts.parse_posts(BeautifulSoup(post_html(position='55坑', published=False), 'lxml'), datetime.now(timezone.utc))
        self.assertIsNone(rows[0]['coordinate'])
        self.assertEqual(rows[0]['spot_label'], '55坑')
        self.assertEqual(rows[0]['validation_status'], '有效')
        self.assertEqual(json.loads(rows[0]['raw_payload'])['time_basis'], 'created_at')

    def test_access_failures_and_unknown_html_are_not_empty_history(self):
        for status, html in ((403, 'Forbidden'), (429, 'Rate limited'), (200, '<title>安全检查</title>'),
                             (200, '<script>load()</script>'), (200, 'Premium required'), (302, '')):
            with self.subTest(status=status, html=html):
                with self.assertRaises(BrowserCollectionError):
                    posts.parse_page_response(status, html, 2)
        self.assertFalse(posts.parse_page_response(200, '', 2).select('.post-item'))
        self.assertEqual(len(posts.parse_page_response(200, post_html(), 1).select('.post-item')), 1)

    def test_bounded_batch_and_resume_overlap(self):
        writer = MagicMock()
        first_calls = []
        def fetch(page):
            first_calls.append(page)
            return self.fetch(page)
        batch = posts.collect_pages(fetch, writer=writer)
        self.assertEqual(first_calls, list(range(1, 11)))
        self.assertEqual(len(batch), 20)
        self.assertEqual(posts.load_posts_state()['next_page'], 11)
        self.assertEqual(posts.load_posts_state()['history_status'], 'pending')
        first_calls.clear()
        posts.collect_pages(fetch, writer=writer)
        self.assertEqual(first_calls, [1, 2, 3, 10, 11, 12, 13, 14, 15, 16])
        self.assertEqual(posts.load_posts_state()['next_page'], 17)
        self.assertTrue(all(call.args[0] >= 3 for call in self.sleep.call_args_list))

    def test_failed_page_does_not_advance_cursor_or_rollback_previous_pages(self):
        calls = []
        def fetch(page):
            if page == 5:
                raise BrowserCollectionError('HTTP 403')
            calls.append(page)
            return self.fetch(page)
        writer = MagicMock()
        with self.assertRaisesRegex(RuntimeError, '已保存4个新请求页'):
            posts.collect_pages(fetch, writer=writer)
        self.assertEqual(writer.call_count, 4)
        self.assertEqual(posts.load_posts_state()['next_page'], 5)
        self.assertEqual(posts.load_posts_state()['history_status'], 'pending')
        self.assertIn('403', posts.load_posts_state()['last_error'])

    def test_failed_database_commit_does_not_advance_cursor(self):
        with self.assertRaisesRegex(RuntimeError, 'disk full'):
            posts.collect_pages(self.fetch, writer=MagicMock(side_effect=OSError('disk full')))
        self.assertEqual(posts.load_posts_state()['next_page'], 1)

    def test_duplicate_pages_are_detected(self):
        with self.assertRaisesRegex(RuntimeError, '相同内容'):
            posts.collect_pages(lambda _: self.fetch(1), writer=MagicMock())
        self.assertEqual(posts.load_posts_state()['next_page'], 2)

    def test_target_boundary_and_source_end_are_distinct(self):
        batch = posts.collect_pages(lambda _: BeautifulSoup(post_html(age=91), 'lxml'), writer=MagicMock())
        self.assertTrue(batch)
        self.assertEqual(posts.load_posts_state()['history_status'], 'target_boundary')
        posts.STATE_PATH.unlink()
        posts.collect_pages(lambda _: BeautifulSoup('', 'lxml'), writer=MagicMock())
        self.assertEqual(posts.load_posts_state()['history_status'], 'source_end')
        self.assertNotIn('oldest_at', posts.load_posts_state())

    def test_selected_source_does_not_refresh_other_sites(self):
        from src.refresh import refresh_all
        collector = MagicMock(return_value=posts.PostBatch([{'x': 1}]))
        other = MagicMock()
        with patch('src.refresh.SOURCES', {'rf4stat_posts': (collector, len, timedelta()), 'other': (other, len, timedelta())}), patch('src.refresh.last_success', return_value=None), patch('src.refresh.log_refresh'), patch('src.refresh.assess_refresh', return_value=[]), patch('src.refresh.upsert_refresh_coverage'):
            result = refresh_all(if_stale=False, sources=('rf4stat_posts',))
        self.assertEqual(result['rf4stat_posts']['rows'], 1)
        other.assert_not_called()

    def test_map_evidence_counts_one_post_once(self):
        from src.analytics import map_community_evidence_table
        import pandas as pd
        rows = posts.parse_posts(BeautifulSoup(post_html(), 'lxml'), datetime.now(timezone.utc))
        with patch('src.analytics.recent_community_spots', return_value=pd.DataFrame(rows)):
            frame = map_community_evidence_table()
        self.assertEqual(frame.iloc[0]['community_records'], 1)
        self.assertEqual(frame.iloc[0]['community_points'], 1)

    def test_database_roundtrip_dedupe_and_recent_recommendation_window(self):
        from src import db
        from src.analytics import recent_community_spots, community_spot_details
        catalog = {'black_crappie': '黑斑刺盖太阳鱼', 'warm': '太阳鱼'}
        bait_catalog = {'vypolzok_01': '地龙', 'cricket': '蟋蟀'}
        rows = posts.parse_posts(BeautifulSoup(post_html(), 'lxml'), datetime.now(timezone.utc))
        old_rows = posts.parse_posts(BeautifulSoup(post_html(post_id=160000, age=60), 'lxml'), datetime.now(timezone.utc))
        database = Path(self.directory.name) / 'test.sqlite3'
        with patch.object(db, 'DB_PATH', database), patch('src.normalization.DB_PATH', database), patch('src.normalization.rf4db_fish_catalog', return_value=catalog), patch('src.normalization.rf4db_bait_catalog', return_value=bait_catalog):
            db.upsert_community_spots(rows + old_rows)
            db.upsert_community_spots(rows)
            self.assertEqual(db.read_frame('SELECT COUNT(*) AS n FROM community_spots').iloc[0]['n'], 4)
            recent = recent_community_spots()
            self.assertEqual(set(recent['source_record_id']), {'161505'})
            self.assertTrue(recent['validation_status'].eq('有效').all())
            self.assertTrue(recent['baits'].eq('地龙 / 蟋蟀').all())
            details = community_spot_details('level_019_american_pond', '黑斑刺盖太阳鱼')
            self.assertEqual(details.iloc[0]['坐标'], '87:84')
            self.assertTrue(details.iloc[0]['来源链接'].endswith('/posts/161505'))

    def test_community_markers_do_not_form_a_route(self):
        from PIL import Image
        from src.map_render import build_interactive_map_figure, spot_table
        image_path = Path(self.directory.name) / 'map.png'
        Image.new('RGB', (100, 100)).save(image_path)
        sample = {
            'map_image_path': str(image_path),
            'map_calibration': json.dumps({'minX': 0, 'minY': 0, 'maxX': 100, 'maxY': 100}),
            'connect_points': False,
            'spot_details': json.dumps([
                {'coordinate': '87:84', 'source': posts.SOURCE_NAME, 'sourceUrl': '/posts/161505', 'clipRaw': '012'},
                {'coordinate': '84:83', 'source': posts.SOURCE_NAME},
            ]),
        }
        figure = build_interactive_map_figure(sample)
        self.assertEqual([trace.mode for trace in figure.data], ['markers'])
        self.assertEqual(spot_table(sample)[0]['来源卡线标记'], '012')
        self.assertEqual(spot_table(sample)[0]['渔获数'], '')

    def test_visible_posts_are_saved_before_pagination_challenge_without_advancing_cursor(self):
        reader = MagicMock()
        reader.visible_posts.return_value = BeautifulSoup(post_html(), 'lxml')
        reader.posts_page.side_effect = BrowserCollectionError('帖子分页安全验证')
        with patch.object(posts, 'upsert_community_spots') as writer:
            with self.assertRaises(posts.PostsCollectionError) as caught:
                posts.collect_reader(reader)
        saved = writer.call_args.args[0]
        self.assertEqual(len(saved), 2)
        self.assertEqual(caught.exception.saved_rows, 2)
        self.assertIn('窗口现有内容保存1篇', str(caught.exception))
        self.assertEqual(posts.load_posts_state()['next_page'], 1)
        self.assertEqual(posts.load_posts_state()['visible_posts'], 1)
        self.assertEqual(json.loads(saved[0]['raw_payload'])['acquisition'], 'browser_visible_dom')
        reader.posts_page.assert_called_once_with(1)

    def test_visible_snapshot_and_new_network_page_are_deduplicated(self):
        reader = MagicMock()
        reader.visible_posts.return_value = BeautifulSoup(post_html(), 'lxml')
        reader.posts_page.return_value = BeautifulSoup(post_html(), 'lxml')
        with patch.object(posts, 'upsert_community_spots'):
            batch = posts.collect_reader(reader, max_pages=1)
        self.assertEqual(len(batch), 2)
        self.assertEqual(posts.load_posts_state()['next_page'], 2)

    def test_partial_failure_reports_committed_row_count(self):
        from src.refresh import refresh_all
        error = posts.PostsCollectionError('分页受阻', saved_keys={'post:a', 'post:b'})
        with patch('src.refresh.SOURCES', {'rf4stat_posts': (MagicMock(side_effect=error), len, timedelta())}), patch('src.refresh.last_success', return_value=None), patch('src.refresh.log_refresh') as log:
            result = refresh_all(if_stale=False)
        self.assertEqual(result['rf4stat_posts']['status'], 'failed')
        self.assertEqual(result['rf4stat_posts']['rows'], 2)
        self.assertEqual(log.call_args.args[3], 2)


if __name__ == '__main__':
    unittest.main()
