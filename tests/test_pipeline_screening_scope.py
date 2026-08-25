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

    def test_empty_selected_rules_skips_the_fetch_entirely(self):
        # 選択中のルールが1つも無ければstatements_df・quotes_dfを使う
        # ルールは1つも動かないため、取得自体を丸ごと省略する
        # （2026-08-25の6巡目のCodexレビューでのsales_growth_doubling専用
        # 対応と合わせて、selected_rules=[]（何も選択していない）の場合も
        # 同じ理由で取得自体を省略するよう修正。以前は範囲を狭めるだけで
        # 取得自体は毎回行っていた）。
        captured = _run_with_capture(selected_rules=[])
        self.assertEqual(captured, {})

    def test_mixed_selection_including_yoy_rule_uses_wide_lookback(self):
        captured = _run_with_capture(selected_rules=["stop_high", "sales_growth_explosive"])
        self.assertLess(captured["start"], dt.date(2026, 8, 1))


if __name__ == "__main__":
    unittest.main()
