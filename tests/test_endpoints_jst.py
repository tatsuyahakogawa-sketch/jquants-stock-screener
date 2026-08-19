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


class TestFinancialsCachePeriod(unittest.TestCase):
    """決算情報は18:00と24:30(=翌0:30)の1日2回更新されるため、日付だけでなく
    どちらの更新を反映済みかもキャッシュキーに含める必要がある
    （日付だけだと18:00更新の前後で同じキーになり、更新前のデータを
    18:00以降も返し続けてしまう）。
    """

    def test_before_0030_jst_shares_previous_days_pm_bucket(self):
        # 0:00〜0:29は前日24:30更新がまだ反映されておらず、前日18:00更新
        # 時点と内容が同じなので、前日日付+"pm"を共有する。
        fixed_now = dt.datetime(2026, 8, 19, 0, 15, tzinfo=endpoints.JST)
        with patch(f"{_MOD}.dt.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            result = endpoints._financials_cache_period()
        self.assertEqual(result, "20260818_pm")

    def test_after_0030_before_1800_uses_am_bucket(self):
        fixed_now = dt.datetime(2026, 8, 19, 0, 45, tzinfo=endpoints.JST)
        with patch(f"{_MOD}.dt.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            result = endpoints._financials_cache_period()
        self.assertEqual(result, "20260819_am")

    def test_daytime_before_1800_uses_am_bucket(self):
        fixed_now = dt.datetime(2026, 8, 19, 15, 0, tzinfo=endpoints.JST)
        with patch(f"{_MOD}.dt.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            result = endpoints._financials_cache_period()
        self.assertEqual(result, "20260819_am")

    def test_after_1800_uses_pm_bucket(self):
        # 18:00更新の前後で異なるバケットになり、更新前のキャッシュを
        # 使い回さないことを確認する（今回のレビュー指摘の回帰テスト）。
        fixed_now = dt.datetime(2026, 8, 19, 18, 30, tzinfo=endpoints.JST)
        with patch(f"{_MOD}.dt.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            result = endpoints._financials_cache_period()
        self.assertEqual(result, "20260819_pm")

    def test_late_night_uses_pm_bucket(self):
        fixed_now = dt.datetime(2026, 8, 19, 23, 30, tzinfo=endpoints.JST)
        with patch(f"{_MOD}.dt.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            result = endpoints._financials_cache_period()
        self.assertEqual(result, "20260819_pm")


if __name__ == "__main__":
    unittest.main()
