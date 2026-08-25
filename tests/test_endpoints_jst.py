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


class TestGetStatementsByDateCacheKey(unittest.TestCase):
    """get_statements_by_date()の当日分キャッシュキーの回帰テスト。

    単純な日付だけをキャッシュキーにすると、18:00更新の前に一度取得して
    しまった場合、その日のうちに18:00を過ぎて再取得しても更新前の古い
    キャッシュを返し続けてしまう。「1年で売上高2倍」(sales_growth_doubling)
    の判定は"今日"を基準に毎回この関数を呼ぶため、この問題があると
    「常に最新」のはずの結果が固定されてしまう
    （2026-08-25のCodexレビューで指摘・修正）。
    """

    def _call_with_fixed_now(self, today: dt.date, fixed_now: dt.datetime):
        mock_client = MagicMock()
        mock_client.get_all_pages.return_value = iter([])
        with patch(f"{_MOD}.today_jst", return_value=today), \
                patch(f"{_MOD}.dt.datetime") as mock_datetime, \
                patch(f"{_MOD}.cache.load", return_value=None) as mock_load, \
                patch(f"{_MOD}.cache.save") as mock_save:
            mock_datetime.now.return_value = fixed_now
            endpoints.get_statements_by_date(mock_client, today)
        return mock_load, mock_save

    def test_am_bucket_before_1800(self):
        today = dt.date(2026, 8, 19)
        fixed_now = dt.datetime(2026, 8, 19, 10, 0, tzinfo=endpoints.JST)
        mock_load, mock_save = self._call_with_fixed_now(today, fixed_now)
        mock_load.assert_called_once_with("statements", "20260819_am")
        self.assertEqual(mock_save.call_args[0][1], "20260819_am")

    def test_pm_bucket_after_1800(self):
        today = dt.date(2026, 8, 19)
        fixed_now = dt.datetime(2026, 8, 19, 20, 0, tzinfo=endpoints.JST)
        mock_load, mock_save = self._call_with_fixed_now(today, fixed_now)
        mock_load.assert_called_once_with("statements", "20260819_pm")
        self.assertEqual(mock_save.call_args[0][1], "20260819_pm")

    def test_grace_window_uses_a_bucket_distinct_from_pm_and_am(self):
        # 0:00〜0:29 JSTは前日24:30更新の直後でまだ何も反映されていない
        # 可能性が高い短命な区分。ここで"pm"を再利用すると、同じ日付の
        # 18:00〜23:59の本来の"pm"区分と同じキーになってしまい、この
        # 短命な区分でキャッシュされた空/一部の結果を18:00以降も誤って
        # 使い回してしまう（2026-08-25の4巡目のCodexレビューで指摘・
        # 修正）。"am"・"pm"のどちらとも異なる専用キーになることを確認する。
        today = dt.date(2026, 8, 26)
        fixed_now = dt.datetime(2026, 8, 26, 0, 15, tzinfo=endpoints.JST)
        mock_load, mock_save = self._call_with_fixed_now(today, fixed_now)
        cache_key = mock_load.call_args[0][1]
        self.assertNotIn(cache_key, {"20260826_am", "20260826_pm"})
        self.assertEqual(cache_key, "20260826_grace")
        self.assertEqual(mock_save.call_args[0][1], "20260826_grace")

    def test_past_date_still_uses_plain_date_cache_key(self):
        # 過去日は結果が変わらないため、従来通り日付だけのキーのまま
        # （am/pmで無駄にキャッシュを分けない）。
        fixed_today = dt.date(2026, 8, 19)
        past_date = dt.date(2026, 8, 10)
        mock_client = MagicMock()
        mock_client.get_all_pages.return_value = iter([])

        with patch(f"{_MOD}.today_jst", return_value=fixed_today), \
                patch(f"{_MOD}.cache.load", return_value=None) as mock_load, \
                patch(f"{_MOD}.cache.save") as mock_save:
            endpoints.get_statements_by_date(mock_client, past_date)

        mock_load.assert_called_once_with("statements", "20260810")
        self.assertEqual(mock_save.call_args[0][1], "20260810")


if __name__ == "__main__":
    unittest.main()
