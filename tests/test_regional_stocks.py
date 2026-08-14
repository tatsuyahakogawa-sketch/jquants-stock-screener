"""src/regional_stocks.py の単体テスト。

「地方株」ページ向けの、地方単独上場企業の検出・株価取得・増分ストアを
検証する。TDnet/yfinanceへの実際のネットワークアクセスは行わない
（unittest.mock.patchで差し替える）。

実行方法:
    python -m unittest discover tests
"""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import regional_stocks

_MOD = "src.regional_stocks"


def _disclosure(id_, code, name, title, pubdate, markets_string, url="https://example.com/x.pdf"):
    return {
        "id": id_,
        "company_code": code,
        "company_name": name,
        "title": title,
        "pubdate": pubdate,
        "markets_string": markets_string,
        "document_url": url,
    }


class TestIsRegionalOnly(unittest.TestCase):
    def test_tokyo_only_is_not_regional(self):
        self.assertFalse(regional_stocks.is_regional_only("東"))

    def test_dual_listed_with_tokyo_is_not_regional_only(self):
        self.assertFalse(regional_stocks.is_regional_only("東福"))

    def test_fukuoka_only_is_regional(self):
        self.assertTrue(regional_stocks.is_regional_only("福"))

    def test_sapporo_fukuoka_dual_regional_is_regional(self):
        self.assertTrue(regional_stocks.is_regional_only("札福"))

    def test_blank_is_not_regional(self):
        self.assertFalse(regional_stocks.is_regional_only(""))
        self.assertFalse(regional_stocks.is_regional_only(None))

    def test_nan_does_not_raise(self):
        # markets_string欠損時、pandasがNaN(float)を入れることがある。
        # bool(NaN)はTrueなので、not markets_stringだけのガードでは
        # 弾けずにTypeErrorになっていた回帰を防ぐ。
        self.assertFalse(regional_stocks.is_regional_only(float("nan")))


class TestRegionalMarketsIn(unittest.TestCase):
    def test_single_market(self):
        self.assertEqual(regional_stocks.regional_markets_in("福"), ["福証"])

    def test_multiple_markets_preserve_order(self):
        self.assertEqual(regional_stocks.regional_markets_in("札福"), ["福証", "札証"])

    def test_tokyo_char_ignored(self):
        self.assertEqual(regional_stocks.regional_markets_in("東"), [])

    def test_nan_does_not_raise(self):
        self.assertEqual(regional_stocks.regional_markets_in(float("nan")), [])


class TestLegacyTicker(unittest.TestCase):
    def test_strips_only_trailing_suffix_digit(self):
        # 実コード自体が0で終わる場合にrstrip("0")で削り過ぎないことを確認
        # （9388のケースは末尾が0以外なので単純なrstripでも壊れないが、
        # 実コードが"7200"のようなケースでの回帰を防ぐための固定長スライス）。
        self.assertEqual(regional_stocks._legacy_ticker("93880"), "9388")

    def test_code_ending_in_zero_is_not_over_stripped(self):
        self.assertEqual(regional_stocks._legacy_ticker("72000"), "7200")

    def test_alphanumeric_code(self):
        self.assertEqual(regional_stocks._legacy_ticker("353A0"), "353A")

    def test_short_code_returned_as_is(self):
        self.assertEqual(regional_stocks._legacy_ticker("1234"), "1234")


class TestClassifyListingStage(unittest.TestCase):
    def test_application_stage(self):
        self.assertEqual(
            regional_stocks._classify_listing_stage("東京証券取引所新規上場申請及び福岡証券取引所本則市場へ市場変更申請のお知らせ"),
            "申請",
        )

    def test_approval_stage(self):
        self.assertEqual(
            regional_stocks._classify_listing_stage("名古屋証券取引所ネクスト市場への上場承認に関するお知らせ"),
            "承認",
        )

    def test_completed_stage(self):
        self.assertEqual(
            regional_stocks._classify_listing_stage("福岡証券取引所Fukuoka PRO Marketへの上場のお知らせ"),
            "上場",
        )

    def test_completed_dual_listing_stage(self):
        self.assertEqual(
            regional_stocks._classify_listing_stage("福岡証券取引所本則市場への重複上場に関するお知らせ"),
            "上場",
        )

    def test_unrelated_title_returns_none(self):
        self.assertIsNone(regional_stocks._classify_listing_stage("自己株式の取得結果に関するお知らせ"))

    def test_title_without_listing_keyword_returns_none(self):
        self.assertIsNone(regional_stocks._classify_listing_stage("業績予想の修正に関するお知らせ"))

    def test_delisting_application_is_not_a_new_listing(self):
        # 「上場廃止申請」は"上場"も"申請"も含むが、新規上場の申請ではない
        # （逆方向のイベント）。誤って"申請"段階と判定しないことを確認。
        self.assertIsNone(regional_stocks._classify_listing_stage("札幌証券取引所上場廃止申請に関するお知らせ"))

    def test_delisting_notice_is_not_a_completed_listing(self):
        self.assertIsNone(regional_stocks._classify_listing_stage("福岡証券取引所への上場廃止に関するお知らせ"))


