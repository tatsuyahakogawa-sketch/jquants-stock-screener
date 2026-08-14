"""src/regional_stocks.py の単体テスト。

「地方株」ページ向けの、地方単独上場企業の検出・時価総額取得・増分ストアを
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


class TestRegionalMarketsIn(unittest.TestCase):
    def test_single_market(self):
        self.assertEqual(regional_stocks.regional_markets_in("福"), ["福証"])

    def test_multiple_markets_preserve_order(self):
        self.assertEqual(regional_stocks.regional_markets_in("札福"), ["福証", "札証"])

    def test_tokyo_char_ignored(self):
        self.assertEqual(regional_stocks.regional_markets_in("東"), [])


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


class TestFetchRegionalMarketCap(unittest.TestCase):
    def test_fukuoka_only_uses_yfinance_f_suffix(self):
        mock_yf = MagicMock()
        mock_yf.Ticker.return_value.fast_info = {"lastPrice": 1824.0}
        with patch.dict(sys.modules, {"yfinance": mock_yf}):
            price, note = regional_stocks.fetch_regional_market_cap("93880", "福")
        self.assertEqual(price, 1824.0)
        self.assertEqual(note, "")
        mock_yf.Ticker.assert_called_once_with("9388.F")

    def test_nagoya_only_is_not_attempted(self):
        price, note = regional_stocks.fetch_regional_market_cap("61110", "名")
        self.assertIsNone(price)
        self.assertIn("取得不可", note)

    def test_sapporo_only_is_not_attempted(self):
        price, note = regional_stocks.fetch_regional_market_cap("48340", "札")
        self.assertIsNone(price)
        self.assertIn("取得不可", note)

    def test_yfinance_exception_is_handled(self):
        mock_yf = MagicMock()
        mock_yf.Ticker.side_effect = RuntimeError("boom")
        with patch.dict(sys.modules, {"yfinance": mock_yf}):
            price, note = regional_stocks.fetch_regional_market_cap("93880", "福")
        self.assertIsNone(price)
        self.assertIn("取得不可", note)

    def test_yfinance_returns_no_price(self):
        mock_yf = MagicMock()
        mock_yf.Ticker.return_value.fast_info = {"lastPrice": None}
        with patch.dict(sys.modules, {"yfinance": mock_yf}):
            price, note = regional_stocks.fetch_regional_market_cap("93880", "福")
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
                patch(f"{_MOD}.fetch_regional_market_cap", return_value=(None, "取得不可（テスト）")):
            today = dt.date(2026, 8, 13)
            result = regional_stocks.update_regional_store(today=today)

        call_start, call_end = mock_get.call_args[0]
        self.assertEqual(call_end, today)
        self.assertLess(call_start, today - dt.timedelta(days=365))  # 3年遡る初回ブートストラップ
        self.assertEqual(len(result["listing_events"]), 1)
        self.assertEqual(result["company_status"].iloc[0]["Code"], "93880")

    def test_second_run_only_fetches_since_watermark(self):
        first_batch = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "福岡証券取引所本則市場への重複上場に関するお知らせ",
                        "2026-08-01 08:00:00", "福"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=first_batch), \
                patch(f"{_MOD}.fetch_regional_market_cap", return_value=(None, "取得不可（テスト）")):
            regional_stocks.update_regional_store(today=dt.date(2026, 8, 13))

        second_batch = pd.DataFrame([
            _disclosure("2", "48340", "キャリアバンク", "当社株式に対するTOBに関するお知らせ",
                        "2026-08-14 08:00:00", "札"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=second_batch) as mock_get, \
                patch(f"{_MOD}.fetch_regional_market_cap", return_value=(None, "取得不可（テスト）")):
            result = regional_stocks.update_regional_store(today=dt.date(2026, 8, 14))

        call_start, _call_end = mock_get.call_args[0]
        self.assertEqual(call_start, dt.date(2026, 8, 14))  # 前回ウォーターマーク(8/13)の翌日から
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
                patch(f"{_MOD}.fetch_regional_market_cap", return_value=(None, "取得不可（テスト）")):
            regional_stocks.update_regional_store(today=dt.date(2026, 8, 13))

        store = regional_stocks.load_regional_store()
        self.assertEqual(len(store["listing_events"]), 1)
        self.assertEqual(store["company_status"].iloc[0]["Code"], "93880")

    def test_company_that_moved_to_tokyo_is_not_re_enriched(self):
        # 東証を含む市場に移った銘柄はis_regional_only=Falseになり、
        # update_regional_store内のMarketCap再取得ループの対象から外れることを確認。
        disclosures = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "東京証券取引所への上場のお知らせ",
                        "2026-08-01 08:00:00", "東福"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=disclosures), \
                patch(f"{_MOD}.fetch_regional_market_cap") as mock_fetch:
            regional_stocks.update_regional_store(today=dt.date(2026, 8, 13))
        mock_fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
