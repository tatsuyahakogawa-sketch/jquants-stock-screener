"""src/pipeline.py のPER計算ロジックの単体テスト。

会社予想EPS(FEPS)が直近のFY開示に無く、非連結(FNCEPS)や来期予想(NxFEPS/
NxFNCEPS)にしか値が無いケースで、古い（無関係な）FEPSに遡ってPERが大きく
狂う不具合（4052で実際に発生: PER=951.8倍）を防ぐための
_last_valid_full_year_forecast_eps_with_dateと、それを使うcompute_market_metrics
のPER算出を検証する。

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

from src import pipeline


def _fy_row(disc_date, **overrides):
    row = {
        "DiscDate": disc_date, "CurPerType": "FY", "CurFYEn": disc_date,
        "FEPS": None, "FNCEPS": None, "NxFEPS": None, "NxFNCEPS": None,
    }
    row.update(overrides)
    return row


class TestForecastEpsFallbackChain(unittest.TestCase):
    def test_uses_feps_when_present_on_latest_row(self):
        df = pd.DataFrame([_fy_row("2026-05-14", FEPS=2.45)])
        value, date = pipeline._last_valid_full_year_forecast_eps_with_date(df)
        self.assertEqual(value, 2.45)
        self.assertEqual(date, "2026-05-14")

    def test_falls_back_to_fnceps_when_feps_blank_on_latest_row(self):
        # 4052フィーチャの実例: 直近開示がFEPSを出さずFNCEPSだけ更新
        df = pd.DataFrame([
            _fy_row("2025-06-20", FEPS=-2.63),
            _fy_row("2026-08-05", FNCEPS=7.27),
        ])
        value, date = pipeline._last_valid_full_year_forecast_eps_with_date(df)
        self.assertEqual(value, 7.27)
        self.assertEqual(date, "2026-08-05")

    def test_falls_back_to_nxfeps_when_current_year_forecast_fields_empty(self):
        # 4527/5711/3422の実例: 直近開示が本決算実績そのもので、今期予想は
        # 実績確定済みのため空、来期予想(NxFEPS)だけが入っている
        df = pd.DataFrame([_fy_row("2026-05-13", NxFEPS=152.68)])
        value, date = pipeline._last_valid_full_year_forecast_eps_with_date(df)
        self.assertEqual(value, 152.68)
        self.assertEqual(date, "2026-05-13")

    def test_falls_back_to_nxfnceps_as_last_resort(self):
        df = pd.DataFrame([_fy_row("2026-05-13", NxFNCEPS=87.14)])
        value, date = pipeline._last_valid_full_year_forecast_eps_with_date(df)
        self.assertEqual(value, 87.14)
        self.assertEqual(date, "2026-05-13")

    def test_does_not_use_older_row_when_latest_row_has_any_forecast_value(self):
        df = pd.DataFrame([
            _fy_row("2025-05-01", FEPS=10.0),
            _fy_row("2026-05-01", FNCEPS=20.0),
        ])
        value, date = pipeline._last_valid_full_year_forecast_eps_with_date(df)
        self.assertEqual(value, 20.0)
        self.assertEqual(date, "2026-05-01")

    def test_negative_feps_on_latest_row_is_returned_as_is(self):
        # 符号の判定はcompute_market_metrics側の責務。この関数は値の有無だけを見る。
        df = pd.DataFrame([_fy_row("2026-05-01", FEPS=-1.0, FNCEPS=5.0)])
        value, _date = pipeline._last_valid_full_year_forecast_eps_with_date(df)
        self.assertEqual(value, -1.0)

    def test_no_fy_rows_returns_none(self):
        df = pd.DataFrame([{"DiscDate": "2026-05-01", "CurPerType": "1Q", "FEPS": 10.0}])
        value, date = pipeline._last_valid_full_year_forecast_eps_with_date(df)
        self.assertIsNone(value)
        self.assertIsNone(date)

    def test_all_forecast_columns_blank_falls_back_to_older_row(self):
        df = pd.DataFrame([
            _fy_row("2025-05-01", FEPS=10.0),
            _fy_row("2026-05-01"),
        ])
        value, date = pipeline._last_valid_full_year_forecast_eps_with_date(df)
        self.assertEqual(value, 10.0)
        self.assertEqual(date, "2025-05-01")


class TestComputeMarketMetricsPer(unittest.TestCase):
    def _prices(self, close):
        return pd.DataFrame({"Date": ["2026-08-07"], "C": [close]})

    def test_per_never_falls_back_to_actual_eps(self):
        # forecast_epsが無い場合、実績EPSを使わずPERはNoneになること
        fins = pd.DataFrame([{
            "DiscDate": "2026-05-01", "CurPerType": "FY", "EPS": 100.0,
            "FEPS": None, "FNCEPS": None, "NxFEPS": None, "NxFNCEPS": None,
        }])
        metrics = pipeline.compute_market_metrics(fins, self._prices(1000.0))
        self.assertIsNone(metrics["per"])
        self.assertIsNotNone(metrics["per_debug"]["source_per"])

    def test_negative_forecast_eps_gives_none_per(self):
        fins = pd.DataFrame([{
            "DiscDate": "2026-05-01", "CurPerType": "FY", "FEPS": -5.0,
        }])
        metrics = pipeline.compute_market_metrics(fins, self._prices(1000.0))
        self.assertIsNone(metrics["per"])

    def test_regression_4052_style_bug_now_gives_reasonable_per(self):
        fins = pd.DataFrame([
            {"DiscDate": "2025-06-20", "CurPerType": "FY", "FEPS": -2.63},
            {"DiscDate": "2026-08-05", "CurPerType": "FY", "FNCEPS": 7.27},
        ])
        metrics = pipeline.compute_market_metrics(fins, self._prices(533.0))
        self.assertAlmostEqual(metrics["per"], 533.0 / 7.27, places=4)
        self.assertLess(metrics["per"], 100)

    def test_per_debug_fields_present(self):
        fins = pd.DataFrame([{"DiscDate": "2026-05-01", "CurPerType": "FY", "FEPS": 10.0}])
        metrics = pipeline.compute_market_metrics(fins, self._prices(1000.0))
        debug = metrics["per_debug"]
        for key in ("forecast_eps", "forecast_eps_date", "calculated_per", "source_per", "per_difference_rate"):
            self.assertIn(key, debug)
        self.assertEqual(debug["calculated_per"], metrics["per"])

    def test_large_divergence_logs_warning(self):
        fins = pd.DataFrame([
            {"DiscDate": "2024-01-01", "CurPerType": "1Q", "EPS": 100.0},
            {"DiscDate": "2026-05-01", "CurPerType": "FY", "FEPS": 1.0},
        ])
        with patch.object(pipeline.logger, "warning") as mock_warning:
            pipeline.compute_market_metrics(fins, self._prices(1000.0))
        mock_warning.assert_called_once()

    def test_small_divergence_does_not_log_warning(self):
        fins = pd.DataFrame([
            {"DiscDate": "2024-01-01", "CurPerType": "FY", "EPS": 10.0},
            {"DiscDate": "2026-05-01", "CurPerType": "FY", "FEPS": 10.0},
        ])
        with patch.object(pipeline.logger, "warning") as mock_warning:
            pipeline.compute_market_metrics(fins, self._prices(1000.0))
        mock_warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
