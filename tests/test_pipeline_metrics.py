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


class TestLatestCloseSkipsNoTradeDays(unittest.TestCase):
    """薄商いで売買が成立しなかった日（O/H/L/C全てnullの行）が最新の取得日
    だった場合に、現在値・PER・PBR・時価総額・配当利回りが軒並み空欄になって
    しまう問題の単体テスト（2026-08-24にユーザー報告で発見・修正。実データで
    銘柄コード7902の2023-06-30・2023-07-06が無取引日と確認。/equities/bars/daily
    は無取引日もO/H/L/C全てnullの行として返してくる）。
    """

    def _fins(self):
        return pd.DataFrame({
            "DiscDate": ["2026-05-14"], "CurPerType": ["FY"],
            "FEPS": [20.0], "BPS": [300.0],
            "ShOutFY": [1_000_000], "TrShFY": [0],
            "DivAnn": [10.0], "FDivAnn": [10.0],
        })

    def test_latest_row_with_null_close_falls_back_to_prior_trading_day(self):
        prices = pd.DataFrame({
            "Date": ["2026-08-05", "2026-08-06", "2026-08-07"],
            "C": [400.0, 420.0, None],
        })
        metrics = pipeline.compute_market_metrics(self._fins(), prices)
        self.assertEqual(metrics["latest_close"], 420.0)
        self.assertEqual(metrics["latest_price_date"], pd.Timestamp("2026-08-06"))
        self.assertEqual(metrics["price_source"], "jquants")
        self.assertIsNotNone(metrics["per"])

    def test_all_rows_null_close_leaves_metrics_blank(self):
        prices = pd.DataFrame({"Date": ["2026-08-06", "2026-08-07"], "C": [None, None]})
        metrics = pipeline.compute_market_metrics(self._fins(), prices)
        self.assertIsNone(metrics["latest_close"])
        self.assertIsNone(metrics["price_source"])


class TestSplitAdjustmentSkipsNoTradeDays(unittest.TestCase):
    """_split_adjustment_sinceが、開示日時点で直近のC/AdjCを探す際に無取引日
    （O/H/L/C全てnullの行）をそのまま使ってしまう問題の単体テスト
    （2026-08-24のCodexレビューで指摘・修正）。

    latest_close側の無取引日フォールバック修正により、以前は無取引日のせいで
    latest_close自体が空欄になっていた銘柄でもPER/PBR/配当利回りが計算される
    ようになった。その際、開示日の直近行がたまたま無取引日だと、C/AdjCが
    両方nullで「分割等が無い」(1.0固定)と誤判定され、実際にはあった分割が
    無視されて誤ったPER/PBR/配当利回りが計算されてしまう。
    """

    def test_looks_back_past_no_trade_day_to_find_split_ratio(self):
        # 2026-01-09(開示日=since_date)は無取引日でC/AdjCともnull。
        # その前日2026-01-08は有効なC/AdjCを持ち、1→2分割の累積倍率(0.5)を示す。
        price_history = pd.DataFrame({
            "Date": ["2026-01-08", "2026-01-09"],
            "C": [1000.0, None],
            "AdjC": [500.0, None],
        })
        ratio = pipeline._split_adjustment_since(price_history, "2026-01-09")
        self.assertEqual(ratio, 0.5)

    def test_no_trade_day_without_earlier_valid_row_falls_back_to_no_adjustment(self):
        price_history = pd.DataFrame({"Date": ["2026-01-09"], "C": [None], "AdjC": [None]})
        self.assertEqual(pipeline._split_adjustment_since(price_history, "2026-01-09"), 1.0)

    def test_recovered_latest_close_uses_correctly_split_adjusted_eps_for_per(self):
        # latest_close側の無取引日フォールバックでPERが計算されるようになった
        # 銘柄で、FEPSの基準日(開示日)側の分割調整も正しく効くことを確認する
        # 統合的なテスト。分割前FEPS=20.0、1→2分割後の現在株式数基準では
        # 10.0相当になるはずなので、PER = latest_close(480) / 10.0 = 48.0
        # （分割調整が効かず1.0のままだとPER = 480/20.0 = 24.0になってしまう）。
        prices = pd.DataFrame({
            "Date": ["2026-01-08", "2026-01-09", "2026-01-15", "2026-01-16"],
            "C": [1000.0, None, 480.0, None],
            "AdjC": [500.0, None, 480.0, None],
        })
        fins = pd.DataFrame({
            "DiscDate": ["2026-01-09"], "CurPerType": ["FY"],
            "FEPS": [20.0], "BPS": [300.0],
            "ShOutFY": [1_000_000], "TrShFY": [0],
            "DivAnn": [10.0], "FDivAnn": [10.0],
        })
        metrics = pipeline.compute_market_metrics(fins, prices)
        self.assertEqual(metrics["latest_close"], 480.0)
        self.assertAlmostEqual(metrics["per"], 48.0)


if __name__ == "__main__":
    unittest.main()
