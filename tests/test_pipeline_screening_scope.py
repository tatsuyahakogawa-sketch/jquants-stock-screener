"""src/pipeline.py の run_screening における selected_rules 連動の単体テスト。

YoY比較が必要なルール（YOY_LOOKBACK_RULES）が選択されていない場合、決算データの
取得期間がstart〜endに絞られ、数年分の遡り取得が省略されることを検証する。
外部API（J-Quants/TDnet）は呼ばず、unittest.mock.patchで差し替えてオフラインで
実行する。

実行方法:
    python -m unittest discover tests
"""
from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import pipeline

_MOD = "src.pipeline"


def _run_with_capture(selected_rules):
    """statements_range呼び出しの引数を捕捉しつつrun_screeningを実行するヘルパー。

    今日の日付はtoday_jstを固定でモックする。run_screeningがLightプランの
    取得可能期間（過去5年、today_jst()基準）で遡り取得をクランプするように
    なったため、モックしないと実行するたびの実際の日付に依存してしまい、
    「5年前」の境界がテストの固定start(2026-08-01)を追い越す将来（2031年頃）に
    このテストの各アサーションが実行タイミングだけで失敗するようになる
    （2026-08-27の2巡目のCodexレビューで指摘・修正）。
    """
    captured = {}

    def _fake_get_statements_range(client, start, end):
        captured["start"] = start
        captured["end"] = end
        return pd.DataFrame()

    with (
        patch(f"{_MOD}.today_jst", return_value=dt.date(2026, 8, 5)),
        patch(f"{_MOD}.endpoints.get_listed_info", return_value=pd.DataFrame()),
        patch(f"{_MOD}.endpoints.get_daily_quotes_range", return_value=pd.DataFrame()),
        patch(f"{_MOD}.endpoints.get_statements_range", side_effect=_fake_get_statements_range),
        patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=pd.DataFrame()),
    ):
        pipeline.run_screening(client=object(), start=dt.date(2026, 8, 1), end=dt.date(2026, 8, 5), selected_rules=selected_rules)

    return captured


class TestSelectedRulesControlsLookback(unittest.TestCase):
    def test_default_none_uses_wide_lookback(self):
        captured = _run_with_capture(selected_rules=None)
        self.assertLess(captured["start"], dt.date(2026, 8, 1))

    def test_yoy_rule_selected_uses_wide_lookback(self):
        captured = _run_with_capture(selected_rules=["profit_doubling"])
        self.assertLess(captured["start"], dt.date(2026, 8, 1))

    def test_non_yoy_rules_only_uses_narrow_range(self):
        captured = _run_with_capture(selected_rules=["stop_high", "pbr_low"])
        self.assertEqual(captured["start"], dt.date(2026, 8, 1))

    def test_empty_selected_rules_uses_narrow_range(self):
        # 選択中のイベント/属性ルールが1つも無くても、結果テーブルの
        # 常時表示列（ストップ高日付・「下方修正歴あり」）はselected_rules
        # に関わらず必要なため、statements_dfの取得自体は省略せず、
        # 遡り取得（YOY_LOOKBACK_RULES用）だけを省略する
        # （2026-08-25の6巡目のCodexレビューで取得自体を丸ごと省略する
        # 最適化を一時追加したが、7巡目のレビューで「下方修正歴あり」列が
        # 常にFalseになり除外フィルターが効かなくなる不具合を指摘され、
        # ストップ高日付列も同様に壊れることが分かったため取りやめた）。
        captured = _run_with_capture(selected_rules=[])
        self.assertEqual(captured["start"], dt.date(2026, 8, 1))

    def test_mixed_selection_including_yoy_rule_uses_wide_lookback(self):
        captured = _run_with_capture(selected_rules=["stop_high", "sales_growth_explosive"])
        self.assertLess(captured["start"], dt.date(2026, 8, 1))


