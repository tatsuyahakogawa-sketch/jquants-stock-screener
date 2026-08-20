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


def _disclosure(company_code, company_name, pubdate, markets_string):
    return {
        "company_code": company_code,
        "company_name": company_name,
        "title": "テスト開示",
        "pubdate": pubdate,
        "markets_string": markets_string,
    }


class TestRegionalFallbackInfo(unittest.TestCase):
    def test_fetches_today_separately_with_force_refresh(self):
        # 当日分のTDnet開示はまだ全件公開されていない可能性があるため、
        # 前日までとは別にforce_refresh=Trueで毎回取り直す必要がある
        # （src/regional_stocks.pyのupdate_regional_storeと同じパターン。
        # 2026-08-20のCodexレビューで指摘・修正）。
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


if __name__ == "__main__":
    unittest.main()
