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

    def _write_state(self, notified: dict, **extra) -> None:
        # last_run_schedule/last_run_dateは、watch_and_notify.pyの当日
        # 最終実行枠(16:10 JST)が完了済みであることを示すデフォルト値。
        # このガード自体を狙ったテスト（TestIsTodaysFinalNotifierRun・
        # test_early_workflow_run_trigger_skips_without_sending等）は
        # extraで明示的に上書きする。
        payload = {
            "notified": notified,
            "last_run_schedule": "10 7 * * 1-5",
            "last_run_date": _TODAY.isoformat(),
        }
        payload.update(extra)
        self.state_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _run(self, *, env=None, holiday=False):
        env = _DEFAULT_ENV if env is None else env
        # _write_stateを呼んでいない（状態ファイルがまだ無い）場合は、
        # ガードを満たすデフォルト状態を書いておく（既存の大半のテストは
        # 通知内容そのものではなくmain()の他の挙動を見ているため）。
        if not self.state_path.exists():
            self._write_state({})
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


class TestIsTodaysFinalNotifierRun(unittest.TestCase):
    def test_final_slot_schedule_today_is_true(self):
        state = {"last_run_schedule": "10 7 * * 1-5", "last_run_date": "2026-09-01"}
        self.assertTrue(sde._is_todays_final_notifier_run(state, dt.date(2026, 9, 1)))

    def test_manual_run_today_is_true(self):
        # workflow_dispatchでのwatch_and_notify.py手動実行も、意図的な
        # 当日分の実行として最終実行枠と同様に扱う。
        state = {"last_run_schedule": "manual", "last_run_date": "2026-09-01"}
        self.assertTrue(sde._is_todays_final_notifier_run(state, dt.date(2026, 9, 1)))

    def test_early_slot_schedule_is_false(self):
        # 10:00・13:00枠相当のcronはまだ当日分が揃っていないため対象外。
        state = {"last_run_schedule": "0 1 * * 1-5", "last_run_date": "2026-09-01"}
        self.assertFalse(sde._is_todays_final_notifier_run(state, dt.date(2026, 9, 1)))
        state = {"last_run_schedule": "0 4 * * 1-5", "last_run_date": "2026-09-01"}
        self.assertFalse(sde._is_todays_final_notifier_run(state, dt.date(2026, 9, 1)))

    def test_final_slot_schedule_on_a_different_day_is_false(self):
        # マーカーの日付が当日と一致しない場合（例: 前日の実行のまま
        # 更新されていない）は対象外とする。
        state = {"last_run_schedule": "10 7 * * 1-5", "last_run_date": "2026-08-31"}
        self.assertFalse(sde._is_todays_final_notifier_run(state, dt.date(2026, 9, 1)))

    def test_missing_marker_is_false(self):
        # watch_and_notify.py側がこの機能の追加より前の状態、または
        # 何らかの理由でマーカーの更新に到達しないまま終了した場合。
        self.assertFalse(sde._is_todays_final_notifier_run({}, dt.date(2026, 9, 1)))


