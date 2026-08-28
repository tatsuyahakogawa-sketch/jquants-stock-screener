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

    @staticmethod
    def _sent_text(mock_send) -> str:
        """全呼び出しのメッセージ本文を結合して返す（1件ずつ個別送信される
        ため、テストでは「どこかのメッセージに含まれるか」を見れば十分な
        ことが多い）。"""
        return "\n".join(call.args[1] for call in mock_send.call_args_list)


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
        # ヘッダー1通+候補1通の計2回に分けて送信される（2026-08-27の3巡目の
        # Codexレビューで指摘・修正。まとめて1通に送っていると、2000文字超で
        # 複数リクエストに分割された際の部分失敗に弱かった）。
        self.assertEqual(mock_send.call_count, 2)
        sent_text = self._sent_text(mock_send)
        self.assertIn("1234", sent_text)
        self.assertIn("ストップ高", sent_text)

        state = self._load_state()
        self.assertIn(f"stop_high|1234|{_TODAY.isoformat()}", state["notified"])
        self.assertEqual(state["stop_high_watermark"], _TODAY.isoformat())

    def test_already_notified_hit_is_not_sent_again(self):
        self._run(stop_high=self._stop_high_hit())
        result, mock_send2 = self._run(stop_high=self._stop_high_hit())

        self.assertEqual(result, 0)
        mock_send2.assert_not_called()

    def test_failed_send_does_not_record_state(self):
        # Discord送信（ヘッダー）が失敗した場合、通知済みとして記録しては
        # ならない（次回実行時に再送を試みられるようにするため）。
        with self.assertRaises(RuntimeError):
            self._run(stop_high=self._stop_high_hit(), send_error=RuntimeError("network error"))

        self.assertFalse(self.state_path.exists())

    def test_second_candidate_send_failure_still_records_the_first(self):
        # ヘッダーと1件目の候補送信は成功し、2件目の送信で失敗した場合、
        # 1件目は既に届いているため通知済みとして記録済みでなければならず、
        # かつ2件目がまだ未送信である以上stop_high_watermarkは進めてはならない
        # （進めてしまうと、次回以降2件目の日付がウォーターマークより古く
        # なり二度と再走査されなくなる。情報源ごとの送信はtry/exceptで
        # 分離しているため、main()自体は例外を送出せずエラーとして
        # 報告する形になる。2026-08-27の2〜3巡目のCodexレビューで指摘・修正）。
        two_hits = pd.DataFrame([
            {"Code": "1234", "Date": pd.Timestamp(_TODAY), "rule": "stop_high", "detail": "ストップ高1"},
            {"Code": "5678", "Date": pd.Timestamp(_TODAY), "rule": "stop_high", "detail": "ストップ高2"},
        ])
        patches = []
        env = _DEFAULT_ENV
        self.mocks = {}
        with ExitStack() as stack:
            def _patch(target, **kwargs):
                m = stack.enter_context(patch(f"{_MOD}.{target}", **kwargs))
                self.mocks[target] = m
                return m

            stack.enter_context(patch.dict(f"{_MOD}.os.environ", env, clear=True))
            _patch("today_jst", return_value=_TODAY)
            _patch("is_market_holiday", return_value=False)
            _patch("JQuantsClient", return_value=MagicMock())
            _patch("endpoints.get_listed_info", return_value=_empty_df())
            _patch("endpoints.get_daily_quotes_range", return_value=_empty_df())
            _patch("endpoints.get_statements_range", return_value=_empty_df())
            _patch("tdnet_client.get_disclosures_range", return_value=_empty_df())
            _patch("rules.detect_stop_high", return_value=two_hits)
            _patch("rules.detect_stock_split", return_value=_empty_df())
            _patch("rules.detect_profit_growth_major", return_value=_empty_df())
            _patch("jpx_new_listings.fetch_new_listing_table", return_value=_empty_listings_df())
            # 1回目(ヘッダー)・2回目(1件目の候補)は成功、3回目(2件目の候補)で失敗
            mock_send = _patch(
                "discord_notify.send_discord_message",
                side_effect=[None, None, RuntimeError("network error"), None],
            )

            result = wan.main()

        self.assertEqual(result, 1)
        state = self._load_state()
        self.assertIn(f"stop_high|1234|{_TODAY.isoformat()}", state["notified"])
        self.assertNotIn(f"stop_high|5678|{_TODAY.isoformat()}", state["notified"])
        self.assertNotIn("stop_high_watermark", state)


