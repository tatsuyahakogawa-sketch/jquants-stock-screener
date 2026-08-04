"""src/score.py の計算式ごとの単体テスト。標準ライブラリのunittestのみ使用。

実行方法:
    python -m unittest discover tests
"""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import score


class TestMarketCap(unittest.TestCase):
    def test_ultra_small_cap_not_excluded(self):
        points, label = score.score_market_cap(29.9)
        self.assertEqual(points, 0.0)
        self.assertEqual(label, "超小型株")

    def test_small_cap_band(self):
        self.assertEqual(score.score_market_cap(30)[0], 1.0)
        self.assertEqual(score.score_market_cap(300)[0], 1.0)

    def test_mid_small_band(self):
        points, label = score.score_market_cap(400)
        self.assertEqual(points, 0.5)
        self.assertEqual(label, "300億円超〜500億円")

    def test_above_band_no_points(self):
        self.assertEqual(score.score_market_cap(600)[0], 0.0)

    def test_missing_value_is_undetermined(self):
        points, label = score.score_market_cap(None)
        self.assertEqual(points, 0.0)
        self.assertEqual(label, "判定不能")
        points, label = score.score_market_cap(float("nan"))
        self.assertEqual(label, "判定不能")


class TestThreeYearRevenueGrowth(unittest.TestCase):
    def test_three_consecutive_increases(self):
        points, _ = score.score_three_year_revenue_growth([100, 110, 120, 130])
        self.assertEqual(points, 1.0)

    def test_not_increasing_every_year(self):
        points, _ = score.score_three_year_revenue_growth([100, 110, 105, 130])
        self.assertEqual(points, 0.0)

    def test_missing_year_is_undetermined(self):
        points, label = score.score_three_year_revenue_growth([100, None, 120, 130])
        self.assertEqual(points, 0.0)
        self.assertEqual(label, "判定不能")

    def test_wrong_length_is_undetermined(self):
        points, label = score.score_three_year_revenue_growth([100, 110, 120])
        self.assertEqual(label, "判定不能")


class TestRevenueCagr(unittest.TestCase):
    def test_high_growth_gets_two_points(self):
        # (2.0)^(1/3) - 1 ≈ 0.26 >= 0.25
        points, cagr = score.score_revenue_cagr(100, 200)
        self.assertEqual(points, 2.0)
        self.assertAlmostEqual(cagr, 0.2599, places=3)

    def test_moderate_growth_gets_one_point(self):
        # 15%: sales ratio = 1.15^3 ≈ 1.5209
        points, cagr = score.score_revenue_cagr(100, 152.09)
        self.assertEqual(points, 1.0)
        self.assertAlmostEqual(cagr, 0.15, places=2)

    def test_low_growth_gets_no_points(self):
        points, _ = score.score_revenue_cagr(100, 110)
        self.assertEqual(points, 0.0)

    def test_zero_base_is_undetermined(self):
        points, cagr = score.score_revenue_cagr(0, 100)
        self.assertEqual(points, 0.0)
        self.assertIsNone(cagr)

    def test_missing_value_is_undetermined(self):
        points, cagr = score.score_revenue_cagr(None, 100)
        self.assertEqual(points, 0.0)
        self.assertIsNone(cagr)


class TestProfitGrowthExceedsSalesGrowth(unittest.TestCase):
    def test_condition_met(self):
        points, _ = score.score_profit_growth_exceeds_sales_growth(0.20, 0.40, 100, 140)
        self.assertEqual(points, 1.0)

    def test_profit_growth_not_exceeding_sales_growth(self):
        points, _ = score.score_profit_growth_exceeds_sales_growth(0.40, 0.35, 100, 135)
        self.assertEqual(points, 0.0)

    def test_sales_growth_too_low(self):
        points, _ = score.score_profit_growth_exceeds_sales_growth(0.10, 0.50, 100, 150)
        self.assertEqual(points, 0.0)

    def test_turnaround_branch_with_negative_prior_profit(self):
        points, label = score.score_profit_growth_exceeds_sales_growth(0.20, None, -10, 5)
        self.assertEqual(points, 1.0)
        self.assertIn("黒字転換", label)

    def test_turnaround_branch_fails_without_enough_sales_growth(self):
        points, _ = score.score_profit_growth_exceeds_sales_growth(0.05, None, -10, 5)
        self.assertEqual(points, 0.0)

    def test_missing_values_undetermined(self):
        points, _ = score.score_profit_growth_exceeds_sales_growth(None, None, 10, 15)
        self.assertEqual(points, 0.0)


class TestMarginImprovement(unittest.TestCase):
    def test_improved_enough(self):
        self.assertEqual(score.score_margin_improvement(2.5), 1.0)

    def test_improved_not_enough(self):
        self.assertEqual(score.score_margin_improvement(1.9), 0.0)

    def test_missing_value(self):
        self.assertEqual(score.score_margin_improvement(None), 0.0)


