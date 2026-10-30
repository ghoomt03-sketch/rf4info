from __future__ import annotations

import tempfile
import unittest
import requests
from pathlib import Path
from unittest.mock import patch

from src import db
from src.analytics import weekly_fish_activity, weekly_evidence_details
from src.collectors import official_weekly as weekly


FORM = '<input name="region" value="CN"><input name="period" value="weekly"><input name="category" value="records">'
ROW = '''<div class="rating-record"><strong>01</strong><b class="rating-record__weight">1,346.405 kg</b>
<a href="/zh/maps/level_012_north_sea">海</a><a class="rating-bait" href="/zh/wiki/lures/a">Hunter 1000-5</a>
<span class="rating-record__player">tester</span><time datetime="2026-10-19">26/10/19</time></div>'''


class WeeklyTests(unittest.TestCase):
    def test_weight_units_and_separators(self):
        for raw, expected in [('1 346.405\u00a0kg', 1346.405), ('1,346.405 kg', 1346.405),
                              ('1\u202f300 kg', 1300), ('855 g', .855), ('1,5 kg', 1.5)]:
            self.assertEqual(weekly.parse_weight(raw), expected)
        for raw in ['01 1.5', 'unknown', '2 lb', '1.2.3 kg', '1,23.4 kg', '0 kg']:
            self.assertIsNone(weekly.parse_weight(raw))

    @patch.object(weekly, 'rf4db_fish_catalog', return_value={'cetorhinus_m': '姥鲨'})
    def test_rendered_fragments_ids_and_models(self, _):
        html = FORM + '<article class="rating-fish-group"><h2><a href="/zh/fishes/cetorhinus_m">鱼</a></h2><template id="P:2"></template></article>'
        html += '<div hidden id="S:2">' + ROW + '</div><script>$RS("S:2","P:2")</script><a href="?page=2">下一页 →</a>'
        rows, nxt = weekly.parse_ratings(html, weekly.WEEKLY_URL)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['fish_name'], '姥鲨')
        self.assertEqual(rows[0]['map_name'], '挪威海')
        self.assertEqual(rows[0]['weight_kg'], 1346.405)
        self.assertEqual(rows[0]['bait_name'], 'Hunter 1000-5')
        self.assertTrue(nxt.endswith('?page=2'))

    def test_reject_wrong_region_or_partial_record(self):
        with self.assertRaises(RuntimeError):
            weekly.parse_ratings(FORM.replace('CN', 'RU'), weekly.WEEKLY_URL)
        with patch.object(weekly, 'rf4db_fish_catalog', return_value={}):
            with self.assertRaises(RuntimeError):
                weekly.parse_ratings(FORM + '<article class="rating-fish-group"><h2><a href="/zh/fishes/a">鱼</a></h2>' + ROW.replace('datetime=', 'bad=') + '</article>', weekly.WEEKLY_URL)

    @patch.object(weekly, 'rf4db_fish_catalog', return_value={})
    def test_id_without_detail_link_and_dot_id(self, _):
        for fish_id in ['bs_salmon', 'n.pike']:
            rows, _ = weekly.parse_ratings(FORM + '<article class="rating-fish-group"><header><small>' + fish_id + '</small><h2>未知目录鱼种</h2></header>' + ROW + '</article>', weekly.WEEKLY_URL)
            self.assertEqual(rows[0]['fish_id'], fish_id)

    def test_threshold_cache_preserves_unknown(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(weekly.time, 'sleep'):
            with patch.object(weekly, 'THRESHOLD_PATH', Path(directory) / 'threshold.json'), patch.object(weekly, '_get', return_value='<dl><div><dt>奖杯线</dt><dd>1300 kg</dd></div></dl>') as get:
                self.assertEqual(weekly.trophy_thresholds({'a'})['a']['kg'], 1300)
                self.assertEqual(weekly.trophy_thresholds({'a'})['a']['kg'], 1300)
                self.assertEqual(get.call_count, 1)
            with patch.object(weekly, 'THRESHOLD_PATH', Path(directory) / 'unknown.json'), patch.object(weekly, '_get', return_value='<h1>未收录</h1>'):
                self.assertIsNone(weekly.trophy_thresholds({'a'})['a']['kg'])

    def test_threshold_access_denial_stops_further_requests(self):
        response = requests.Response()
        response.status_code = 403
        with tempfile.TemporaryDirectory() as directory, patch.object(weekly, 'THRESHOLD_PATH', Path(directory) / 'threshold.json'), patch.object(weekly, '_get', side_effect=requests.HTTPError(response=response)) as get:
            self.assertEqual(weekly.trophy_thresholds({'a', 'b'}), {})
            self.assertEqual(get.call_count, 1)

    def test_db_map_fish_isolation_threshold_unknown_and_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(db, 'DATA_DIR', root), patch.object(db, 'DB_PATH', root / 'test.sqlite3'):
                base = dict(week_key='2026-10-19', region='CN', fish_name='姥鲨', weight_kg=1346.405,
                            map_name='挪威海', bait_name='Hunter', player_name='a', record_date='2026-10-19',
                            collected_at='2026-10-19T18:00:00+08:00', source_url=weekly.WEEKLY_URL,
                            fish_id='cetorhinus_m', map_id='map-a', category='records', trophy_threshold_kg=1300)
                rows = [dict(base, source_key='1'), dict(base, source_key='2', weight_kg=1200),
                        dict(base, source_key='3', trophy_threshold_kg=None),
                        dict(base, source_key='4', map_id='map-b'),
                        dict(base, source_key='legacy', fish_id=None, map_id=None)]
                self.assertEqual(db.upsert_official_weekly(rows), 5)
                activity = weekly_fish_activity().set_index('map_id')
                self.assertEqual(activity.loc['map-a', 'weekly_record_count'], 3)
                self.assertEqual(activity.loc['map-a', 'weekly_threshold_known'], 2)
                self.assertEqual(activity.loc['map-a', 'weekly_inferred_trophies'], 1)
                details = weekly_evidence_details('map-a', 'cetorhinus_m')
                self.assertEqual(set(details['达标判定']), {'按重量推定达标', '未达目录奖杯线', '门槛未知'})
                self.assertTrue((root / 'rf4-before-weekly-v2.sqlite3').exists())
                db.upsert_official_weekly([dict(base, source_key='new')])
                self.assertEqual(len(db.read_frame('SELECT * FROM official_weekly_records')), 1)
                with self.assertRaises(Exception):
                    db.upsert_official_weekly([{'source_key': 'bad', 'week_key': '2026-10-19', 'region': 'CN'}])
                self.assertEqual(db.read_frame('SELECT source_key FROM official_weekly_records').iloc[0, 0], 'new')

    def test_failed_collection_does_not_return_partial_snapshot(self):
        batch = [dict(fish_id='a', map_id='m', weight_kg=1, player_name='p', record_date='2026-10-19')]
        with patch.object(weekly, '_get', side_effect=['one', RuntimeError('page failed')]), patch.object(weekly, 'parse_ratings', return_value=(batch, 'https://download.rf4db.com/zh/ratings?page=2')), patch.object(weekly.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'page failed'):
                weekly.collect_official_weekly()

    def test_duplicate_page_and_out_of_scope_links_are_rejected(self):
        batch = [dict(fish_id='a', map_id='m', weight_kg=1, player_name='p', record_date='2026-10-19')]
        with patch.object(weekly, '_get', return_value='html'), patch.object(weekly, 'parse_ratings', return_value=(batch, 'https://download.rf4db.com/zh/ratings?page=2')), patch.object(weekly.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, '重复内容'):
                weekly.collect_official_weekly()
        with patch.object(weekly, '_get', return_value='html') as get, patch.object(weekly, 'parse_ratings', return_value=(batch, 'https://other.example/zh/ratings?page=2')), patch.object(weekly.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, '超出采集范围'):
                weekly.collect_official_weekly()
            self.assertEqual(get.call_count, 1)


if __name__ == '__main__':
    unittest.main()
