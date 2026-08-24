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


def _status_row(code, name, markets_string, is_delisted=False):
    return {
        "Code": code, "CompanyName": name, "MarketsString": markets_string,
        "LastSeenDate": pd.Timestamp("2026-08-01"), "LastDelistingDate": pd.NaT,
        "IsDelisted": is_delisted, "CurrentPrice": None, "CurrentPriceNote": "",
    }


class TestRegionalFallbackInfo(unittest.TestCase):
    """_regional_fallback_infoはregional_stocks.update_regional_store()
    （「地方株」ページの更新ボタンと同じ、自己ウォーターマーク方式の差分更新）
    経由で会社名・市場情報を取得する。load_regional_store()（読み取り専用）
    だとストアが古いまま(上場廃止・市場変更を取りこぼす)になりうるため、
    常にupdate_regional_store()で鮮度を保証する
    （2026-08-20の3巡目のCodexレビューで指摘・修正）。
    """

    def test_uses_updated_store_result(self):
        store = {"company_status": pd.DataFrame([_status_row("93880", "テスト株式会社", "福")])}
        with patch(f"{_MOD}.regional_stocks.update_regional_store", return_value=store) as mock_update:
            name, markets_string = excel_export._regional_fallback_info("9388")

        mock_update.assert_called_once_with()
        self.assertEqual(name, "テスト株式会社")
        self.assertEqual(markets_string, "福")

    def test_delisted_company_returns_no_market(self):
        # 上場廃止済みの銘柄は、株価フォールバックを発動させないためmarkets_string
        # をNoneで返す（古いyfinance終値を現在値として誤表示しないため）。
        store = {"company_status": pd.DataFrame(
            [_status_row("93880", "テスト株式会社", "福", is_delisted=True)]
        )}
        with patch(f"{_MOD}.regional_stocks.update_regional_store", return_value=store):
            name, markets_string = excel_export._regional_fallback_info("9388")

        self.assertEqual(name, "テスト株式会社")
        self.assertIsNone(markets_string)

    def test_returns_blank_when_company_not_found(self):
        store = {"company_status": pd.DataFrame(
            columns=["Code", "CompanyName", "MarketsString", "LastSeenDate",
                     "LastDelistingDate", "IsDelisted", "CurrentPrice", "CurrentPriceNote"]
        )}
        with patch(f"{_MOD}.regional_stocks.update_regional_store", return_value=store):
            name, markets_string = excel_export._regional_fallback_info("9388")

        self.assertEqual(name, "")
        self.assertIsNone(markets_string)

    def test_matches_four_digit_code_with_trailing_zero(self):
        # TDnet/regional_stocks側のcompany_codeは5桁表記(4桁+"0")のことがある。
        store = {"company_status": pd.DataFrame([_status_row("93880", "テスト株式会社", "福")])}
        with patch(f"{_MOD}.regional_stocks.update_regional_store", return_value=store):
            name, markets_string = excel_export._regional_fallback_info("9388")

        self.assertEqual(name, "テスト株式会社")
        self.assertEqual(markets_string, "福")

    def test_returns_blank_when_store_update_fails(self):
        # TDnetミラー障害等でupdate_regional_store()自体が例外を投げても、
        # 呼び出し元の処理(Excel生成)は続行できるよう空の結果を返す。
        with patch(f"{_MOD}.regional_stocks.update_regional_store", side_effect=RuntimeError("network down")):
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