class TestEventDateFromKey(unittest.TestCase):
    def test_date_only_suffix(self):
        self.assertEqual(
            sde._event_date_from_key("stop_high|1234|2026-08-28"), dt.date(2026, 8, 28)
        )

    def test_datetime_suffix_uses_date_part_only(self):
        self.assertEqual(
            sde._event_date_from_key("stock_split|1234|2026-08-28T15:30:00"),
            dt.date(2026, 8, 28),
        )

    def test_malformed_suffix_returns_none(self):
        self.assertIsNone(sde._event_date_from_key("stop_high|1234|not-a-date"))


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
        # 誤らないことを確認する（対象銘柄・事象自体の日付も同じ08-28、
        # つまりJST日付を跨いだ直後に同日分として送信された想定）。
        target = dt.date(2026, 8, 28)
        state = {
            "notified": {
                "stop_high|1234|2026-08-28": {
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

    def test_catch_up_entry_for_an_older_date_is_excluded(self):
        # 土日を挟んで金曜分のストップ高が月曜(target)にcatch-up送信された
        # 場合、sent_atは月曜だが対象は金曜のデータなので、月曜分のまとめ
        # メールには含めない（2026-09-29にユーザー指摘・修正）。
        target = dt.date(2026, 8, 31)  # 月曜
        state = {
            "notified": {
                "stop_high|1234|2026-08-31": {
                    "sent_at": dt.datetime(2026, 8, 31, 16, 10, tzinfo=JST).isoformat(),
                    "message": "月曜分のストップ高",
                },
                "stop_high|5678|2026-08-28": {
                    "sent_at": dt.datetime(2026, 8, 31, 13, 5, tzinfo=JST).isoformat(),
                    "message": "金曜分のcatch-upストップ高",
                },
            }
        }
        messages = sde._collect_digest_messages(state, target)
        self.assertEqual(messages, ["月曜分のストップ高"])

    def test_ipo_listed_reminder_is_kept_despite_next_day_key_date(self):
        # ipo_listed（上場前日のお知らせ）は、送信日(target)の翌営業日を
        # キーの日付に持つ（watch_and_notify.py _ipo_candidates参照）ため、
        # 他のruleと同じ日付一致条件を適用すると毎回除外されてしまう
        # （2026-09-29のCodexレビューで指摘・修正）。
        target = dt.date(2026, 8, 28)  # 金曜（例）
        listing_date = target + dt.timedelta(days=1)
        state = {
            "notified": {
                f"ipo_listed|634A|{listing_date.isoformat()}": {
                    "sent_at": dt.datetime(2026, 8, 28, 16, 10, tzinfo=JST).isoformat(),
                    "message": "🔔 上場前日のお知らせ",
                },
            }
        }
        messages = sde._collect_digest_messages(state, target)
        self.assertEqual(messages, ["🔔 上場前日のお知らせ"])

    def test_profit_growth_major_one_day_source_lag_is_kept(self):
        # 財務情報(/fins/summary)は18:00/24:30に更新されるが、
        # watch_and_notify.pyの実行は16:10 JSTが最終なため、18:00以降に
        # 確定した分は翌営業日の10:00の実行で初めて検出・送信される。
        # この場合sent_atは翌営業日(target)、キーの日付(開示日)は
        # 前営業日になるが、これは一時的な障害由来のcatch-upではなく
        # 構造上ほぼ毎回起きる正常な1日遅れなので、target分のまとめ
        # メールに含める（2026-09-29のCodexレビューで指摘・修正）。
        target = dt.date(2026, 8, 31)  # 月曜
        disclosure_date = dt.date(2026, 8, 28)  # 前営業日(金曜)
        state = {
            "notified": {
                f"profit_growth_major|1234|{disclosure_date.isoformat()}": {
                    "sent_at": dt.datetime(2026, 8, 31, 10, 0, tzinfo=JST).isoformat(),
                    "message": "経常利益急増（1日遅れ検出）",
                },
            }
        }
        messages = sde._collect_digest_messages(state, target)
        self.assertEqual(messages, ["経常利益急増（1日遅れ検出）"])

    def test_profit_growth_major_older_than_one_day_lag_is_still_excluded(self):
        # 1日遅れは許容するが、それより古い（2営業日以上前の）catch-upは
        # 依然として除外する。
        target = dt.date(2026, 8, 31)  # 月曜
        stale_date = dt.date(2026, 8, 26)  # 3営業日前
        state = {
            "notified": {
                f"profit_growth_major|1234|{stale_date.isoformat()}": {
                    "sent_at": dt.datetime(2026, 8, 31, 10, 0, tzinfo=JST).isoformat(),
                    "message": "経常利益急増（古いcatch-up）",
                },
            }
        }
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

    def test_early_workflow_run_trigger_skips_without_sending(self):
        # watch-and-notifyの10:00・13:00枠の完了イベントで起動された場合、
        # まだ当日分の最終実行枠が完了していないため何もせず終了する
        # （2026-09-30のCodexレビューで指摘・修正。以前は16:30固定cronの
        # みに頼っており、watch_and_notify.py側のschedule遅延でその
        # 16:30より後にずれ込んだ場合に当日分の一部が永久にメールから
        # 漏れうる問題があった。単純な時刻ガードでは、遅延した13:00枠を
        # 最終実行枠と誤認しうる点もCodexに指摘され、時刻ではなく
        # notify_state.json自身のマーカーで判定するよう作り直した）。
        self._write_state({}, last_run_schedule="0 4 * * 1-5", last_run_date=_TODAY.isoformat())
        result, smtp_instance = self._run()
        self.assertEqual(result, 0)
        smtp_instance.send_message.assert_not_called()

    def test_failed_or_incomplete_notifier_run_skips_without_sending(self):
        # watch-and-notify側が失敗・中断し、最終実行枠のマーカーが当日分
        # として更新されないまま終了した場合、実際には確認できていない
        # のに「該当銘柄はありませんでした」という誤った空メールを送って
        # しまわないよう、何もせず終了する（2026-09-30のCodexレビューで
        # 指摘・修正）。
        self._write_state({}, last_run_schedule="10 7 * * 1-5", last_run_date="2026-08-31")
        result, smtp_instance = self._run()
        self.assertEqual(result, 0)
        smtp_instance.send_message.assert_not_called()

    def test_force_send_bypasses_the_marker_guard(self):
        # workflow_dispatch（手動実行）はFORCE_SEND=trueでマーカーによる
        # ガードを無効化し、いつでも送信できる（動作確認・手動リカバリ用）。
        self._write_state({}, last_run_schedule="0 1 * * 1-5", last_run_date=_TODAY.isoformat())
        env = dict(_DEFAULT_ENV, FORCE_SEND="true")
        result, smtp_instance = self._run(env=env)
        self.assertEqual(result, 0)
        smtp_instance.send_message.assert_called_once()

    def test_missing_env_returns_error_without_sending(self):
        result, smtp_instance = self._run(env={})
        self.assertEqual(result, 1)
        smtp_instance.send_message.assert_not_called()

    def test_blank_recipient_list_returns_error_without_sending(self):
        env = dict(_DEFAULT_ENV, NOTIFY_EMAIL_TO=" , ,")
        result, smtp_instance = self._run(env=env)
        self.assertEqual(result, 1)
        smtp_instance.send_message.assert_not_called()

    def test_sends_digest_for_today(self):
        # 2026-09-30にユーザー指摘・変更: 前営業日分ではなく当日分を送る。
        self._write_state(
            {
                f"stop_high|1234|{_TODAY.isoformat()}": {
                    "sent_at": dt.datetime(2026, 9, 1, 16, 10, tzinfo=JST).isoformat(),
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
        self.assertIn(_TODAY.isoformat(), sent_msg["Subject"])
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
        self._write_state({})
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