class TestDetectRegionalListingEvents(unittest.TestCase):
    def test_detects_tokyo_application_from_regional_only_company(self):
        df = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ",
                        "東京証券取引所新規上場申請及び福岡証券取引所本則市場へ市場変更申請のお知らせ",
                        "2026-07-15 15:30:00", "福"),
        ])
        result = regional_stocks.detect_regional_listing_events(df)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["Stage"], "申請")
        self.assertTrue(result.iloc[0]["IsTokyoRelated"])

    def test_excludes_disclosures_from_tokyo_listed_companies(self):
        df = pd.DataFrame([
            _disclosure("1", "72030", "トヨタ", "株式分割に関するお知らせ", "2026-07-15 15:30:00", "東"),
        ])
        result = regional_stocks.detect_regional_listing_events(df)
        self.assertTrue(result.empty)

    def test_non_tokyo_regional_listing_is_not_flagged_as_tokyo_related(self):
        df = pd.DataFrame([
            _disclosure("1", "588A0", "Ｓ－アットマークテク",
                        "札幌証券取引所Sapporo PRO Frontier Market への上場のお知らせ",
                        "2026-06-30 08:00:00", "札"),
        ])
        result = regional_stocks.detect_regional_listing_events(df)
        self.assertEqual(len(result), 1)
        self.assertFalse(result.iloc[0]["IsTokyoRelated"])
        self.assertEqual(result.iloc[0]["Stage"], "上場")

    def test_empty_input_returns_empty_with_columns(self):
        result = regional_stocks.detect_regional_listing_events(pd.DataFrame())
        self.assertTrue(result.empty)
        self.assertListEqual(list(result.columns), regional_stocks._LISTING_EVENTS_COLUMNS)

    def test_unrelated_disclosure_from_regional_company_is_not_hit(self):
        df = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "自己株式の取得結果に関するお知らせ",
                        "2026-07-16 16:30:00", "福"),
        ])
        result = regional_stocks.detect_regional_listing_events(df)
        self.assertTrue(result.empty)

    def test_completed_tokyo_listing_is_caught_via_known_regional_codes(self):
        # 東証上場が完了した開示は、その時点でmarkets_stringに既に"東"が
        # 含まれる（例:"東福"）。markets_stringだけで絞り込むと取り逃すため、
        # known_regional_codesに直前まで地方単独上場だったコードを渡すことで
        # 検出できることを確認する。
        df = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "東京証券取引所への上場のお知らせ",
                        "2026-09-01 08:00:00", "東福"),
        ])
        without_context = regional_stocks.detect_regional_listing_events(df)
        self.assertTrue(without_context.empty)

        with_context = regional_stocks.detect_regional_listing_events(df, known_regional_codes={"93880"})
        self.assertEqual(len(with_context), 1)
        self.assertEqual(with_context.iloc[0]["Stage"], "上場")
        self.assertTrue(with_context.iloc[0]["IsTokyoRelated"])

    def test_unrelated_company_not_in_known_codes_with_tokyo_word_is_still_excluded(self):
        # markets_stringが地方単独でもなく、known_regional_codesにも
        # 含まれない銘柄は対象外のままであること（無関係な東証銘柄まで
        # 拾ってしまわないことの確認）。
        df = pd.DataFrame([
            _disclosure("1", "72030", "トヨタ", "東京証券取引所プライム市場への上場に関するお知らせ",
                        "2026-09-01 08:00:00", "東"),
        ])
        result = regional_stocks.detect_regional_listing_events(df, known_regional_codes={"93880"})
        self.assertTrue(result.empty)


