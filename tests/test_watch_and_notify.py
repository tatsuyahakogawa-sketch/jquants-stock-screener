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
        disclosures_error=None,
        send_error=None,
    ):
        """main()を、指定した戻り値/例外でモックした状態で1回実行する。

        戻り値: (main()の戻り値, send_discord_messageのモック)。
        呼び出し中に使った全モックはself.mocksからも参照できる（呼び出し
        引数の検証用）。send_error指定時はmain()が送出する例外をそのまま
        外へ伝播させる（呼び出し側でassertRaisesする）。
        """
        env = _DEFAULT_ENV if env is None else env
        self.mocks = {}
        with ExitStack() as stack:
            def _patch(target, **kwargs):
                m = stack.enter_context(patch(f"{_MOD}.{target}", **kwargs))
                self.mocks[target] = m
                return m

            stack.enter_context(patch.dict(f"{_MOD}.os.environ", env, clear=True))
            _patch("today_jst", return_value=_TODAY)
            _patch("is_market_holiday", return_value=holiday)
            _patch("JQuantsClient", return_value=MagicMock())
            _patch("endpoints.get_listed_info", return_value=_empty_df())
            _patch("endpoints.get_daily_quotes_range", return_value=_empty_df())
            _patch("endpoints.get_statements_range", return_value=_empty_df())
            if disclosures_error is not None:
                _patch("tdnet_client.get_disclosures_range", side_effect=disclosures_error)
            else:
                _patch("tdnet_client.get_disclosures_range", return_value=_empty_df())
            _patch("rules.detect_stop_high", return_value=stop_high if stop_high is not None else _empty_df())
            _patch("rules.detect_stock_split", return_value=split if split is not None else _empty_df())
            _patch(
                "rules.detect_profit_growth_major",
                return_value=profit if profit is not None else _empty_df(),
            )
            if fetch_listings_error is not None:
                _patch("jpx_new_listings.fetch_new_listing_table", side_effect=fetch_listings_error)
            else:
                _patch(
                    "jpx_new_listings.fetch_new_listing_table",
                    return_value=listings if listings is not None else _empty_listings_df(),
                )
            mock_send = _patch("discord_notify.send_discord_message")
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


class TestSourceIsolation(_WatchAndNotifyTestCase):
    def test_tdnet_failure_does_not_suppress_jquants_hit(self):
        # TDnet(株式分割/併合)のチェックが失敗しても、独立して取得している
        # J-Quants由来のストップ高通知は届く（2026-08-27のCodexレビューで
        # 指摘・修正。以前は1つのtry/exceptで両方まとめて囲んでいたため、
        # TDnetの障害がストップ高・経常利益急増の通知まで消してしまっていた）。
        stop_high_hit = pd.DataFrame([
            {"Code": "1234", "Date": pd.Timestamp(_TODAY), "rule": "stop_high", "detail": "ストップ高"},
        ])
        result, mock_send = self._run(
            stop_high=stop_high_hit, disclosures_error=RuntimeError("tdnet mirror down")
        )

        self.assertEqual(result, 1)  # TDnet側の失敗はエラーとして報告される
        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][1]
        self.assertIn("ストップ高", sent_text)
        self.assertIn("TDnet", sent_text)

        state = self._load_state()
        self.assertIn(f"stop_high|1234|{_TODAY.isoformat()}", state["notified"])


class TestQuotesExcludeToday(_WatchAndNotifyTestCase):
    def test_daily_quotes_range_end_is_yesterday(self):
        # 株価四本値は大引け後にしか当日分が更新されないため、大引け前に
        # 実行される10:00/13:00 JSTの時点で当日を含めて取得すると空の
        # レスポンスが恒久キーでキャッシュされてしまい、後で実際のデータが
        # 揃っても再取得されない（2026-08-27のCodexレビューで指摘・修正）。
        self._run()
        quotes_call = self.mocks["endpoints.get_daily_quotes_range"].call_args
        end_arg = quotes_call[0][2]
        self.assertEqual(end_arg, _TODAY - dt.timedelta(days=1))


class TestTdnetForceRefresh(_WatchAndNotifyTestCase):
    def test_disclosures_range_uses_force_refresh(self):
        # 同じ(start, today)を平日10:00と13:00の2回呼ぶため、force_refresh
        # しないと13:00の実行が10:00時点の不完全な結果を再利用してしまう
        # （2026-08-27のCodexレビューで指摘・修正）。
        self._run()
        disclosures_call = self.mocks["tdnet_client.get_disclosures_range"].call_args
        self.assertTrue(disclosures_call.kwargs.get("force_refresh"))


class TestSameRunDeduplication(_WatchAndNotifyTestCase):
    def test_two_hits_with_same_rule_code_date_produce_one_message(self):
        # 同一銘柄が同日中にoriginal disclosureと訂正の両方を出す等、1回の
        # 検出結果に同じ(rule, code, date)の行が複数混ざっていても、
        # 1通のメッセージにまとめる（stateはDiscord送信成功後にしか更新
        # されないため、state側のチェックだけでは同一実行内の重複を防げない。
        # 2026-08-27のCodexレビューで指摘・修正）。
        duplicate_split_hits = pd.DataFrame([
            {"Code": "1234", "Date": pd.Timestamp(_TODAY), "rule": "stock_split", "detail": "元の発表"},
            {"Code": "1234", "Date": pd.Timestamp(_TODAY), "rule": "stock_split", "detail": "訂正後の発表"},
        ])
        result, mock_send = self._run(split=duplicate_split_hits)

        self.assertEqual(result, 0)
        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][1]
        self.assertEqual(sent_text.count("1234"), 1)


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
