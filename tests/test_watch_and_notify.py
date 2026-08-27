"""scripts/watch_and_notify.py の単体テスト。

外部API（J-Quants/TDnet/JPX/Discord）は呼ばず、unittest.mock.patchで
差し替えてオフラインで実行する。通知済み状態ファイル(STATE_PATH)は
一時ディレクトリに差し替える。

実行方法:
    python -m unittest discover tests
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import watch_and_notify as wan

_MOD = "scripts.watch_and_notify"

_TODAY = dt.date(2026, 8, 27)
_DEFAULT_ENV = {"DISCORD_WEBHOOK_URL": "https://example.com/webhook"}


def _empty_df(*columns):
    return pd.DataFrame(columns=list(columns) if columns else None)


def _empty_listings_df():
    return _empty_df("Code", "CompanyName", "MarketSegment", "ListingDate", "ApprovalDate")


class _WatchAndNotifyTestCase(unittest.TestCase):
    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.state_path = Path(tmpdir.name) / "notify_state.json"
        patcher = patch(f"{_MOD}.STATE_PATH", self.state_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(
        self,
        *,
        env=None,
        holiday=False,
        stop_high=None,
        split=None,
        profit=None,
        listings=None,
        fetch_listings_error=None,
        send_error=None,
    ):
        """main()を、指定した戻り値/例外でモックした状態で1回実行する。

        戻り値: (main()の戻り値, send_discord_messageのモック)。
        send_error指定時はmain()が送出する例外をそのまま外へ伝播させる
        （呼び出し側でassertRaisesする）。
        """
        env = _DEFAULT_ENV if env is None else env
        with ExitStack() as stack:
            stack.enter_context(patch.dict(f"{_MOD}.os.environ", env, clear=True))
            stack.enter_context(patch(f"{_MOD}.today_jst", return_value=_TODAY))
            stack.enter_context(patch(f"{_MOD}.is_market_holiday", return_value=holiday))
            stack.enter_context(patch(f"{_MOD}.JQuantsClient", return_value=MagicMock()))
            stack.enter_context(patch(f"{_MOD}.endpoints.get_listed_info", return_value=_empty_df()))
            stack.enter_context(patch(f"{_MOD}.endpoints.get_daily_quotes_range", return_value=_empty_df()))
            stack.enter_context(patch(f"{_MOD}.endpoints.get_statements_range", return_value=_empty_df()))
            stack.enter_context(patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=_empty_df()))
            stack.enter_context(
                patch(f"{_MOD}.rules.detect_stop_high", return_value=stop_high if stop_high is not None else _empty_df())
            )
            stack.enter_context(
                patch(f"{_MOD}.rules.detect_stock_split", return_value=split if split is not None else _empty_df())
            )
            stack.enter_context(
                patch(f"{_MOD}.rules.detect_profit_growth_major", return_value=profit if profit is not None else _empty_df())
            )
            if fetch_listings_error is not None:
                stack.enter_context(
                    patch(f"{_MOD}.jpx_new_listings.fetch_new_listing_table", side_effect=fetch_listings_error)
                )
            else:
                stack.enter_context(
                    patch(
                        f"{_MOD}.jpx_new_listings.fetch_new_listing_table",
                        return_value=listings if listings is not None else _empty_listings_df(),
                    )
                )
            mock_send = stack.enter_context(patch(f"{_MOD}.discord_notify.send_discord_message"))
            if send_error is not None:
                mock_send.side_effect = send_error

            result = wan.main()
        return result, mock_send

    def _load_state(self) -> dict:
        with self.state_path.open(encoding="utf-8") as f:
            return json.load(f)


class TestMarketHolidaySkip(_WatchAndNotifyTestCase):
    def test_holiday_skips_without_touching_state_or_discord(self):
        result, mock_send = self._run(holiday=True)
        self.assertEqual(result, 0)
        mock_send.assert_not_called()
        self.assertFalse(self.state_path.exists())


class TestMissingWebhookUrl(_WatchAndNotifyTestCase):
    def test_missing_webhook_url_returns_error(self):
        result, mock_send = self._run(env={})
        self.assertEqual(result, 1)
        mock_send.assert_not_called()


class TestStopHighNotification(_WatchAndNotifyTestCase):
    def _stop_high_hit(self):
        return pd.DataFrame([
            {"Code": "1234", "Date": pd.Timestamp(_TODAY), "rule": "stop_high", "detail": "ストップ高"},
        ])

    def test_new_hit_is_sent_and_recorded(self):
        result, mock_send = self._run(stop_high=self._stop_high_hit())

        self.assertEqual(result, 0)
        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][1]
        self.assertIn("1234", sent_text)
        self.assertIn("ストップ高", sent_text)

        state = self._load_state()
        self.assertIn(f"stop_high|1234|{_TODAY.isoformat()}", state["notified"])

    def test_already_notified_hit_is_not_sent_again(self):
        self._run(stop_high=self._stop_high_hit())
        result, mock_send2 = self._run(stop_high=self._stop_high_hit())

        self.assertEqual(result, 0)
        mock_send2.assert_not_called()

    def test_failed_send_does_not_record_state(self):
        # Discord送信が失敗した場合、通知済みとして記録してはならない
        # （次回実行時に再送を試みられるようにするため）。
        with self.assertRaises(RuntimeError):
            self._run(stop_high=self._stop_high_hit(), send_error=RuntimeError("network error"))

        self.assertFalse(self.state_path.exists())


class TestIpoNotifications(_WatchAndNotifyTestCase):
    def test_approval_and_listing_today_are_both_notified(self):
        listings = pd.DataFrame([
            {
                "Code": "634A", "CompanyName": "（株）レイヤード", "MarketSegment": "スタンダード",
                "ListingDate": _TODAY, "ApprovalDate": _TODAY - dt.timedelta(days=1),
            },
        ])
        result, mock_send = self._run(listings=listings)

        self.assertEqual(result, 0)
        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][1]
        self.assertIn("新規上場承認", sent_text)
        self.assertIn("本日新規上場", sent_text)
        self.assertIn("634A", sent_text)

    def test_jpx_fetch_failure_still_reports_and_fails_job(self):
        result, mock_send = self._run(fetch_listings_error=RuntimeError("scrape failed"))

        self.assertEqual(result, 1)
        mock_send.assert_called_once()
        self.assertIn("JPX新規上場会社情報の取得に失敗", mock_send.call_args[0][1])


if __name__ == "__main__":
    unittest.main()