class TestStopHighAndProfitGrowthIsolation(_WatchAndNotifyTestCase):
    def test_statements_fetch_failure_does_not_suppress_stop_high(self):
        # ストップ高(quotes)と経常利益急増(statements)は別のJ-Quants
        # エンドポイントを使う独立したチェックのため、片方の取得失敗が
        # もう片方の通知を巻き込んではならない（2026-08-28のCodexレビューで
        # 指摘・修正。以前は1つのtry/exceptにまとめており、statements側
        # （約425日分を1日ごとに個別リクエストする重い処理で障害が起きやすい）
        # の失敗がquotes側の通知まで消してしまっていた）。
        stop_high_hit = pd.DataFrame([
            {"Code": "1234", "Date": pd.Timestamp(_TODAY), "rule": "stop_high", "detail": "ストップ高"},
        ])
        self.mocks = {}
        with ExitStack() as stack:
            def _patch(target, **kwargs):
                m = stack.enter_context(patch(f"{_MOD}.{target}", **kwargs))
                self.mocks[target] = m
                return m

            stack.enter_context(patch.dict(f"{_MOD}.os.environ", _DEFAULT_ENV, clear=True))
            _patch("today_jst", return_value=_TODAY)
            _patch("is_market_holiday", return_value=False)
            _patch("JQuantsClient", return_value=MagicMock())
            _patch("endpoints.get_listed_info", return_value=_empty_df())
            _patch("endpoints.get_daily_quotes_range", return_value=_empty_df())
            _patch("endpoints.get_statements_range", side_effect=RuntimeError("fins/summary down"))
            _patch("tdnet_client.get_disclosures_range", return_value=_empty_df())
            _patch("rules.detect_stop_high", return_value=stop_high_hit)
            _patch("rules.detect_stock_split", return_value=_empty_df())
            _patch("jpx_new_listings.fetch_new_listing_table", return_value=_empty_listings_df())
            mock_send = _patch("discord_notify.send_discord_message")

            result = wan.main()

        self.assertEqual(result, 1)
        sent_text = self._sent_text(mock_send)
        self.assertIn("ストップ高", sent_text)
        self.assertIn("経常利益急増", sent_text)
        state = self._load_state()
        self.assertIn(f"stop_high|1234|{_TODAY.isoformat()}", state["notified"])
        self.assertEqual(state["stop_high_watermark"], _TODAY.isoformat())
        self.assertNotIn("profit_growth_watermark", state)


class TestJQuantsClientConstructionFailure(_WatchAndNotifyTestCase):
    def test_client_construction_failure_reports_once_and_sets_no_jquants_watermark(self):
        # APIキー未設定・不正等でJQuantsClient自体の構築が失敗する場合、
        # ストップ高・経常利益急増の両方に等しく影響する単一の原因のため、
        # 1つのエラーとしてまとめて報告し、どちらのwatermarkも進めない。
        self.mocks = {}
        with ExitStack() as stack:
            def _patch(target, **kwargs):
                m = stack.enter_context(patch(f"{_MOD}.{target}", **kwargs))
                self.mocks[target] = m
                return m

            stack.enter_context(patch.dict(f"{_MOD}.os.environ", _DEFAULT_ENV, clear=True))
            _patch("today_jst", return_value=_TODAY)
            _patch("is_market_holiday", return_value=False)
            _patch("JQuantsClient", side_effect=RuntimeError("invalid api key"))
            _patch("tdnet_client.get_disclosures_range", return_value=_empty_df())
            _patch("rules.detect_stock_split", return_value=_empty_df())
            _patch("jpx_new_listings.fetch_new_listing_table", return_value=_empty_listings_df())
            mock_send = _patch("discord_notify.send_discord_message")

            result = wan.main()

        self.assertEqual(result, 1)
        mock_send.assert_called_once()
        state = self._load_state()
        self.assertNotIn("stop_high_watermark", state)
        self.assertNotIn("profit_growth_watermark", state)