class TestWideLookbackDoesNotExceedLightPlanRetention(unittest.TestCase):
    def test_two_year_start_with_yoy_rule_does_not_request_beyond_five_years_back(self):
        # ユーザーが開始日を2年前に設定してsales_growth_explosive
        # （YOY_LOOKBACK_RULES）を選択すると、遡り取得(約4年+60日)により
        # 取得開始日が「2年前からさらに約4年」=約6年前になり、Lightプランの
        # 契約プランの取得可能期間（過去5年）を超えてJ-Quantsに400エラーで
        # 拒否されていた（実機で確認: 2026-08-27に開始日2024-08-27で
        # sales_growth_explosiveを選択し"Your subscription covers the
        # following dates: 2021-08-27 ~"で拒否された）。ユーザー自身が
        # 選んだstart〜end自体は取得可能な範囲内であるにも関わらず
        # スクリーニングが実行できなくなるバグの回帰テスト。
        today = dt.date(2026, 8, 27)
        start = today - dt.timedelta(days=365 * 2)
        end = today
        captured = {}

        def _fake_get_statements_range(client, fetch_start, fetch_end):
            captured["start"] = fetch_start
            captured["end"] = fetch_end
            return pd.DataFrame()

        with (
            patch(f"{_MOD}.today_jst", return_value=today),
            patch(f"{_MOD}.endpoints.get_listed_info", return_value=pd.DataFrame()),
            patch(f"{_MOD}.endpoints.get_daily_quotes_range", return_value=pd.DataFrame()),
            patch(f"{_MOD}.endpoints.get_statements_range", side_effect=_fake_get_statements_range),
            patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=pd.DataFrame()),
        ):
            pipeline.run_screening(
                client=object(), start=start, end=end, selected_rules=["sales_growth_explosive"],
            )

        # 境界は日数の近似(365*5日)ではなく実際の暦日で計算される
        # （うるう年を挟むと1〜2日ずれるため。2026-08-27の2巡目の
        # Codexレビューで指摘・修正）。
        five_years_back = today.replace(year=today.year - 5)
        self.assertGreaterEqual(captured["start"], five_years_back)

    def test_clamped_lookback_warns_only_for_rules_whose_history_was_actually_truncated(self):
        # クランプによってAPI呼び出し自体は成功するが、比較用に遡って
        # 取得したかった一部の決算データは実際には取得できていない。
        # 黙って「合致なし」を返すと、本来ヒットすべき銘柄が見逃されている
        # ことにユーザーが気づけないため、メッセージで明示する
        # （2026-08-27の2巡目のCodexレビューで指摘・修正）。
        # ただし開始日が2年前で影響を受けるのはprofit_doubling（4年分の
        # 比較が必要）だけで、sales_growth_explosive（1年分で足りる）は
        # 2年前のstartから見て前年同期データが十分手前にあるため無関係。
        # 全YOY_LOOKBACK_RULESを一律に警告すると、選択していない・影響も
        # 受けていないルールについてまで不要な警告が出てしまう
        # （2026-08-27の2巡目のCodexレビューで指摘・修正）。
        today = dt.date(2026, 8, 27)
        start = today - dt.timedelta(days=365 * 2)
        end = today

        with (
            patch(f"{_MOD}.today_jst", return_value=today),
            patch(f"{_MOD}.endpoints.get_listed_info", return_value=pd.DataFrame()),
            patch(f"{_MOD}.endpoints.get_daily_quotes_range", return_value=pd.DataFrame()),
            patch(f"{_MOD}.endpoints.get_statements_range", return_value=pd.DataFrame()),
            patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=pd.DataFrame()),
        ):
            _hits, messages = pipeline.run_screening(
                client=object(), start=start, end=end,
                selected_rules=["sales_growth_explosive", "profit_doubling"],
            )

        matching = [m for m in messages if "Lightプラン" in m]
        self.assertEqual(len(matching), 1)
        self.assertIn("経常利益が4年で2倍以上", matching[0])
        self.assertNotIn("売上高が爆発的に増加", matching[0])

    def test_narrow_lookback_rule_alone_does_not_warn_with_a_two_year_start(self):
        # profit_doublingを選択していなければ、開始日が2年前でも
        # sales_growth_explosive自身の前年同期データは十分手前にあり、
        # クランプの影響を受けないため警告は出ない。
        today = dt.date(2026, 8, 27)
        start = today - dt.timedelta(days=365 * 2)
        end = today

        with (
            patch(f"{_MOD}.today_jst", return_value=today),
            patch(f"{_MOD}.endpoints.get_listed_info", return_value=pd.DataFrame()),
            patch(f"{_MOD}.endpoints.get_daily_quotes_range", return_value=pd.DataFrame()),
            patch(f"{_MOD}.endpoints.get_statements_range", return_value=pd.DataFrame()),
            patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=pd.DataFrame()),
        ):
            _hits, messages = pipeline.run_screening(
                client=object(), start=start, end=end, selected_rules=["sales_growth_explosive"],
            )

        self.assertFalse(any("Lightプラン" in m for m in messages))

    def test_narrow_range_does_not_warn(self):
        with (
            patch(f"{_MOD}.today_jst", return_value=dt.date(2026, 8, 5)),
            patch(f"{_MOD}.endpoints.get_listed_info", return_value=pd.DataFrame()),
            patch(f"{_MOD}.endpoints.get_daily_quotes_range", return_value=pd.DataFrame()),
            patch(f"{_MOD}.endpoints.get_statements_range", return_value=pd.DataFrame()),
            patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=pd.DataFrame()),
        ):
            _hits, messages = pipeline.run_screening(
                client=object(), start=dt.date(2026, 8, 1), end=dt.date(2026, 8, 5),
                selected_rules=["stop_high"],
            )

        self.assertFalse(any("Lightプラン" in m for m in messages))


class TestYearsBefore(unittest.TestCase):
    def test_ordinary_date_subtracts_calendar_years_exactly(self):
        self.assertEqual(
            pipeline._years_before(dt.date(2026, 8, 27), 5), dt.date(2021, 8, 27)
        )

    def test_leap_day_falls_back_to_feb_28_when_target_year_is_not_leap(self):
        # 2024年は うるう年だが5年前の2019年は うるう年ではないため、
        # 2/29はそのまま存在しない。2/28にフォールバックする。
        self.assertEqual(
            pipeline._years_before(dt.date(2024, 2, 29), 5), dt.date(2019, 2, 28)
        )

    def test_leap_day_maps_to_leap_day_when_target_year_is_also_leap(self):
        self.assertEqual(
            pipeline._years_before(dt.date(2024, 2, 29), 4), dt.date(2020, 2, 29)
        )


if __name__ == "__main__":
    unittest.main()
