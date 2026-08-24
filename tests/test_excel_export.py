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


def _status_row(code, name, markets_string, is_delisted=False, current_price=None):
    return {
        "Code": code, "CompanyName": name, "MarketsString": markets_string,
        "LastSeenDate": pd.Timestamp("2026-08-01"), "LastDelistingDate": pd.NaT,
        "IsDelisted": is_delisted, "CurrentPrice": current_price, "CurrentPriceNote": "",
    }


class TestRegionalFallbackInfo(unittest.TestCase):
    """_regional_fallback_infoはregional_stocks.update_regional_store()
    （「地方株」ページの更新ボタンと同じ、自己ウォーターマーク方式の差分更新）
    経由で会社名・市場情報・現在値を取得する。load_regional_store()（読み取り
    専用）だとストアが古いまま(上場廃止・市場変更を取りこぼす)になりうる
    ため、常にupdate_regional_store()で鮮度を保証する
    （2026-08-20の3巡目のCodexレビューで指摘・修正）。
    """

    def test_uses_updated_store_result(self):
        store = {"company_status": pd.DataFrame(
            [_status_row("93880", "テスト株式会社", "福", current_price=830.0)]
        )}
        with patch(f"{_MOD}.regional_stocks.update_regional_store", return_value=store) as mock_update:
            name, markets_string, price = excel_export._regional_fallback_info("9388")

        mock_update.assert_called_once_with()
        self.assertEqual(name, "テスト株式会社")
        self.assertEqual(markets_string, "福")
        self.assertEqual(price, 830.0)

    def test_reuses_current_price_already_fetched_by_store_update(self):
        # update_regional_store()は現在も地方単独上場の銘柄について、その場で
        # yfinanceからCurrentPriceを取得済み。ここで別途fetch_regional_share_price
        # を呼び直すと、1回のExcel生成でyfinanceに2回問い合わせることになり、
        # 個別の再取得だけが一時的に失敗した場合に前者の値まで失われてしまう
        # （2026-08-24の4巡目のCodexレビューで指摘・修正）。
        store = {"company_status": pd.DataFrame(
            [_status_row("93880", "テスト株式会社", "福", current_price=830.0)]
        )}
        with patch(f"{_MOD}.regional_stocks.update_regional_store", return_value=store), \
                patch(f"{_MOD}.regional_stocks.fetch_regional_share_price") as mock_fetch:
            _name, _markets_string, price = excel_export._regional_fallback_info("9388")

        mock_fetch.assert_not_called()
        self.assertEqual(price, 830.0)

    def test_rejects_stale_price_after_tokyo_market_transition_not_yet_in_master(self):
        # update_regional_store()は「現在も地方単独上場」の銘柄だけCurrentPrice
        # を更新し、東証を含む市場に移った銘柄は更新をスキップして古い値を
        # そのまま保持する。TDnetでは既に東証移籍(markets_stringが"福"→"東福")
        # が反映されているのに、J-Quantsのマスタがまだ追いついていない
        # （equities/masterに未反映でmaster_rowが空のまま）タイミングでは、
        # IsDelisted判定だけでは弾けず、更新が止まった古い価格を「現在値」として
        # 誤って提示してしまう（2026-08-24の5巡目のCodexレビューで指摘・修正）。
        store = {"company_status": pd.DataFrame(
            [_status_row("93880", "テスト株式会社", "東福", is_delisted=False, current_price=830.0)]
        )}
        with patch(f"{_MOD}.regional_stocks.update_regional_store", return_value=store):
            name, markets_string, price = excel_export._regional_fallback_info("9388")

        self.assertEqual(name, "テスト株式会社")
        self.assertEqual(markets_string, "東福")  # 市場表示自体は最新のTDnet情報のまま
        self.assertIsNone(price)  # ただし現在値(古い可能性がある)は返さない

    def test_delisted_company_returns_no_market_or_price(self):
        # 上場廃止済みの銘柄は、株価フォールバックを発動させないためmarkets_string・
        # 現在値ともNoneで返す（古いyfinance終値を現在値として誤表示しないため）。
        store = {"company_status": pd.DataFrame(
            [_status_row("93880", "テスト株式会社", "福", is_delisted=True, current_price=830.0)]
        )}
        with patch(f"{_MOD}.regional_stocks.update_regional_store", return_value=store):
            name, markets_string, price = excel_export._regional_fallback_info("9388")

        self.assertEqual(name, "テスト株式会社")
        self.assertIsNone(markets_string)
        self.assertIsNone(price)

    def test_returns_blank_when_company_not_found(self):
        store = {"company_status": pd.DataFrame(
            columns=["Code", "CompanyName", "MarketsString", "LastSeenDate",
                     "LastDelistingDate", "IsDelisted", "CurrentPrice", "CurrentPriceNote"]
        )}
        with patch(f"{_MOD}.regional_stocks.update_regional_store", return_value=store):
            name, markets_string, price = excel_export._regional_fallback_info("9388")

        self.assertEqual(name, "")
        self.assertIsNone(markets_string)
        self.assertIsNone(price)

    def test_matches_four_digit_code_with_trailing_zero(self):
        # TDnet/regional_stocks側のcompany_codeは5桁表記(4桁+"0")のことがある。
        store = {"company_status": pd.DataFrame([_status_row("93880", "テスト株式会社", "福")])}
        with patch(f"{_MOD}.regional_stocks.update_regional_store", return_value=store):
            name, markets_string, _price = excel_export._regional_fallback_info("9388")

        self.assertEqual(name, "テスト株式会社")
        self.assertEqual(markets_string, "福")

    def test_returns_blank_when_store_update_fails(self):
        # TDnetミラー障害等でupdate_regional_store()自体が例外を投げても、
        # 呼び出し元の処理(Excel生成)は続行できるよう空の結果を返す。
        with patch(f"{_MOD}.regional_stocks.update_regional_store", side_effect=RuntimeError("network down")):
            name, markets_string, price = excel_export._regional_fallback_info("9388")

        self.assertEqual(name, "")
        self.assertIsNone(markets_string)
        self.assertIsNone(price)


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


