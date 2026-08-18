"""src/endpoints.py の日付範囲計算(JST基準)の回帰テスト。

2026-08-19に実機で確認した不具合: get_price_history_by_code()が
dt.date.today()（サーバーのローカル時刻）を使っていたため、Streamlit Cloud
のようなUTCサーバーではJST 0:00〜8:59の間に計算した「契約プラン(Light=5年)
の取得可能期間の起点」が実際より1日早くなり、J-Quantsから400エラー
（"Your subscription covers the following dates: ..."）で拒否されていた。
today_jst()を使うよう修正した後、その日付範囲がAPI呼び出しに正しく
渡っていることを確認する（J-Quantsへの実通信は行わない）。

実行方法:
    python -m unittest discover tests
"""
from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import endpoints

_MOD = "src.endpoints"


class TestGetPriceHistoryByCodeDateRange(unittest.TestCase):
    def test_uses_jst_today_not_system_local_today(self):
        # today_jst()が返す日付だけを基準に境界を計算していること
        # （dt.date.today()を直接呼んでいないこと）を確認する。
        fixed_today = dt.date(2026, 8, 19)
        mock_client = MagicMock()
        mock_client.get_all_pages.return_value = iter([])

        with patch(f"{_MOD}.today_jst", return_value=fixed_today), \
                patch(f"{_MOD}.cache.load", return_value=None), \
                patch(f"{_MOD}.cache.save"):
            endpoints.get_price_history_by_code(mock_client, "52410", lookback_years=5)

        params = mock_client.get_all_pages.call_args[0][1]
        self.assertEqual(params["from"], "20210819")
        self.assertEqual(params["to"], "20260818")

    def test_calls_today_jst_only_once(self):
        # from/to境界とキャッシュキーが別々にtoday_jst()を呼んでいると、
        # 呼び出しの間にJST日付が変わった場合に境界とキャッシュキーが
        # 食い違ってしまう。1回だけ呼んで使い回していることを確認する。
        mock_client = MagicMock()
        mock_client.get_all_pages.return_value = iter([])

        with patch(f"{_MOD}.today_jst", return_value=dt.date(2026, 8, 19)) as mock_today, \
                patch(f"{_MOD}.cache.load", return_value=None), \
                patch(f"{_MOD}.cache.save"):
            endpoints.get_price_history_by_code(mock_client, "52410", lookback_years=5)

        self.assertEqual(mock_today.call_count, 1)


class TestFinancialsCacheDate(unittest.TestCase):
    def test_before_0030_jst_uses_previous_day(self):
        # 決算情報は18:00と24:30(=翌0:30)に更新されるため、0:00〜0:29 JSTの
        # 間はまだ更新前とみなし、前日の日付をキャッシュキーに使う。
        fixed_now = dt.datetime(2026, 8, 19, 0, 15, tzinfo=endpoints.JST)
        with patch(f"{_MOD}.dt.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            result = endpoints._financials_cache_date()
        self.assertEqual(result, dt.date(2026, 8, 18))

    def test_after_0030_jst_uses_current_day(self):
        fixed_now = dt.datetime(2026, 8, 19, 0, 45, tzinfo=endpoints.JST)
        with patch(f"{_MOD}.dt.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            result = endpoints._financials_cache_date()
        self.assertEqual(result, dt.date(2026, 8, 19))

    def test_daytime_uses_current_day(self):
        fixed_now = dt.datetime(2026, 8, 19, 15, 0, tzinfo=endpoints.JST)
        with patch(f"{_MOD}.dt.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            result = endpoints._financials_cache_date()
        self.assertEqual(result, dt.date(2026, 8, 19))


if __name__ == "__main__":
    unittest.main()
