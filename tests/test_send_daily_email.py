"""scripts/send_daily_email.py の単体テスト。

実際のSMTP送信は行わず、unittest.mock.patchでsmtplib.SMTPを差し替えて
オフラインで実行する。通知済み状態ファイル(STATE_PATH)は一時ディレクトリに
差し替える。

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
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import send_daily_email as sde
from src.jst import JST

_MOD = "scripts.send_daily_email"
_TODAY = dt.date(2026, 9, 1)  # 火曜日
_DEFAULT_ENV = {
    "GMAIL_ADDRESS": "sender@example.com",
    "GMAIL_APP_PASSWORD": "app-password",
    "NOTIFY_EMAIL_TO": "to@example.com",
}


class _SendDailyEmailTestCase(unittest.TestCase):
    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.state_path = Path(tmpdir.name) / "notify_state.json"
        patcher = patch(f"{_MOD}.STATE_PATH", self.state_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_state(self, notified: dict) -> None:
        self.state_path.write_text(
            json.dumps({"notified": notified}, ensure_ascii=False), encoding="utf-8"
        )

    def _run(self, *, env=None, holiday=False):
        env = _DEFAULT_ENV if env is None else env
        self.mocks = {}
        with ExitStack() as stack:
            def _patch(target, **kwargs):
                m = stack.enter_context(patch(f"{_MOD}.{target}", **kwargs))
                self.mocks[target] = m
                return m

            stack.enter_context(patch.dict(f"{_MOD}.os.environ", env, clear=True))
            _patch("today_jst", return_value=_TODAY)
            _patch("is_market_holiday", side_effect=lambda d: holiday if d == _TODAY else False)
            smtp_cls = _patch("smtplib.SMTP")
            smtp_instance = smtp_cls.return_value
            smtp_instance.__enter__.return_value = smtp_instance
            result = sde.main()
        return result, smtp_instance


class TestParseRecipients(unittest.TestCase):
    def test_splits_and_strips_whitespace(self):
        self.assertEqual(
            sde._parse_recipients(" a@example.com, b@example.com ,c@example.com"),
            ["a@example.com", "b@example.com", "c@example.com"],
        )

    def test_single_address(self):
        self.assertEqual(sde._parse_recipients("a@example.com"), ["a@example.com"])

    def test_empty_or_blank_yields_no_recipients(self):
        self.assertEqual(sde._parse_recipients(""), [])
        self.assertEqual(sde._parse_recipients("  ,  ,"), [])


class TestPreviousBusinessDay(unittest.TestCase):
    def test_skips_weekend(self):
        # 2026-09-01(火)の前営業日は2026-08-31(月)。
        self.assertEqual(sde._previous_business_day(dt.date(2026, 9, 1)), dt.date(2026, 8, 31))

    def test_monday_skips_back_to_friday(self):
        # 2026-08-31(月)の前営業日は土日を越えて2026-08-28(金)。
        self.assertEqual(sde._previous_business_day(dt.date(2026, 8, 31)), dt.date(2026, 8, 28))


class TestCollectDigestMessages(unittest.TestCase):
    def test_filters_by_sent_at_date_in_jst(self):
        target = dt.date(2026, 8, 28)
        state = {
            "notified": {
                "stop_high|1234|2026-08-28": {
                    "sent_at": dt.datetime(2026, 8, 28, 10, 5, tzinfo=JST).isoformat(),
                    "message": "対象日のストップ高",
                },
                "stop_high|5678|2026-08-27": {
                    "sent_at": dt.datetime(2026, 8, 27, 13, 5, tzinfo=JST).isoformat(),
                    "message": "前日のストップ高",
                },
            }
        }
        messages = sde._collect_digest_messages(state, target)
        self.assertEqual(messages, ["対象日のストップ高"])

    def test_utc_sent_at_is_converted_to_jst_before_comparing(self):
        # 2026-08-27T15:30:00Z はJSTで2026-08-28 00:30。日付境界のズレを
        # 誤らないことを確認する。
        target = dt.date(2026, 8, 28)
        state = {
            "notified": {
                "stop_high|1234|2026-08-27": {
                    "sent_at": "2026-08-27T15:30:00+00:00",
                    "message": "UTC跨ぎの通知",
                },
            }
        }
        messages = sde._collect_digest_messages(state, target)
        self.assertEqual(messages, ["UTC跨ぎの通知"])

    def test_legacy_boolean_entries_are_ignored(self):
        # sent_at・messageを持たない旧形式(value=True)のエントリは無視する。
        target = dt.date(2026, 8, 28)
        state = {"notified": {"stop_high|1234|2026-08-28": True}}
        messages = sde._collect_digest_messages(state, target)
        self.assertEqual(messages, [])

    def test_ordered_by_rule_then_time(self):
        target = dt.date(2026, 8, 28)
        state = {
            "notified": {
                "profit_growth_major|1|2026-08-28": {
                    "sent_at": dt.datetime(2026, 8, 28, 10, 0, tzinfo=JST).isoformat(),
                    "message": "経常利益急増",
                },
                "stop_high|2|2026-08-28": {
                    "sent_at": dt.datetime(2026, 8, 28, 13, 0, tzinfo=JST).isoformat(),
                    "message": "ストップ高",
                },
            }
        }
        # _RULE_ORDERではstop_highがprofit_growth_majorより先。
        messages = sde._collect_digest_messages(state, target)
        self.assertEqual(messages, ["ストップ高", "経常利益急増"])


class TestBuildEmailBody(unittest.TestCase):
    def test_empty_messages_says_no_hits(self):
        body = sde._build_email_body(dt.date(2026, 8, 28), [])
        self.assertIn("該当銘柄はありませんでした。", body)

    def test_messages_are_included_in_body(self):
        body = sde._build_email_body(dt.date(2026, 8, 28), ["🔴 ストップ高\n1234 テスト株式"])
        self.assertIn("🔴 ストップ高\n1234 テスト株式", body)


class TestMain(_SendDailyEmailTestCase):
    def test_holiday_skips_without_sending(self):
        result, smtp_instance = self._run(holiday=True)
        self.assertEqual(result, 0)
        smtp_instance.send_message.assert_not_called()

    def test_missing_env_returns_error_without_sending(self):
        result, smtp_instance = self._run(env={})
        self.assertEqual(result, 1)
        smtp_instance.send_message.assert_not_called()

    def test_blank_recipient_list_returns_error_without_sending(self):
        env = dict(_DEFAULT_ENV, NOTIFY_EMAIL_TO=" , ,")
        result, smtp_instance = self._run(env=env)
        self.assertEqual(result, 1)
        smtp_instance.send_message.assert_not_called()

    def test_sends_digest_for_previous_business_day(self):
        self._write_state(
            {
                "stop_high|1234|2026-08-31": {
                    "sent_at": dt.datetime(2026, 8, 31, 10, 5, tzinfo=JST).isoformat(),
                    "message": "🔴 ストップ高\n1234 テスト株式",
                }
            }
        )
        result, smtp_instance = self._run()
        self.assertEqual(result, 0)
        smtp_instance.starttls.assert_called_once()
        smtp_instance.login.assert_called_once_with("sender@example.com", "app-password")
        smtp_instance.send_message.assert_called_once()
        sent_msg = smtp_instance.send_message.call_args[0][0]
        self.assertEqual(sent_msg["To"], "to@example.com")
        self.assertEqual(sent_msg["From"], "sender@example.com")
        self.assertIn("2026-08-31", sent_msg["Subject"])
        self.assertIn("1234 テスト株式", sent_msg.get_content())

    def test_multiple_recipients_are_split_and_all_addressed(self):
        env = dict(_DEFAULT_ENV, NOTIFY_EMAIL_TO=" a@example.com, b@example.com ,c@example.com")
        result, smtp_instance = self._run(env=env)
        self.assertEqual(result, 0)
        sent_msg = smtp_instance.send_message.call_args[0][0]
        self.assertEqual(sent_msg["To"], "a@example.com, b@example.com, c@example.com")
        self.assertEqual(
            smtp_instance.send_message.call_args.kwargs["to_addrs"],
            ["a@example.com", "b@example.com", "c@example.com"],
        )

    def test_no_hits_still_sends_confirmation_email(self):
        result, smtp_instance = self._run()
        self.assertEqual(result, 0)
        smtp_instance.send_message.assert_called_once()
        sent_msg = smtp_instance.send_message.call_args[0][0]
        self.assertIn("該当銘柄はありませんでした。", sent_msg.get_content())

    def test_smtp_failure_returns_error(self):
        with ExitStack() as stack:
            def _patch(target, **kwargs):
                return stack.enter_context(patch(f"{_MOD}.{target}", **kwargs))

            stack.enter_context(patch.dict(f"{_MOD}.os.environ", _DEFAULT_ENV, clear=True))
            _patch("today_jst", return_value=_TODAY)
            _patch("is_market_holiday", return_value=False)
            smtp_cls = _patch("smtplib.SMTP", side_effect=OSError("connection refused"))
            result = sde.main()
        self.assertEqual(result, 1)


if __name__ == "__main__":
    unittest.main()
