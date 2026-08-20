"""src/excel_export.py の _regional_fallback_info の単体テスト
（equities/masterに載っていない地方単独上場企業向けの会社名・市場情報補完）。

実行方法:
    python -m unittest discover tests
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import excel_export

_MOD = "src.excel_export"

_EMPTY_STORE = {
    "company_status": pd.DataFrame(
        columns=["Code", "CompanyName", "MarketsString", "LastSeenDate", "LastDelistingDate",
                 "IsDelisted", "CurrentPrice", "CurrentPriceNote"]
    ),
    "listing_events": pd.DataFrame(),
    "major_events": pd.DataFrame(),
}


def _disclosure(company_code, company_name, pubdate, markets_string, title="テスト開示"):
    return {
        "company_code": company_code,
        "company_name": company_name,
        "title": title,
        "pubdate": pubdate,
        "markets_string": markets_string,
    }


def _status_row(code, name, markets_string, is_delisted=False):
    return {
        "Code": code, "CompanyName": name, "MarketsString": markets_string,
        "LastSeenDate": pd.Timestamp("2026-08-01"), "LastDelistingDate": pd.NaT,
        "IsDelisted": is_delisted, "CurrentPrice": None, "CurrentPriceNote": "",
    }


class TestRegionalFallbackInfoUsesStore(unittest.TestCase):
    """地方株ストア(regional_stocks.load_regional_store)に既に載っている銘柄は、
    TDnetへ問い合わせずストアの結果をそのまま使う
    （2026-08-20のCodexレビュー: 3年分のTDnet再取得を避けるための修正）。
    """

    def test_uses_store_without_calling_tdnet(self):
        store = {**_EMPTY_STORE, "company_status": pd.DataFrame([_status_row("93880", "テスト株式会社", "福")])}
        with patch(f"{_MOD}.regional_stocks.load_regional_store", return_value=store), \
                patch(f"{_MOD}.tdnet_client.get_disclosures_range") as mock_get:
            name, markets_string = excel_export._regional_fallback_info("9388")

        mock_get.assert_not_called()
        self.assertEqual(name, "テスト株式会社")
        self.assertEqual(markets_string, "福")

    def test_delisted_company_in_store_returns_no_market(self):
        # 上場廃止済みの銘柄は、株価フォールバックを発動させないためmarkets_string
        # をNoneで返す（古いyfinance終値を現在値として誤表示しないため）。
        store = {**_EMPTY_STORE, "company_status": pd.DataFrame(
            [_status_row("93880", "テスト株式会社", "福", is_delisted=True)]
        )}
        with patch(f"{_MOD}.regional_stocks.load_regional_store", return_value=store), \
                patch(f"{_MOD}.tdnet_client.get_disclosures_range") as mock_get:
            name, markets_string = excel_export._regional_fallback_info("9388")

        mock_get.assert_not_called()
        self.assertEqual(name, "テスト株式会社")
        self.assertIsNone(markets_string)


class TestRegionalFallbackInfoTdnetFallback(unittest.TestCase):
    """ストアにまだ無い銘柄（一度も「地方株」ページで検出されていない）は、
    従来通りTDnetから直接検索する。
    """

    def setUp(self):
        self._store_patcher = patch(f"{_MOD}.regional_stocks.load_regional_store", return_value=_EMPTY_STORE)
        self._store_patcher.start()
        self.addCleanup(self._store_patcher.stop)

    def test_fetches_today_separately_with_force_refresh(self):
        disclosures = pd.DataFrame([
            _disclosure("93880", "テスト株式会社", "2026-08-01 08:00:00", "福"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=disclosures) as mock_get:
            excel_export._regional_fallback_info("9388")

        self.assertEqual(len(mock_get.call_args_list), 2)
        self.assertFalse(mock_get.call_args_list[0].kwargs.get("force_refresh", False))
        today_call = mock_get.call_args_list[1]
        self.assertTrue(today_call.kwargs.get("force_refresh"))
        self.assertEqual(today_call.args[0], today_call.args[1])  # start == end == today

    def test_uses_latest_valid_market_even_if_newest_disclosure_lacks_it(self):
        # 直近の開示にmarkets_stringが欠損していても、それより前の開示に
        # 有効な市場情報があればそれを使う（会社名は単純に最新の開示から）。
        stable = pd.DataFrame([
            _disclosure("93880", "テスト株式会社", "2026-08-01 08:00:00", "福"),
        ])
        todays = pd.DataFrame([
            _disclosure("93880", "テスト株式会社（改称）", "2026-08-20 08:00:00", None),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", side_effect=[stable, todays]):
            name, markets_string = excel_export._regional_fallback_info("9388")

        self.assertEqual(name, "テスト株式会社（改称）")
        self.assertEqual(markets_string, "福")

    def test_returns_none_market_when_latest_disclosure_is_delisting(self):
        # ストアに未登録の銘柄で、直近の開示が上場廃止のお知らせ(markets_string
        # 欠損)の場合、それより前の開示の市場情報にフォールバックしない
        # （既に上場廃止した銘柄を地方単独上場のまま扱ってしまうことを防ぐ。
        # 2026-08-20のCodexレビューで指摘）。
        stable = pd.DataFrame([
            _disclosure("93880", "テスト株式会社", "2026-08-01 08:00:00", "福"),
        ])
        todays = pd.DataFrame([
            _disclosure("93880", "テスト株式会社", "2026-08-20 08:00:00", None, title="上場廃止に関するお知らせ"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", side_effect=[stable, todays]):
            name, markets_string = excel_export._regional_fallback_info("9388")

        self.assertEqual(name, "テスト株式会社")
        self.assertIsNone(markets_string)

    def test_returns_none_market_when_no_disclosure_ever_had_valid_markets(self):
        disclosures = pd.DataFrame([
            _disclosure("93880", "テスト株式会社", "2026-08-01 08:00:00", None),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=disclosures):
            name, markets_string = excel_export._regional_fallback_info("9388")

        self.assertEqual(name, "テスト株式会社")
        self.assertIsNone(markets_string)

    def test_returns_blank_when_company_not_found(self):
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=pd.DataFrame()):
            name, markets_string = excel_export._regional_fallback_info("9388")

        self.assertEqual(name, "")
        self.assertIsNone(markets_string)


class TestHasUsableClose(unittest.TestCase):
    def test_empty_dataframe_is_not_usable(self):
        self.assertFalse(excel_export._has_usable_close(pd.DataFrame()))

    def test_rows_with_all_null_close_are_not_usable(self):
        # equities/masterから消えた銘柄は、/equities/bars/dailyが空ではなく
        # 各営業日の行はあるがC(終値)列が全期間nullという形で返ってくる
        # （CLAUDE.md「データ対象範囲の制約」参照。2026-08-20のCodexレビューで指摘）。
        prices = pd.DataFrame({"Date": ["2026-08-01", "2026-08-02"], "C": [None, None]})
        self.assertFalse(excel_export._has_usable_close(prices))

    def test_at_least_one_valid_close_is_usable(self):
        prices = pd.DataFrame({"Date": ["2026-08-01", "2026-08-02"], "C": [None, 500.0]})
        self.assertTrue(excel_export._has_usable_close(prices))


if __name__ == "__main__":
    unittest.main()
