"""app.py のUI連携（時間がかかる業績条件pillsの分離）のテスト。

streamlit.testing.v1.AppTest（streamlit本体に同梱、追加ライブラリ不要）で
ブラウザを使わずスクリプトを実行し、ウィジェットの状態を検証する。
選択済み時の紫色CSS自体は実ブラウザで目視確認済み（このテストでは検証しない）。

実行方法:
    python -m unittest discover tests
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import pipeline

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")

# app.pyの_PERFORMANCE_EVENT_RULESと同じ内容（画面上の見た目分割前の全体集合）。
_EXPECTED_PERFORMANCE_RULES = {
    "sales_growth_major",
    "sales_growth_explosive",
    "earnings_beat",
    "two_quarter_growth",
    "profit_doubling",
}


class TestSlowPerformancePillsSeparation(unittest.TestCase):
    def test_app_loads_without_exception(self):
        at = AppTest.from_file(APP_PATH)
        at.run(timeout=30)
        self.assertEqual(len(at.exception), 0)

    def test_slow_pills_are_exactly_the_yoy_lookback_rules(self):
        # at.pills(...).optionsはformat_func適用後の表示ラベルを返すため、
        # RULE_LABELS経由でラベルに変換して比較する。
        at = AppTest.from_file(APP_PATH)
        at.run(timeout=30)
        slow_options = set(at.pills(key="slow_performance_events_pills").options)
        expected_labels = {pipeline.RULE_LABELS[r] for r in pipeline.YOY_LOOKBACK_RULES}
        self.assertEqual(slow_options, expected_labels)

    def test_fast_and_slow_pills_cover_all_performance_rules_without_overlap(self):
        at = AppTest.from_file(APP_PATH)
        at.run(timeout=30)
        fast_options = set(at.pills(key="fast_performance_events_pills").options)
        slow_options = set(at.pills(key="slow_performance_events_pills").options)
        expected_labels = {pipeline.RULE_LABELS[r] for r in _EXPECTED_PERFORMANCE_RULES}
        self.assertEqual(fast_options & slow_options, set())
        self.assertEqual(fast_options | slow_options, expected_labels)

    def test_selecting_slow_pill_updates_selection_count_caption(self):
        at = AppTest.from_file(APP_PATH)
        at.run(timeout=30)
        at.pills(key="slow_performance_events_pills").set_value(["profit_doubling"])
        at.run(timeout=30)
        self.assertEqual(len(at.exception), 0)
        captions = [c.value for c in at.caption]
        self.assertTrue(any("1件選択中" in c for c in captions))


if __name__ == "__main__":
    unittest.main()
