"""src/company_research.py の単体テスト。標準ライブラリのunittestのみ使用。

外部API（J-Quants/EDINET）は呼ばず、依存先の関数をunittest.mock.patchで
差し替えてオフラインで実行する。

実行方法:
    python -m unittest discover tests
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import company_research, excel_export

_MOD = "src.company_research"


class TestEnsureCodeExists(unittest.TestCase):
    def test_raises_when_master_and_fins_both_empty(self):
        with self.assertRaises(company_research.CompanyResearchError):
            company_research._ensure_code_exists("9999", {}, pd.DataFrame())

    def test_no_raise_when_fins_has_data_but_master_empty(self):
        # 地方取引所移籍等でequities/masterから消えても決算開示は残るケース（9388相当）
        fins = pd.DataFrame({"Sales": [100]})
        company_research._ensure_code_exists("9388", {}, fins)

    def test_no_raise_when_master_has_data_but_fins_empty(self):
        company_research._ensure_code_exists("1234", {"CoName": "テスト"}, pd.DataFrame())


class TestBuildCompanyResearchExistenceCheck(unittest.TestCase):
    def test_raises_company_research_error_for_unknown_code(self):
        with (
            patch(f"{_MOD}.excel_export._get_master_row", return_value={}),
            patch(f"{_MOD}.endpoints.get_financials_by_code", return_value=pd.DataFrame()),
            self.assertRaises(company_research.CompanyResearchError),
        ):
            company_research.build_company_research(client=object(), code="99999999")

    def test_succeeds_when_master_missing_but_financials_present(self):
        yuho = {
            "business_overview": "事業概要テキスト",
            "shareholders": None,
            "potential_shares": None,
            "doc_id": None,
        }
        with (
            patch(f"{_MOD}.excel_export._get_master_row", return_value={}),
            patch(
                f"{_MOD}.endpoints.get_financials_by_code",
                return_value=pd.DataFrame({"Sales": [100]}),
            ),
            patch(f"{_MOD}.endpoints.get_price_history_by_code", return_value=pd.DataFrame()),
            patch(f"{_MOD}.pipeline.compute_market_metrics", return_value={}),
            patch(f"{_MOD}.pipeline.estimate_listing_date", return_value=(None, None)),
            patch(f"{_MOD}.excel_export._latest_actual_fy_end", return_value=None),
            patch(f"{_MOD}.excel_export._stock_split_events_text", return_value=None),
            patch(f"{_MOD}.edinet_client.fetch_yuho_texts", return_value=yuho),
        ):
            result = company_research.build_company_research(client=object(), code="9388")

        self.assertIsNone(result.company_name)
        self.assertEqual(result.business_overview, "事業概要テキスト")


class TestNotCommonStockPropagates(unittest.TestCase):
    def test_etf_code_raises_not_common_stock_error(self):
        master_row = {"CoName": "テストETF", "ProdCat": "014"}
        with (
            patch(f"{_MOD}.excel_export._get_master_row", return_value=master_row),
            patch(
                f"{_MOD}.endpoints.get_financials_by_code",
                return_value=pd.DataFrame({"Sales": [100]}),
            ),
            self.assertRaises(excel_export.NotCommonStockError),
        ):
            company_research.build_company_research(client=object(), code="1305")


class TestDegradableFailuresDoNotPropagate(unittest.TestCase):
    """絞った例外タプルでは捕まらない「未知の例外」でも、必ず空値で継続すること。"""

    def test_compute_metrics_swallows_unexpected_exception(self):
        with patch(f"{_MOD}.pipeline.compute_market_metrics", side_effect=ZeroDivisionError("boom")):
            result = company_research._compute_metrics("0000", pd.DataFrame(), pd.DataFrame())
        self.assertEqual(result, {})

    def test_listing_date_swallows_unexpected_exception(self):
        with patch(f"{_MOD}.pipeline.estimate_listing_date", side_effect=RuntimeError("boom")):
            result = company_research._fetch_listing_date("0000", pd.DataFrame())
        self.assertIsNone(result)

    def test_fiscal_year_end_swallows_unexpected_exception(self):
        with patch(f"{_MOD}.excel_export._latest_actual_fy_end", side_effect=OSError("boom")):
            result = company_research._fetch_fiscal_year_end("0000", pd.DataFrame())
        self.assertIsNone(result)

    def test_format_shareholders_swallows_unexpected_exception(self):
        shareholders = pd.DataFrame({"col": [1]})
        with patch(f"{_MOD}.excel_export._format_shareholders_text", side_effect=RuntimeError("boom")):
            result = company_research._format_shareholders_text("0000", shareholders)
        self.assertIsNone(result)

    def test_split_events_swallows_unexpected_exception(self):
        with patch(f"{_MOD}.excel_export._stock_split_events_text", side_effect=RuntimeError("boom")):
            result = company_research._fetch_split_events_text("0000", pd.DataFrame())
        self.assertIsNone(result)


class TestComputeMetricsNormalizesNan(unittest.TestCase):
    def test_nan_values_become_none(self):
        raw = {"market_cap": float("nan"), "per": 1.5, "pbr": None}
        with patch(f"{_MOD}.pipeline.compute_market_metrics", return_value=raw):
            result = company_research._compute_metrics("0000", pd.DataFrame(), pd.DataFrame())
        self.assertIsNone(result["market_cap"])
        self.assertEqual(result["per"], 1.5)
        self.assertIsNone(result["pbr"])
        self.assertFalse(any(isinstance(v, float) and math.isnan(v) for v in result.values()))


class TestBuildCompanyResearchHappyPath(unittest.TestCase):
    def test_all_fields_map_correctly(self):
        master_row = {"CoName": "テスト株式会社", "MktNm": "プライム", "ProdCat": "011"}
        metrics = {
            "market_cap": 123.0,
            "per": 10.0,
            "pbr": 1.2,
            "dividend_yield": 0.03,
            "latest_close": 1000.0,
            "shares_out": 456.0,
        }
        yuho = {
            "business_overview": "概要",
            "shareholders": pd.DataFrame({"株主": ["A"], "比率": ["10%"]}),
            "potential_shares": "潜在株式なし",
            "doc_id": "DOC1",
        }
        with (
            patch(f"{_MOD}.excel_export._get_master_row", return_value=master_row),
            patch(
                f"{_MOD}.endpoints.get_financials_by_code",
                return_value=pd.DataFrame({"Sales": [100]}),
            ),
            patch(f"{_MOD}.endpoints.get_price_history_by_code", return_value=pd.DataFrame()),
            patch(f"{_MOD}.pipeline.compute_market_metrics", return_value=metrics),
            patch(f"{_MOD}.pipeline.estimate_listing_date", return_value=(None, None)),
            patch(f"{_MOD}.excel_export._latest_actual_fy_end", return_value=None),
            patch(f"{_MOD}.excel_export._format_shareholders_text", return_value="A　10%"),
            patch(f"{_MOD}.excel_export._stock_split_events_text", return_value=None),
            patch(f"{_MOD}.edinet_client.fetch_yuho_texts", return_value=yuho),
        ):
            result = company_research.build_company_research(client=object(), code="0000")

        self.assertEqual(result.company_name, "テスト株式会社")
        self.assertEqual(result.market_name, "プライム")
        self.assertEqual(result.market_cap, 123.0)
        self.assertEqual(result.per, 10.0)
        self.assertEqual(result.pbr, 1.2)
        self.assertEqual(result.dividend_yield, 0.03)
        self.assertEqual(result.latest_close, 1000.0)
        self.assertEqual(result.shares_out, 456.0)
        self.assertEqual(result.business_overview, "概要")
        self.assertEqual(result.shareholders_text, "A　10%")
        self.assertEqual(result.potential_shares_text, "潜在株式なし")


if __name__ == "__main__":
    unittest.main()