class TestPerSourceWatermarkCommit(_WatchAndNotifyTestCase):
    def test_send_failure_in_one_source_does_not_block_another_sources_watermark(self):
        # J-Quants側の候補は送信に成功し、TDnet側の候補は送信に失敗した場合、
        # J-Quants側のwatermarkは確定してよいが、TDnet側は確定してはならない
        # （情報源ごとに独立して送信・watermark確定するため。fetch時点での
        # 障害分離と同じ考え方をDiscord送信の失敗にも適用する。2026-08-27の
        # 3巡目のCodexレビューで指摘・修正）。
        stop_high_hit = pd.DataFrame([
            {"Code": "1234", "Date": pd.Timestamp(_TODAY), "rule": "stop_high", "detail": "ストップ高"},
        ])
        split_hit = pd.DataFrame([
            {"Code": "5678", "Date": pd.Timestamp(_TODAY), "rule": "stock_split", "detail": "分割発表"},
        ])
        patches = []
        self.mocks = {}
        with ExitStack() as stack:
            def _patch(target, **kwargs):
                m = stack.enter_context(patch(f"{_MOD}.{target}", **kwargs))
                self.mocks[target] = m
                return m

            stack.enter_context(patch.dict(f"{_MOD}.os.environ", _DEFAULT_ENV, clear=True))
            _patch("today_jst", return_value=_TODAY)
            _patch("is_market_holiday", return_value=False)
            _patch("JQuantsClient", return_value=MagicMock())
            _patch("endpoints.get_listed_info", return_value=_empty_df())
            _patch("endpoints.get_daily_quotes_range", return_value=_empty_df())
            _patch("endpoints.get_statements_range", return_value=_empty_df())
            _patch("tdnet_client.get_disclosures_range", return_value=_empty_df())
            _patch("rules.detect_stop_high", return_value=stop_high_hit)
            _patch("rules.detect_stock_split", return_value=split_hit)
            _patch("rules.detect_profit_growth_major", return_value=_empty_df())
            _patch("jpx_new_listings.fetch_new_listing_table", return_value=_empty_listings_df())
            # 1回目(ヘッダー)・2回目(jquants候補)は成功、3回目(tdnet候補)で失敗
            _patch(
                "discord_notify.send_discord_message",
                side_effect=[None, None, RuntimeError("network error"), None],
            )

            result = wan.main()

        self.assertEqual(result, 1)
        state = self._load_state()
        self.assertIn(f"stop_high|1234|{_TODAY.isoformat()}", state["notified"])
        self.assertNotIn(f"stock_split|5678|{_TODAY.isoformat()}", state["notified"])
        self.assertEqual(state["stop_high_watermark"], _TODAY.isoformat())
        self.assertNotIn("tdnet_watermark", state)


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
        # ヘッダー1通+候補1通+エラー報告1通の計3回。
        self.assertEqual(mock_send.call_count, 3)
        sent_text = self._sent_text(mock_send)
        self.assertIn("ストップ高", sent_text)
        self.assertIn("TDnet", sent_text)

        state = self._load_state()
        self.assertIn(f"stop_high|1234|{_TODAY.isoformat()}", state["notified"])
        # J-Quants側は成功しているためウォーターマークは進む。TDnet側は
        # 失敗しているため進まない（次回もこの範囲を再走査する）。
        self.assertEqual(state["stop_high_watermark"], _TODAY.isoformat())
        self.assertNotIn("tdnet_watermark", state)


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


class TestProfitGrowthLookbackAnchoredToScanStart(_WatchAndNotifyTestCase):
    def test_statements_fetch_start_uses_scan_start_not_today(self):
        # ウォーターマークによる巻き戻り走査で最も古い候補日がstart(today
        # 基準ではない)になりうるため、前年同期比較用の遡り取得もstartを
        # 基準にしないと、巻き戻りが大きい場合に最も古い候補の比較対象
        # データが取得範囲の外になり、閾値を満たす候補が「比較対象なし」で
        # 静かに見逃される（2026-08-27の3巡目のCodexレビューで指摘・修正）。
        old_watermark = (_TODAY - dt.timedelta(days=20)).isoformat()
        with self.state_path.open("w", encoding="utf-8") as f:
            json.dump({"notified": {}, "profit_growth_watermark": old_watermark}, f)

        self._run()

        statements_call = self.mocks["endpoints.get_statements_range"].call_args
        fetch_start_arg = statements_call[0][1]
        expected_start = (_TODAY - dt.timedelta(days=20)) - dt.timedelta(days=wan.PROFIT_GROWTH_LOOKBACK_DAYS)
        self.assertEqual(fetch_start_arg, expected_start)


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
        # ヘッダー1通+重複排除後の候補1通の計2回（3回ではない＝重複が
        # 1件にまとまっている）。
        self.assertEqual(mock_send.call_count, 2)
        sent_text = self._sent_text(mock_send)
        self.assertEqual(sent_text.count("1234"), 1)


