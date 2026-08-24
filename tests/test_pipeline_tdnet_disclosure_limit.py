"""src/pipeline.py の run_screening における、TDnet開示件数の上限
（MAX_TDNET_DISCLOSURES_FOR_SCREENING）の単体テスト。

期間指定が広すぎる等でTDnet開示が大量に該当する場合、タイトルの
キーワード一致で判定するTDNET_TITLE_BASED_RULES（新工場・新店舗・東証移籍・
株式分割・プライム市場変更・大型受注・世界初の発表）の検索を行わず、
注意メッセージを返す（2026-08-24にユーザーが指定）。メッセージはPython標準の
warnings.warn（プロセスグローバルでスレッドセーフでない）ではなく、
run_screeningの戻り値として明示的に返す（2026-08-24の2巡目のCodexレビューで
指摘・修正）。外部API（J-Quants/TDnet）は呼ばず、unittest.mock.patchで
差し替えてオフラインで実行する。

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

from src import config, pipeline

_MOD = "src.pipeline"


def _facility_disclosures(n: int) -> pd.DataFrame:
    """detect_new_facility_or_storeがヒットするタイトルの開示をn件作る。"""
    return pd.DataFrame([
        {
            "company_code": f"{1000 + i}0",
            "title": "新工場の稼働開始に関するお知らせ",
            "pubdate": "2026-08-01 08:00:00",
        }
        for i in range(n)
    ])


def _run(disclosures_df: pd.DataFrame, selected_rules=("new_facility_or_store",)):
    with (
        patch(f"{_MOD}.endpoints.get_listed_info", return_value=pd.DataFrame()),
        patch(f"{_MOD}.endpoints.get_daily_quotes_range", return_value=pd.DataFrame()),
        patch(f"{_MOD}.endpoints.get_statements_range", return_value=pd.DataFrame()),
        patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=disclosures_df),
    ):
        return pipeline.run_screening(
            client=object(), start=dt.date(2026, 8, 1), end=dt.date(2026, 8, 5),
            selected_rules=list(selected_rules) if selected_rules is not None else None,
        )


def _run_with_tdnet_failure(selected_rules):
    with (
        patch(f"{_MOD}.endpoints.get_listed_info", return_value=pd.DataFrame()),
        patch(f"{_MOD}.endpoints.get_daily_quotes_range", return_value=pd.DataFrame()),
        patch(f"{_MOD}.endpoints.get_statements_range", return_value=pd.DataFrame()),
        patch(f"{_MOD}.tdnet_client.get_disclosures_range", side_effect=RuntimeError("TDnetミラー障害(テスト)")),
    ):
        return pipeline.run_screening(
            client=object(), start=dt.date(2026, 8, 1), end=dt.date(2026, 8, 5),
            selected_rules=list(selected_rules) if selected_rules is not None else None,
        )


class TestTdnetDisclosureLimit(unittest.TestCase):
    def test_within_limit_runs_title_based_rules_normally(self):
        hits, messages = _run(_facility_disclosures(config.MAX_TDNET_DISCLOSURES_FOR_SCREENING))
        self.assertFalse(hits.empty)
        self.assertEqual(messages, [])

    def test_over_limit_skips_title_based_rules_and_warns(self):
        hits, messages = _run(_facility_disclosures(config.MAX_TDNET_DISCLOSURES_FOR_SCREENING + 1))
        self.assertTrue(hits.empty)
        self.assertEqual(len(messages), 1)
        self.assertIn(str(config.MAX_TDNET_DISCLOSURES_FOR_SCREENING + 1), messages[0])
        self.assertIn(str(config.MAX_TDNET_DISCLOSURES_FOR_SCREENING), messages[0])

    def test_over_limit_does_not_warn_when_no_tdnet_rule_selected(self):
        # stop_high・pbr_low等、J-Quants由来のルールしか選んでいない場合は
        # TDnet開示件数がいくら多くても検索結果に影響しないため、無関係な
        # 「期間を絞り込め」警告を出してはいけない（2026-08-24のCodexレビューで
        # 指摘・修正）。この場合は従来通りタイトルベースの検出も普通に実行する。
        hits, messages = _run(
            _facility_disclosures(config.MAX_TDNET_DISCLOSURES_FOR_SCREENING + 1),
            selected_rules=["stop_high", "pbr_low"],
        )
        self.assertFalse(hits.empty)
        self.assertEqual(messages, [])

    def test_over_limit_warns_when_selected_rules_is_none(self):
        # selected_rules=None（後方互換のデフォルト。全ルール対象）の場合は
        # TDnetルールも当然含まれるため、上限超過時は通常通り警告する。
        hits, messages = _run(
            _facility_disclosures(config.MAX_TDNET_DISCLOSURES_FOR_SCREENING + 1),
            selected_rules=None,
        )
        self.assertTrue(hits.empty)
        self.assertEqual(len(messages), 1)

    def test_tdnet_fetch_failure_warns_when_tdnet_rule_selected(self):
        hits, messages = _run_with_tdnet_failure(selected_rules=["new_facility_or_store"])
        self.assertTrue(hits.empty)
        self.assertEqual(len(messages), 1)
        self.assertIn("TDnet開示情報の取得に失敗しました", messages[0])

    def test_tdnet_fetch_failure_is_silent_when_no_tdnet_rule_selected(self):
        # stop_high・pbr_low等、J-Quants由来のルールしか選んでいない場合は
        # TDnetミラーが障害中でも検索結果に一切影響しないため、無関係な
        # 「取得に失敗しました」警告を出してはいけない（2026-08-24の3巡目の
        # Codexレビューで指摘・修正）。
        hits, messages = _run_with_tdnet_failure(selected_rules=["stop_high", "pbr_low"])
        self.assertTrue(hits.empty)  # J-Quants側もモックで空データのため空(仕様上は無関係)
        self.assertEqual(messages, [])


if __name__ == "__main__":
    unittest.main()
