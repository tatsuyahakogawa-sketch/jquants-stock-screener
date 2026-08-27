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
    """statements_range呼び出しの引数を捕捉しつつrun_screeningを実行するヘルパー。"""
    captured = {}

    def _fake_get_statements_range(client, start, end):
        captured["start"] = start
        captured["end"] = end
        return pd.DataFrame()

    with (
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

        five_years_back = today - dt.timedelta(days=365 * 5)
        self.assertGreaterEqual(captured["start"], five_years_back)


if __name__ == "__main__":
    unittest.main()
