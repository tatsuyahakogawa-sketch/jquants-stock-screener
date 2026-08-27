"""src/market_calendar.py の単体テスト。

実行方法:
    python -m unittest discover tests
"""
from __future__ import annotations

import datetime as dt
import unittest

from src.market_calendar import is_market_holiday


class TestIsMarketHoliday(unittest.TestCase):
    def test_ordinary_weekday_is_not_a_holiday(self):
        self.assertFalse(is_market_holiday(dt.date(2026, 8, 27)))  # 木曜日

    def test_saturday_is_a_holiday(self):
        self.assertTrue(is_market_holiday(dt.date(2026, 8, 29)))

    def test_sunday_is_a_holiday(self):
        self.assertTrue(is_market_holiday(dt.date(2026, 8, 30)))

    def test_national_holiday_on_a_weekday_is_a_holiday(self):
        # 敬老の日（2026年は9/21・月曜）
        self.assertTrue(is_market_holiday(dt.date(2026, 9, 21)))


if __name__ == "__main__":
    unittest.main()