class TestTurnaround(unittest.TestCase):
    def test_turnaround_without_sustain_bonus(self):
        points, turned = score.score_turnaround(-10, 5, sustained_two_periods=False)
        self.assertEqual(points, 2.0)
        self.assertTrue(turned)

    def test_turnaround_with_sustain_bonus(self):
        points, turned = score.score_turnaround(-10, 5, sustained_two_periods=True)
        self.assertEqual(points, 3.0)
        self.assertTrue(turned)

    def test_still_in_loss_no_points(self):
        points, turned = score.score_turnaround(-10, -2, sustained_two_periods=False)
        self.assertEqual(points, 0.0)
        self.assertFalse(turned)

    def test_already_profitable_last_year_not_a_turnaround(self):
        points, turned = score.score_turnaround(5, 10, sustained_two_periods=False)
        self.assertEqual(points, 0.0)
        self.assertFalse(turned)

    def test_missing_values(self):
        points, turned = score.score_turnaround(None, 5, sustained_two_periods=False)
        self.assertEqual(points, 0.0)
        self.assertFalse(turned)


class TestUpwardRevision(unittest.TestCase):
    def test_sales_and_profit_revision_with_streak(self):
        points = score.score_upward_revision(0.06, 0.12, consecutive_upward_count=2)
        self.assertEqual(points, 3.0)

    def test_only_sales_revision(self):
        points = score.score_upward_revision(0.06, 0.05, consecutive_upward_count=1)
        self.assertEqual(points, 1.0)

    def test_no_revision(self):
        points = score.score_upward_revision(0.01, 0.02, consecutive_upward_count=0)
        self.assertEqual(points, 0.0)

    def test_missing_values_contribute_nothing(self):
        points = score.score_upward_revision(None, None, consecutive_upward_count=0)
        self.assertEqual(points, 0.0)


class TestProgressRatio(unittest.TestCase):
    def test_first_quarter_meets_threshold(self):
        points, _ = score.score_progress_ratio("1Q", 30.0, None)
        self.assertEqual(points, 1.0)

    def test_first_quarter_below_threshold(self):
        points, _ = score.score_progress_ratio("1Q", 20.0, None)
        self.assertEqual(points, 0.0)

    def test_seasonality_guard_blocks_when_not_improved(self):
        points, label = score.score_progress_ratio("2Q", 55.0, 50.0)
        self.assertEqual(points, 0.0)
        self.assertIn("前年同期", label)

    def test_seasonality_guard_passes_when_improved(self):
        points, _ = score.score_progress_ratio("2Q", 65.0, 50.0)
        self.assertEqual(points, 1.0)

    def test_fy_period_is_undetermined(self):
        points, label = score.score_progress_ratio("FY", 100.0, None)
        self.assertEqual(points, 0.0)
        self.assertEqual(label, "判定不能")


class TestDilution(unittest.TestCase):
    def test_large_increase_penalized(self):
        points, _ = score.score_dilution(0.12, looks_like_split=False)
        self.assertEqual(points, -2.0)

    def test_moderate_increase_penalized_less(self):
        points, _ = score.score_dilution(0.07, looks_like_split=False)
        self.assertEqual(points, -1.0)

    def test_small_increase_not_penalized(self):
        points, _ = score.score_dilution(0.02, looks_like_split=False)
        self.assertEqual(points, 0.0)

    def test_stock_split_not_penalized(self):
        points, label = score.score_dilution(2.0, looks_like_split=True)
        self.assertEqual(points, 0.0)
        self.assertIn("株式分割", label)

    def test_missing_value_is_unjudged(self):
        points, label = score.score_dilution(None, looks_like_split=False)
        self.assertEqual(points, 0.0)
        self.assertEqual(label, "未判定")


class TestLooksLikeStockSplit(unittest.TestCase):
    def test_two_for_one_split_detected(self):
        # 株式数が2倍(+100%)になり、BPSがほぼ半分(-50%)になっているケース
        self.assertTrue(score.looks_like_stock_split(1.0, -0.5))

    def test_real_dilution_not_detected_as_split(self):
        # 株式数が20%増えたが、BPSはほとんど変わらない（新規資本が入った）ケース
        self.assertFalse(score.looks_like_stock_split(0.20, -0.02))

    def test_no_share_increase_is_not_a_split(self):
        self.assertFalse(score.looks_like_stock_split(0.0, 0.0))

    def test_missing_values(self):
        self.assertFalse(score.looks_like_stock_split(None, -0.5))


class TestDownwardRevision(unittest.TestCase):
    def test_penalize_only_mode(self):
        self.assertEqual(score.score_downward_revision(True, penalize_only=True), -2.0)

    def test_no_downward_revision(self):
        self.assertEqual(score.score_downward_revision(False, penalize_only=True), 0.0)

    def test_exclude_mode_returns_zero_here(self):
        # 除外モードは呼び出し側のフィルターで処理するため、ここでは常に0点
        self.assertEqual(score.score_downward_revision(True, penalize_only=False), 0.0)


if __name__ == "__main__":
    unittest.main()
