"""src/pipeline.py の compute_market_metrics における fallback_price
（地方単独上場企業向けのyfinance株価フォールバック）の単体テスト。

実行方法:
    python -m unittest discover tests
"""
from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import pipeline


def _fins():
    return pd.DataFrame({
        "DiscDate": ["2026-05-14"],
        "CurPerType": ["FY"],
        "FEPS": [20.0],
        "BPS": [300.0],
        "ShOutFY": [1_000_000],
        "TrShFY": [0],
        "DivAnn": [10.0],
        "FDivAnn": [10.0],
    })


class TestFallbackPrice(unittest.TestCase):
    def test_market_cap_is_none_when_price_comes_from_fallback(self):
        # 地方単独上場企業はJ-Quantsに株価が無いため、東証離脱前の最後の
        # ShOutFY/TrShFYしか分からない。fallback_price(yfinance現在値)と
        # 組み合わせると、異なる時点の値を掛け合わせた誤った時価総額に
        # なるため、price_source="yfinance"の場合は時価総額を計算しない
        # （2026-08-20のCodexレビューで指摘・修正）。
        metrics = pipeline.compute_market_metrics(
            _fins(), pd.DataFrame(), fallback_price=500.0, fallback_price_date=dt.date(2026, 8, 20),
        )
        self.assertEqual(metrics["price_source"], "yfinance")
        self.assertEqual(metrics["latest_close"], 500.0)
        self.assertIsNone(metrics["market_cap"])

    def test_per_pbr_dividend_yield_still_computed_from_fallback_price(self):
        # 時価総額だけを除外し、PER/PBR/配当利回りはfallback_priceを使って
        # 従来通り計算する。
        metrics = pipeline.compute_market_metrics(
            _fins(), pd.DataFrame(), fallback_price=500.0, fallback_price_date=dt.date(2026, 8, 20),
        )
        self.assertAlmostEqual(metrics["per"], 500.0 / 20.0)
        self.assertAlmostEqual(metrics["pbr"], 500.0 / 300.0)
        self.assertAlmostEqual(metrics["dividend_yield"], 10.0 / 500.0)

    def test_market_cap_still_computed_when_price_is_from_jquants(self):
        prices = pd.DataFrame({"Date": ["2026-08-19"], "C": [500.0]})
        metrics = pipeline.compute_market_metrics(_fins(), prices, fallback_price=999.0)
        self.assertEqual(metrics["price_source"], "jquants")
        self.assertEqual(metrics["market_cap"], 500.0 * 1_000_000)


if __name__ == "__main__":
    unittest.main()