class TestNearestClose(unittest.TestCase):
    """薄商いで売買が成立しなかった日（O/H/L/C全てnullの行）を、直近の終値と
    して誤って扱わないことの単体テスト（2026-08-24にユーザー報告で発見・
    修正。実データで銘柄コード7902の2023-06-30・2023-07-06が無取引日と確認）。
    """

    def _prices(self, rows):
        df = pd.DataFrame(rows, columns=["Date", "C"])
        df["Date"] = pd.to_datetime(df["Date"])
        return df

    def test_exact_date_match_with_null_close_falls_back_to_prior_trading_day(self):
        prices = self._prices([
            ("2023-06-29", 829.0),
            ("2023-06-30", None),  # 無取引日
            ("2023-07-03", 837.0),
        ])
        self.assertEqual(excel_export._nearest_close(prices, "2023-06-30"), 829.0)

    def test_target_before_all_valid_data_falls_back_to_first_valid_row(self):
        prices = self._prices([
            ("2023-06-28", None),  # 無取引日
            ("2023-06-29", 829.0),
            ("2023-07-03", 837.0),
        ])
        self.assertEqual(excel_export._nearest_close(prices, "2023-06-01"), 829.0)

    def test_all_null_close_returns_none(self):
        prices = self._prices([("2023-06-29", None), ("2023-06-30", None)])
        self.assertIsNone(excel_export._nearest_close(prices, "2023-06-30"))

    def test_normal_case_unaffected(self):
        prices = self._prices([("2023-06-29", 829.0), ("2023-06-30", 831.0)])
        self.assertEqual(excel_export._nearest_close(prices, "2023-06-30"), 831.0)


if __name__ == "__main__":
    unittest.main()