class TestTdnetSameDayCorrectionIsNotDiscarded(_WatchAndNotifyTestCase):
    # TDnet由来のrule(stock_split等)はpubdateの時刻情報を保持しているため、
    # 同一銘柄・同一暦日でも時刻が異なれば別の開示として扱う。J-Quants由来の
    # rule(stop_high・profit_growth_major)は元々日付単位の情報しか無いため
    # 真夜中(00:00:00)ちょうどになり、この区別の影響を受けない
    # （2026-08-28のCodexレビューで指摘・修正。以前は日付だけをキーにしており、
    # 10:00の実行で朝の発表を通知した後、13:00までに同日中の訂正・取り下げが
    # 出ても「既に通知済みの日付」として永久に握りつぶされていた）。
    def _split_hit(self, hour: int, detail: str):
        return pd.DataFrame([
            {
                "Code": "1234",
                "Date": pd.Timestamp(_TODAY.year, _TODAY.month, _TODAY.day, hour, 0, 0),
                "rule": "stock_split",
                "detail": detail,
            },
        ])

    def test_two_different_times_same_day_are_both_sent_in_one_run(self):
        morning_and_afternoon = pd.concat([
            self._split_hit(9, "朝の発表"), self._split_hit(16, "夕方の訂正"),
        ], ignore_index=True)
        result, mock_send = self._run(split=morning_and_afternoon)

        self.assertEqual(result, 0)
        # ヘッダー1通+朝1通+夕方1通の計3回（同日でも別の候補として扱われる）。
        self.assertEqual(mock_send.call_count, 3)
        sent_text = self._sent_text(mock_send)
        self.assertIn("朝の発表", sent_text)
        self.assertIn("夕方の訂正", sent_text)

    def test_afternoon_correction_is_still_sent_after_morning_already_notified(self):
        # 10:00相当の実行で朝の発表だけを通知済みにしてから、13:00相当の
        # 実行で同日の夕方の訂正が新たに検出された状況を再現する。
        self._run(split=self._split_hit(9, "朝の発表"))

        result, mock_send2 = self._run(split=self._split_hit(16, "夕方の訂正"))

        self.assertEqual(result, 0)
        self.assertEqual(mock_send2.call_count, 2)  # ヘッダー+夕方の訂正
        sent_text = self._sent_text(mock_send2)
        self.assertIn("夕方の訂正", sent_text)
        self.assertNotIn("朝の発表", sent_text)


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
        # ヘッダー1通+承認1通+本日上場1通の計3回。
        self.assertEqual(mock_send.call_count, 3)
        sent_text = self._sent_text(mock_send)
        self.assertIn("新規上場承認", sent_text)
        self.assertIn("新規上場", sent_text)
        self.assertIn(f"上場日: {_TODAY:%Y-%m-%d}", sent_text)
        self.assertIn("634A", sent_text)

        state = self._load_state()
        self.assertEqual(state["ipo_watermark"], _TODAY.isoformat())

    def test_listing_notified_late_states_actual_listing_date_not_today(self):
        # 上場日当日の取得・送信が一時的に失敗して後日リトライされた場合、
        # 「本日新規上場」のまま実際にはtodayより前の上場日を隠してしまうと
        # 誤解を招くため、実際の上場日を明記する（2026-08-28のCodexレビューで
        # 指摘・修正）。
        actual_listing_date = _TODAY - dt.timedelta(days=2)
        listings = pd.DataFrame([
            {
                "Code": "634A", "CompanyName": "（株）レイヤード", "MarketSegment": "スタンダード",
                "ListingDate": actual_listing_date, "ApprovalDate": None,
            },
        ])
        with self.state_path.open("w", encoding="utf-8") as f:
            json.dump({"notified": {}, "ipo_watermark": actual_listing_date.isoformat()}, f)

        result, mock_send = self._run(listings=listings)

        self.assertEqual(result, 0)
        sent_text = self._sent_text(mock_send)
        self.assertNotIn("本日", sent_text)
        self.assertIn(f"上場日: {actual_listing_date:%Y-%m-%d}", sent_text)

    def test_jpx_fetch_failure_still_reports_and_fails_job(self):
        result, mock_send = self._run(fetch_listings_error=RuntimeError("scrape failed"))

        self.assertEqual(result, 1)
        mock_send.assert_called_once()
        self.assertIn("JPX新規上場会社情報の取得に失敗", mock_send.call_args[0][1])