class TestDetectRegionalMajorEvents(unittest.TestCase):
    def test_detects_tob_from_regional_only_company(self):
        df = pd.DataFrame([
            _disclosure("1", "48340", "キャリアバンク", "当社株式に対する公開買付け(TOB)に関するお知らせ",
                        "2026-05-01 15:00:00", "札"),
        ])
        result = regional_stocks.detect_regional_major_events(df)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["MatchedKeyword"], "TOB")

    def test_excludes_tokyo_listed_companies(self):
        df = pd.DataFrame([
            _disclosure("1", "72030", "トヨタ", "子会社化に関するお知らせ", "2026-05-01 15:00:00", "東"),
        ])
        result = regional_stocks.detect_regional_major_events(df)
        self.assertTrue(result.empty)

    def test_no_keyword_match_returns_empty(self):
        df = pd.DataFrame([
            _disclosure("1", "48340", "キャリアバンク", "定例の決算短信", "2026-05-01 15:00:00", "札"),
        ])
        result = regional_stocks.detect_regional_major_events(df)
        self.assertTrue(result.empty)


class TestFetchRegionalSharePrice(unittest.TestCase):
    def test_fukuoka_only_uses_yfinance_f_suffix(self):
        mock_yf = MagicMock()
        mock_yf.Ticker.return_value.fast_info = {"lastPrice": 1824.0}
        with patch.dict(sys.modules, {"yfinance": mock_yf}):
            price, note = regional_stocks.fetch_regional_share_price("93880", "福")
        self.assertEqual(price, 1824.0)
        self.assertEqual(note, "")
        mock_yf.Ticker.assert_called_once_with("9388.F")

    def test_nagoya_only_is_not_attempted(self):
        price, note = regional_stocks.fetch_regional_share_price("61110", "名")
        self.assertIsNone(price)
        self.assertIn("取得不可", note)

    def test_sapporo_only_is_not_attempted(self):
        price, note = regional_stocks.fetch_regional_share_price("48340", "札")
        self.assertIsNone(price)
        self.assertIn("取得不可", note)

    def test_yfinance_exception_is_handled(self):
        mock_yf = MagicMock()
        mock_yf.Ticker.side_effect = RuntimeError("boom")
        with patch.dict(sys.modules, {"yfinance": mock_yf}):
            price, note = regional_stocks.fetch_regional_share_price("93880", "福")
        self.assertIsNone(price)
        self.assertIn("取得不可", note)

    def test_yfinance_returns_no_price(self):
        mock_yf = MagicMock()
        mock_yf.Ticker.return_value.fast_info = {"lastPrice": None}
        with patch.dict(sys.modules, {"yfinance": mock_yf}):
            price, note = regional_stocks.fetch_regional_share_price("93880", "福")
        self.assertIsNone(price)
        self.assertIn("取得不可", note)


