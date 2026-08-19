"""src/jst.py の単体テスト。

Streamlit Cloud等のUTCサーバーで、JST 0:00〜8:59（=UTC前日15:00〜23:59）の
間にdate.today()を使うと前日の日付になってしまい、J-Quantsの契約期間
起点計算がずれて400エラーになる不具合（2026-08-19に実機確認）の回帰テスト。

実行方法:
    python -m unittest discover tests
"""
from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import jst


class TestTodayJst(unittest.TestCase):
    def test_rolls_over_to_next_day_while_utc_is_still_on_previous_day(self):
        # UTC 23:00 (2026-08-18)時点で、JSTは既に2026-08-19 08:00。
        # サーバーがUTCのdate.today()を使うと2026-08-18のままになってしまう
        # ケースの回帰テスト（today_jst()は正しく2026-08-19を返すべき）。
        fixed_utc = dt.datetime(2026, 8, 18, 23, 0, tzinfo=dt.timezone.utc)

        def fake_now(tz=None):
            return fixed_utc.astimezone(tz) if tz else fixed_utc

        with patch("src.jst.dt.datetime") as mock_datetime:
            mock_datetime.now.side_effect = fake_now
            result = jst.today_jst()
        self.assertEqual(result, dt.date(2026, 8, 19))

    def test_real_call_returns_a_date(self):
        # モックなしで実際に呼んでも例外にならず、date型が返ることを確認する。
        result = jst.today_jst()
        self.assertIsInstance(result, dt.date)


if __name__ == "__main__":
    unittest.main()
