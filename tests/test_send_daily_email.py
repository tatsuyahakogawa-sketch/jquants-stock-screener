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


class TestResolveTodaysFinalNotifierRun(unittest.TestCase):
    def test_final_slot_schedule_today_returns_that_date(self):
        state = {"last_run_schedule": "10 7 * * 1-5", "last_run_date": "2026-09-01"}
        self.assertEqual(
            sde._resolve_todays_final_notifier_run(state, dt.date(2026, 9, 1), None),
            dt.date(2026, 9, 1),
        )

    def test_manual_run_today_does_not_auto_trigger(self):
        # watch_and_notify.pyのworkflow_dispatchによる手動実行（動作確認・
        # 当日中の即時確認等で頻繁に行う）は、自動送信（workflow_run経由の
        # 起動）の対象にはしない。含めてしまうと、最終実行枠(16:10 JST)より
        # 前に手動実行した場合にその時点で一度送信され、後で本来の16:10枠が
        # 完了した際にも改めて送信されてしまい、同じ日に重複送信されうる
        # （このスクリプトは読み取り専用で送信済みかどうかの永続的な記録を
        # 持たないため、この重複を別途検知できない。2026-09-30のCodex
        # レビューで指摘・修正）。手動実行後に明示的に送りたい場合は
        # FORCE_SEND=trueで日次メール自体を手動実行する。
        state = {"last_run_schedule": "manual", "last_run_date": "2026-09-01"}
        self.assertIsNone(
            sde._resolve_todays_final_notifier_run(state, dt.date(2026, 9, 1), None)
        )

    def test_early_slot_schedule_is_none(self):
        # 10:00・13:00枠相当のcronはまだ当日分が揃っていないため対象外。
        state = {"last_run_schedule": "0 1 * * 1-5", "last_run_date": "2026-09-01"}
        self.assertIsNone(sde._resolve_todays_final_notifier_run(state, dt.date(2026, 9, 1), None))
        state = {"last_run_schedule": "0 4 * * 1-5", "last_run_date": "2026-09-01"}
        self.assertIsNone(sde._resolve_todays_final_notifier_run(state, dt.date(2026, 9, 1), None))

    def test_one_day_gap_is_tolerated_and_returns_the_marker_date(self):
        # watch_and_notify.py側の16:10枠実行がJST深夜を跨いでずれ込んだ
        # 場合等、このスクリプト自身のtoday_jst()とマーカーのlast_run_date
        # が厳密には一致しないことがある。前後1日までは許容し、target_dayは
        # マーカーのlast_run_dateをそのまま使う（2026-09-30のCodexレビューで
        # 指摘・修正）。
        state = {"last_run_schedule": "10 7 * * 1-5", "last_run_date": "2026-08-31"}
        self.assertEqual(
            sde._resolve_todays_final_notifier_run(state, dt.date(2026, 9, 1), None),
            dt.date(2026, 8, 31),
        )

    def test_more_than_one_day_gap_is_none(self):
        # 1日を超えるずれは「失敗・中断してマーカーが更新されないまま
        # 放置されている」可能性が高いため対象外とする。
        state = {"last_run_schedule": "10 7 * * 1-5", "last_run_date": "2026-08-28"}
        self.assertIsNone(sde._resolve_todays_final_notifier_run(state, dt.date(2026, 9, 1), None))

    def test_missing_marker_is_none(self):
        # watch_and_notify.py側がこの機能の追加より前の状態、または
        # 何らかの理由でマーカーの更新に到達しないまま終了した場合。
        self.assertIsNone(sde._resolve_todays_final_notifier_run({}, dt.date(2026, 9, 1), None))

    def test_matching_run_id_returns_the_date(self):
        state = {
            "last_run_schedule": "10 7 * * 1-5",
            "last_run_date": "2026-09-01",
            "last_run_id": "12345",
        }
        self.assertEqual(
            sde._resolve_todays_final_notifier_run(state, dt.date(2026, 9, 1), "12345"),
            dt.date(2026, 9, 1),
        )

    def test_mismatching_run_id_is_none(self):
        # 10:00・13:00枠の完了イベントに起因する起動が、ジョブ開始待ち等で
        # たまたま16:10枠のpush後に状態を読んでしまった場合、この起動を
        # 起こしたrun_id自体は16:10枠のものではないため、重複送信を防ぐため
        # 送信しない（2026-09-30のCodexレビューで指摘・修正）。
        state = {
            "last_run_schedule": "10 7 * * 1-5",
            "last_run_date": "2026-09-01",
            "last_run_id": "99999",
        }
        self.assertIsNone(
            sde._resolve_todays_final_notifier_run(state, dt.date(2026, 9, 1), "12345")
        )


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


