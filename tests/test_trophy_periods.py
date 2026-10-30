import unittest

import pandas as pd

from src.trophy_periods import game_period, period_distributions


class TrophyPeriodTests(unittest.TestCase):
    def test_boundaries_and_invalid_times(self):
        for text, expected in [('04:59', '夜间'), ('05:00', '早晨'), ('10:59', '早晨'),
                               ('11:00', '午间'), ('17:00', '傍晚'), ('20:59', '傍晚'), ('21:00', '夜间'), ('00:00', '夜间')]:
            self.assertEqual(game_period(text), expected)
        for text in [None, '', '24:00', '09:60', 'date 12:00', '12']:
            self.assertIsNone(game_period(text))

    def test_dedup_missing_dates_grades_and_separate_history(self):
        def row(key, game, date, grade='star'):
            return dict(source_id=key, game_time=game, caught_at=date, fish_grade=grade)
        r = pd.DataFrame([row('a', '06:00', '2026-10-19T00:00:00Z'),
                          row('a', '06:00', '2026-10-19T00:00:00Z'),
                          row('b', '18:00', '2026-10-19T00:00:00Z'),
                          row('c', None, '2026-10-19T00:00:00Z'),
                          row('d', '22:00', '2026-10-09T00:00:00Z'),
                          row('e', '12:00', '2026-08-19T00:00:00Z'),
                          row('f', '12:00', None), row('g', '12:00', '2027-01-01T00:00:00Z'),
                          row('normal', '12:00', '2026-10-19T00:00:00Z', 'normal')])
        result = period_distributions(r, '2026-10-20T00:00:00Z')
        first, middle, old = result['blocks']
        self.assertEqual(first['eligible'], 2)
        self.assertEqual(first['missing'], 1)
        self.assertEqual(first['leaders'], '早晨、傍晚')
        self.assertEqual(first['table']['样本占比_pct'].sum(), 100)
        self.assertEqual(middle['eligible'], 1)
        self.assertEqual(old['eligible'], 1)
        self.assertEqual(result['excluded_date'], 2)

    def test_empty_is_unknown_not_zero_probability(self):
        records = pd.DataFrame(columns=['source_id', 'game_time', 'caught_at', 'fish_grade'])
        result = period_distributions(records, '2026-10-20T00:00:00Z')
        for block in result['blocks']:
            self.assertEqual(block['eligible'], 0)
            self.assertTrue(block['table']['样本占比_pct'].isna().all())


if __name__ == '__main__':
    unittest.main()