class TestNoHitsStillPersistsWatermark(_WatchAndNotifyTestCase):
    def test_no_candidates_no_errors_still_saves_watermark(self):
        # 該当銘柄・エラーが1件も無い日でも、ウォーターマークの更新は保存
        # しなければならない（保存しないと、次回実行時にLOOKBACK_DAYSしか
        # 遡らない初回相当の挙動に戻ってしまう）。
        result, mock_send = self._run()

        self.assertEqual(result, 0)
        mock_send.assert_not_called()
        state = self._load_state()
        self.assertEqual(state["stop_high_watermark"], _TODAY.isoformat())
        self.assertEqual(state["profit_growth_watermark"], _TODAY.isoformat())
        self.assertEqual(state["tdnet_watermark"], _TODAY.isoformat())
        self.assertEqual(state["ipo_watermark"], _TODAY.isoformat())


class TestPruneState(unittest.TestCase):
    def test_plain_date_keys_are_pruned_by_cutoff(self):
        # 重複排除キーの日時部分は、TDnet由来のruleでは時刻付き
        # (YYYY-MM-DDTHH:MM:SS)、それ以外は日付のみ(YYYY-MM-DD)の
        # 2種類が混在しうる（_hits_to_candidates docstring参照）。
        # 文字列比較(key.rsplit("|",1)[-1] >= cutoff_str)がどちらの
        # 形式でも正しく機能することを確認する（2026-08-28のCodexレビューで
        # 指摘・修正した重複排除キーの精度向上に伴う回帰確認）。
        today = dt.date(2026, 8, 27)
        state = {
            "notified": {
                "stop_high|1234|2026-06-01": True,  # STATE_RETENTION_DAYS(60日)より前 -> 削除
                "stop_high|1234|2026-08-01": True,  # 60日以内 -> 残る
                "stock_split|5678|2026-06-01T09:00:00": True,  # 60日より前 -> 削除
                "stock_split|5678|2026-08-01T16:30:00": True,  # 60日以内 -> 残る
            }
        }
        wan._prune_state(state, today)
        self.assertEqual(
            set(state["notified"]),
            {"stop_high|1234|2026-08-01", "stock_split|5678|2026-08-01T16:30:00"},
        )


class TestScanStart(unittest.TestCase):
    def test_no_watermark_falls_back_to_lookback_days(self):
        today = dt.date(2026, 8, 27)
        start = wan._scan_start({}, "stop_high_watermark", today)
        self.assertEqual(start, today - dt.timedelta(days=wan.LOOKBACK_DAYS))

    def test_watermark_covers_a_gap_longer_than_lookback_days(self):
        # ゴールデンウィーク等で実行間隔がLOOKBACK_DAYSを超えて空いても、
        # 前回成功した走査の開始日から再開することで取りこぼさない
        # （2026-08-27のCodexレビューで指摘・修正）。
        today = dt.date(2026, 5, 7)
        watermark = (today - dt.timedelta(days=9)).isoformat()  # LOOKBACK_DAYS(5)より長い空白
        state = {"stop_high_watermark": watermark}
        start = wan._scan_start(state, "stop_high_watermark", today)
        self.assertEqual(start, today - dt.timedelta(days=9))

    def test_watermark_is_capped_at_max_catch_up_days(self):
        today = dt.date(2026, 8, 27)
        very_old = (today - dt.timedelta(days=200)).isoformat()
        state = {"stop_high_watermark": very_old}
        start = wan._scan_start(state, "stop_high_watermark", today)
        self.assertEqual(start, today - dt.timedelta(days=wan.MAX_CATCH_UP_DAYS))

    def test_invalid_watermark_falls_back_to_lookback_days(self):
        today = dt.date(2026, 8, 27)
        state = {"stop_high_watermark": "not-a-date"}
        start = wan._scan_start(state, "stop_high_watermark", today)
        self.assertEqual(start, today - dt.timedelta(days=wan.LOOKBACK_DAYS))


if __name__ == "__main__":
    unittest.main()