class TestEventDatetimeFromKey(unittest.TestCase):
    def test_date_only_suffix_is_midnight(self):
        self.assertEqual(
            sde._event_datetime_from_key("profit_growth_major|1234|2026-08-28"),
            dt.datetime(2026, 8, 28, 0, 0, 0),
        )

    def test_datetime_suffix_is_preserved(self):
        self.assertEqual(
            sde._event_datetime_from_key("stock_split|1234|2026-08-28T16:30:00"),
            dt.datetime(2026, 8, 28, 16, 30, 0),
        )

    def test_malformed_suffix_returns_none(self):
        self.assertIsNone(sde._event_datetime_from_key("stop_high|1234|not-a-date"))


class TestIsWithinAllowedLag(unittest.TestCase):
    def test_same_day_is_always_allowed(self):
        target = dt.date(2026, 8, 31)
        self.assertTrue(sde._is_within_allowed_lag("stop_high|1|2026-08-31", "stop_high", target))

    def test_no_catch_up_rule_previous_day_is_rejected(self):
        target = dt.date(2026, 8, 31)
        self.assertFalse(sde._is_within_allowed_lag("stop_high|1|2026-08-28", "stop_high", target))

    def test_profit_growth_major_whole_previous_day_is_allowed(self):
        # 財務情報は日付単位の情報しか無く開示"時刻"という概念自体が無い
        # ため、前営業日全体を許容する。
        target = dt.date(2026, 8, 31)  # 月曜
        key = "profit_growth_major|1|2026-08-28"  # 前営業日(金曜)
        self.assertTrue(sde._is_within_allowed_lag(key, "profit_growth_major", target))

    def test_tdnet_disclosure_after_final_slot_is_allowed(self):
        # 前営業日の最終実行枠(16:10 JST)より後に出た開示は、正常な1日遅れ。
        target = dt.date(2026, 8, 31)  # 月曜
        key = "stock_split|1|2026-08-28T16:30:00"  # 前営業日16:30
        self.assertTrue(sde._is_within_allowed_lag(key, "stock_split", target))

    def test_tdnet_disclosure_before_final_slot_is_rejected(self):
        # 前営業日の朝の開示がTDnetミラーAPIの障害等で取りこぼされ、今日
        # catch-upで初めて検出された場合。16:10枠より前の開示なので、
        # 本来は前営業日中に検出できていたはずの一時的な取りこぼしであり、
        # 正常な1日遅れとは区別して除外する（2026-09-30のCodexレビューで
        # 指摘・修正。以前は前営業日全体を許容しており、このケースも
        # 誤って含めてしまっていた）。
        target = dt.date(2026, 8, 31)  # 月曜
        key = "stock_split|1|2026-08-28T09:00:00"  # 前営業日09:00
        self.assertFalse(sde._is_within_allowed_lag(key, "stock_split", target))

    def test_tdnet_disclosure_two_days_before_is_rejected(self):
        target = dt.date(2026, 8, 31)  # 月曜
        key = "stock_split|1|2026-08-27T17:00:00"  # 2営業日前
        self.assertFalse(sde._is_within_allowed_lag(key, "stock_split", target))


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

    def test_today_label_is_shown_by_default(self):
        body = sde._build_email_body(dt.date(2026, 8, 28), [])
        self.assertIn("（本日）", body)

    def test_today_label_is_omitted_for_a_recovered_or_delayed_date(self):
        # target_dayは常に「今日」とは限らない。JST深夜を跨ぐ遅延や
        # FORCE_SENDによる手動リカバリでは、target_dayが実行日より前の
        # 日になりうる。それでも「（本日）」と表示すると、件名・本文の
        # 対象日は過去の日付なのに本文だけ「今日のことだ」と読めてしまい
        # 紛らわしい（2026-09-30のCodexレビューで指摘・修正）。
        body = sde._build_email_body(dt.date(2026, 8, 28), [], is_today=False)
        self.assertNotIn("（本日）", body)
        self.assertIn("2026-08-28", body)

    def test_messages_are_included_in_body(self):
        body = sde._build_email_body(dt.date(2026, 8, 28), ["🔴 ストップ高\n1234 テスト株式"])
        self.assertIn("🔴 ストップ高\n1234 テスト株式", body)

    def test_empty_messages_with_error_avoids_confident_no_hits_claim(self):
        # 「確認した上で0件だった」と「一部を確認できていない」は全く
        # 違う情報。watch_and_notify.py側でいずれかの情報源の取得に
        # 失敗していた日は、0件でも「該当銘柄はありませんでした」と
        # 断定的に伝えてはならない（2026-09-30のCodexレビューで指摘・修正）。
        body = sde._build_email_body(dt.date(2026, 8, 28), [], had_error=True)
        self.assertNotIn("該当銘柄はありませんでした。", body)
        self.assertIn("完了できなかった可能性があります", body)

    def test_messages_present_with_error_still_shows_the_messages(self):
        # 一部の情報源が失敗していても、他の情報源で実際に検出・送信済み
        # の内容はそのまま正しく載せる。
        body = sde._build_email_body(
            dt.date(2026, 8, 28), ["🔴 ストップ高\n1234 テスト株式"], had_error=True
        )
        self.assertIn("🔴 ストップ高\n1234 テスト株式", body)

    def test_messages_present_with_error_still_shows_the_caveat(self):
        # 検出・送信済みの内容が一部あっても、他の情報源が失敗していた
        # ことを示す注記は省略してはならない。省略すると、一部の情報源が
        # 失敗しつつ別の情報源は成功した日に、何も欠けていない完全な
        # まとめだと誤解を与える（2026-09-30のCodexレビューで指摘・修正。
        # 以前はmessagesが空の場合にしかこの注記を出していなかった）。
        body = sde._build_email_body(
            dt.date(2026, 8, 28), ["🔴 ストップ高\n1234 テスト株式"], had_error=True
        )
        self.assertIn("完了できなかった可能性があります", body)