class TestUpdateRegionalStore(unittest.TestCase):
    """cache.pyのCACHE_DIRを一時ディレクトリに差し替えて、実際にparquet往復させる。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._cache_dir_patcher = patch("src.cache.CACHE_DIR", self._tmpdir.name)
        self._cache_dir_patcher.start()
        self._supabase_patcher = patch("src.cache._supabase_config", return_value=None)
        self._supabase_patcher.start()

    def tearDown(self):
        self._supabase_patcher.stop()
        self._cache_dir_patcher.stop()
        self._tmpdir.cleanup()

    def test_first_run_uses_bootstrap_lookback_and_persists(self):
        disclosures = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ",
                        "福岡証券取引所本則市場への重複上場に関するお知らせ",
                        "2026-08-01 08:00:00", "福"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=disclosures) as mock_get, \
                patch(f"{_MOD}.fetch_regional_share_price", return_value=(None, "取得不可（テスト）")):
            today = dt.date(2026, 8, 13)
            result = regional_stocks.update_regional_store(today=today)

        call_start, call_end = mock_get.call_args[0]
        self.assertEqual(call_end, today)
        self.assertLess(call_start, today - dt.timedelta(days=365))  # 3年遡る初回ブートストラップ
        self.assertEqual(len(result["listing_events"]), 1)
        self.assertEqual(result["company_status"].iloc[0]["Code"], "93880")

    def test_watermark_stays_one_day_behind_today(self):
        # 更新ボタンが押された当日分のTDnet開示はまだ全件公開されていない
        # 可能性があるため、ウォーターマークはtodayではなくtoday-1にする
        # （次回はtodayを再スキャンする）。
        disclosures = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "福岡証券取引所本則市場への重複上場に関するお知らせ",
                        "2026-08-01 08:00:00", "福"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=disclosures), \
                patch(f"{_MOD}.fetch_regional_share_price", return_value=(None, "取得不可（テスト）")):
            regional_stocks.update_regional_store(today=dt.date(2026, 8, 13))

        second_batch = pd.DataFrame([
            _disclosure("2", "48340", "キャリアバンク", "当社株式に対するTOBに関するお知らせ",
                        "2026-08-14 08:00:00", "札"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=second_batch) as mock_get, \
                patch(f"{_MOD}.fetch_regional_share_price", return_value=(None, "取得不可（テスト）")):
            result = regional_stocks.update_regional_store(today=dt.date(2026, 8, 14))

        call_start, _call_end = mock_get.call_args[0]
        # 前回のwatermark=8/12(=today-1)の翌日=8/13から再取得する（8/13を再スキャン）
        self.assertEqual(call_start, dt.date(2026, 8, 13))
        # 1回目の検出結果が消えずに残っていること（増分マージの確認）
        self.assertEqual(len(result["listing_events"]), 1)
        self.assertEqual(len(result["major_events"]), 1)
        self.assertEqual(set(result["company_status"]["Code"]), {"93880", "48340"})

    def test_load_regional_store_without_update_reads_persisted_data(self):
        disclosures = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "福岡証券取引所本則市場への重複上場に関するお知らせ",
                        "2026-08-01 08:00:00", "福"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=disclosures), \
                patch(f"{_MOD}.fetch_regional_share_price", return_value=(None, "取得不可（テスト）")):
            regional_stocks.update_regional_store(today=dt.date(2026, 8, 13))

        store = regional_stocks.load_regional_store()
        self.assertEqual(len(store["listing_events"]), 1)
        self.assertEqual(store["company_status"].iloc[0]["Code"], "93880")

    def test_empty_store_round_trip_keeps_expected_columns(self):
        # 0件の結果を保存・再読込した場合でも、列名が失われず後続処理が
        # KeyErrorにならないことを確認する（cache.pyが空フレームを
        # マーカー形式で保存する仕様のための回帰テスト）。
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=pd.DataFrame()):
            regional_stocks.update_regional_store(today=dt.date(2026, 8, 13))

        store = regional_stocks.load_regional_store()
        self.assertListEqual(list(store["listing_events"].columns), regional_stocks._LISTING_EVENTS_COLUMNS)
        self.assertListEqual(list(store["major_events"].columns), regional_stocks._MAJOR_EVENTS_COLUMNS)
        self.assertListEqual(list(store["company_status"].columns), regional_stocks._COMPANY_STATUS_COLUMNS)

    def test_company_that_moved_to_tokyo_is_not_re_enriched(self):
        # 1回目の更新で93880を地方単独上場(福)として認識させ、2回目の更新で
        # 東証重複上場(東福)に移った開示が来た場合、company_statusの
        # MarketsStringが更新される一方、株価取得ループの対象からは外れる
        # （is_regional_only("東福")==False）ことを確認する。
        first_batch = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "福岡証券取引所本則市場への重複上場に関するお知らせ",
                        "2026-08-01 08:00:00", "福"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=first_batch), \
                patch(f"{_MOD}.fetch_regional_share_price", return_value=(1824.0, "")):
            regional_stocks.update_regional_store(today=dt.date(2026, 8, 13))

        second_batch = pd.DataFrame([
            _disclosure("2", "93880", "Ｑ－パパネッツ", "東京証券取引所への上場のお知らせ",
                        "2026-08-15 08:00:00", "東福"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=second_batch), \
                patch(f"{_MOD}.fetch_regional_share_price") as mock_fetch:
            result = regional_stocks.update_regional_store(today=dt.date(2026, 8, 15))

        mock_fetch.assert_not_called()
        status_row = result["company_status"].set_index("Code").loc["93880"]
        self.assertEqual(status_row["MarketsString"], "東福")

    def test_watermark_not_advanced_when_a_table_save_fails(self):
        # Supabase設定済みの環境で一部テーブルの保存だけ失敗すると、次回
        # 再デプロイ後にそのテーブルだけ永久に欠落する恐れがある
        # （ウォーターマークだけ進んでしまい、二度と再取得されないため）。
        # cache.save()がFalseを返した場合はウォーターマークを進めない。
        disclosures = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "福岡証券取引所本則市場への重複上場に関するお知らせ",
                        "2026-08-01 08:00:00", "福"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=disclosures), \
                patch(f"{_MOD}.fetch_regional_share_price", return_value=(None, "取得不可（テスト）")), \
                patch(f"{_MOD}.cache.save", return_value=False), \
                patch(f"{_MOD}._save_watermark") as mock_save_watermark:
            regional_stocks.update_regional_store(today=dt.date(2026, 8, 13))

        mock_save_watermark.assert_not_called()


if __name__ == "__main__":
    unittest.main()
