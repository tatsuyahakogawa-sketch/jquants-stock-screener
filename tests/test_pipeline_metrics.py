"""src/pipeline.py の compute_market_metrics における metrics_as_of（デバッグ用の
データ基準日）の単体テスト。

PER・PBR・配当利回りは常にlatest_close（最新の株価）を分母/分子に使うが、EPS・
BPS・配当という分子側は財務開示ごとに異なる時点のデータになる。metrics_as_ofは
それぞれの基準日を画面には出さずに内部で追跡するためのデバッグ情報。

実行方法:
    python -m unittest discover tests
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import pipeline


class TestMetricsAsOf(unittest.TestCase):
    def test_metrics_as_of_present_with_full_data(self):
        prices = pd.DataFrame({
            "Date": ["2026-08-06", "2026-08-07"],
            "C": [400.0, 420.0],
        })
        fins = pd.DataFrame({
            "DiscDate": ["2026-05-14"],
            "CurPerType": ["FY"],
            "FEPS": [20.0],
            "BPS": [300.0],
            "ShOutFY": [1_000_000],
            "TrShFY": [0],
            "DivAnn": [10.0],
            "FDivAnn": [10.0],
        })
        metrics = pipeline.compute_market_metrics(fins, prices)
        as_of = metrics["metrics_as_of"]
        self.assertEqual(as_of["latest_price_date"], pd.Timestamp("2026-08-07"))
        self.assertEqual(as_of["per_eps_date"], pd.Timestamp("2026-05-14"))
        self.assertEqual(as_of["bps_date"], pd.Timestamp("2026-05-14"))

    def test_metrics_as_of_all_none_when_no_data(self):
        metrics = pipeline.compute_market_metrics(pd.DataFrame(), pd.DataFrame())
        as_of = metrics["metrics_as_of"]
        self.assertIsNone(as_of["latest_price_date"])
        self.assertIsNone(as_of["per_eps_date"])
        self.assertIsNone(as_of["bps_date"])
        self.assertIsNone(as_of["dividend_source_date"])

    def test_metrics_as_of_does_not_remove_existing_fields(self):
        metrics = pipeline.compute_market_metrics(pd.DataFrame(), pd.DataFrame())
        for key in ("latest_close", "market_cap", "per", "pbr", "dividend_yield", "shares_out"):
            self.assertIn(key, metrics)


if __name__ == "__main__":
    unittest.main()