class TestMain(_SendDailyEmailTestCase):
    def test_holiday_skips_without_sending(self):
        result, smtp_instance = self._run(holiday=True)
        self.assertEqual(result, 0)
        smtp_instance.send_message.assert_not_called()

    def test_force_send_bypasses_the_holiday_guard(self):
        # FORCE_SEND=trueの判定を休日チェックより前に行う。以前は休日
        # チェックが先だったため、休日に動作確認・手動リカバリのつもりで
        # workflow_dispatchしても何も送らず終了してしまっていた
        # （2026-09-30のCodexレビューで指摘・修正）。
        env = dict(_DEFAULT_ENV, FORCE_SEND="true")
        result, smtp_instance = self._run(env=env, holiday=True)
        self.assertEqual(result, 0)
        smtp_instance.send_message.assert_called_once()

    def test_holiday_is_evaluated_on_the_resolved_target_day_not_today(self):
        # 金曜深夜近くまで遅延した最終実行枠の完了をきっかけに、土曜に
        # なってからこのスクリプトが起動された場合。today_jst()（土曜）
        # ではなく解決済みのtarget_day（マーカーの金曜）に対して休日判定
        # しなければならない。today基準で判定すると、前後1日許容している
        # はずのtarget_day解決が正しく機能していても、この休日チェック
        # 自体が誤って土曜日を見てスキップしてしまう
        # （2026-09-30のCodexレビューで指摘・修正）。
        friday = dt.date(2026, 8, 28)
        saturday = dt.date(2026, 8, 29)
        self._write_state(
            {
                f"stop_high|1234|{friday.isoformat()}": {
                    "sent_at": dt.datetime(2026, 8, 28, 23, 55, tzinfo=JST).isoformat(),
                    "message": "🔴 ストップ高\n1234 テスト株式",
                }
            },
            last_run_schedule="10 7 * * 1-5",
            last_run_date=friday.isoformat(),
        )
        with ExitStack() as stack:
            def _patch(target, **kwargs):
                return stack.enter_context(patch(f"{_MOD}.{target}", **kwargs))

            stack.enter_context(patch.dict(f"{_MOD}.os.environ", _DEFAULT_ENV, clear=True))
            _patch("today_jst", return_value=saturday)
            # today(土曜)は休日だが、target_day(金曜)は休日ではない。
            _patch("is_market_holiday", side_effect=lambda d: d != friday)
            smtp_cls = _patch("smtplib.SMTP")
            smtp_instance = smtp_cls.return_value
            smtp_instance.__enter__.return_value = smtp_instance
            result = sde.main()
        self.assertEqual(result, 0)
        smtp_instance.send_message.assert_called_once()
        sent_msg = smtp_instance.send_message.call_args[0][0]
        self.assertIn(friday.isoformat(), sent_msg["Subject"])

    def test_force_send_uses_the_persisted_notifier_date_for_recovery(self):
        # workflow_dispatchによる手動リカバリ（例: 前日にwatch_and_notify
        # 自体が失敗し、修正後に前日分を送り直したい場合）で、target_dayを
        # todayに固定していると本来確認したかった前日分ではなく空の
        # 「今日分」を送ってしまう。last_run_dateがあればそれを使う
        # （2026-09-30のCodexレビューで指摘・修正）。
        yesterday = _TODAY - dt.timedelta(days=1)
        self._write_state(
            {
                f"stop_high|1234|{yesterday.isoformat()}": {
                    "sent_at": dt.datetime(2026, 8, 31, 16, 10, tzinfo=JST).isoformat(),
                    "message": "🔴 ストップ高\n1234 テスト株式",
                }
            },
            last_run_date=yesterday.isoformat(),
        )
        env = dict(_DEFAULT_ENV, FORCE_SEND="true")
        result, smtp_instance = self._run(env=env)
        self.assertEqual(result, 0)
        sent_msg = smtp_instance.send_message.call_args[0][0]
        self.assertIn(yesterday.isoformat(), sent_msg["Subject"])
        self.assertIn("1234 テスト株式", sent_msg.get_content())
        # 対象日は実行日(today)ではなく前日のため「（本日）」とは表示しない。
        self.assertNotIn("（本日）", sent_msg.get_content())

    def test_recovery_date_takes_precedence_over_last_run_date(self):
        # last_run_dateはwatch_and_notify.pyの実行のたびに（最終実行枠か
        # どうかを問わず）更新される単一の値のため、前日分の送信に失敗した
        # 後、リカバリ操作前に当日の10:00・13:00枠が実行されるとlast_run_date
        # が当日に上書きされてしまい、本来リカバリしたかった前日分ではなく
        # 不完全な当日分を対象にしてしまう。RECOVERY_DATE環境変数
        # （.github/workflows/daily_email_digest.ymlのworkflow_dispatch入力
        # recovery_date）を明示的に指定できれば、last_run_dateが上書きされて
        # いてもこの問題を避けられる（2026-09-30のCodexレビューで指摘・修正）。
        two_days_ago = _TODAY - dt.timedelta(days=2)
        self._write_state(
            {
                f"stop_high|1234|{two_days_ago.isoformat()}": {
                    "sent_at": dt.datetime(2026, 8, 30, 16, 10, tzinfo=JST).isoformat(),
                    "message": "🔴 ストップ高\n1234 テスト株式",
                }
            },
            # last_run_dateは（10:00枠の実行等で）当日に上書きされている想定。
            last_run_date=_TODAY.isoformat(),
        )
        env = dict(_DEFAULT_ENV, FORCE_SEND="true", RECOVERY_DATE=two_days_ago.isoformat())
        result, smtp_instance = self._run(env=env)
        self.assertEqual(result, 0)
        sent_msg = smtp_instance.send_message.call_args[0][0]
        self.assertIn(two_days_ago.isoformat(), sent_msg["Subject"])
        self.assertIn("1234 テスト株式", sent_msg.get_content())

    def test_manual_notifier_run_does_not_auto_trigger_the_digest(self):
        # watch_and_notify.pyのworkflow_dispatchによる手動実行（動作確認・
        # 当日中の即時確認等で頻繁に行う）の完了イベントでは自動送信しない。
        # 含めてしまうと、最終実行枠(16:10 JST)より前に手動実行した場合に
        # その時点で一度送信され、後で本来の16:10枠が完了した際にも改めて
        # 送信されてしまい、同じ日に重複送信されうる（2026-09-30のCodex
        # レビューで指摘・修正）。
        self._write_state({}, last_run_schedule="manual")
        result, smtp_instance = self._run()
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
        # として更新されないまま長期間放置されている場合（1日を超える
        # ずれ）、実際には確認できていないのに「該当銘柄はありませんでした」
        # という誤った空メールを送ってしまわないよう、何もせず終了する
        # （2026-09-30のCodexレビューで指摘・修正）。
        self._write_state({}, last_run_schedule="10 7 * * 1-5", last_run_date="2026-08-28")
        result, smtp_instance = self._run()
        self.assertEqual(result, 0)
        smtp_instance.send_message.assert_not_called()

    def test_mismatching_triggering_run_id_skips_without_sending(self):
        # 10:00・13:00枠の完了イベントに起因する起動が、ジョブ開始待ち等で
        # たまたま16:10枠のpush後に状態を読んでしまった場合、この起動を
        # 起こしたrun_id自体は16:10枠のものではないため送信しない
        # （2026-09-30のCodexレビューで指摘・修正）。
        self._write_state({}, last_run_id="12345")
        env = dict(_DEFAULT_ENV, TRIGGERING_RUN_ID="99999")
        result, smtp_instance = self._run(env=env)
        self.assertEqual(result, 0)
        smtp_instance.send_message.assert_not_called()

    def test_matching_triggering_run_id_sends(self):
        self._write_state({}, last_run_id="12345")
        env = dict(_DEFAULT_ENV, TRIGGERING_RUN_ID="12345")
        result, smtp_instance = self._run(env=env)
        self.assertEqual(result, 0)
        smtp_instance.send_message.assert_called_once()

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

    def test_no_hits_with_marker_had_error_avoids_confident_no_hits_claim(self):
        # watch_and_notify.py側でstate["last_run_had_error"]=Trueが記録
        # されている日は、0件でも「該当銘柄はありませんでした」と断定的に
        # 伝えない（2026-09-30のCodexレビューで指摘・修正）。
        self._write_state({}, last_run_had_error=True)
        result, smtp_instance = self._run()
        self.assertEqual(result, 0)
        sent_msg = smtp_instance.send_message.call_args[0][0]
        self.assertNotIn("該当銘柄はありませんでした。", sent_msg.get_content())
        self.assertIn("完了できなかった可能性があります", sent_msg.get_content())

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
