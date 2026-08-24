"""src/pipeline.py の run_screening における、TDnet開示件数の上限
（MAX_TDNET_DISCLOSURES_FOR_SCREENING）の単体テスト。

期間指定が広すぎる等でTDnet開示が大量に該当する場合、タイトルの
キーワード一致で判定するTDNET_TITLE_BASED_RULES（新工場・新店舗・東証移籍・
株式分割・プライム市場変更・大型受注・世界初の発表）の検索を行わず、
警告を出す（2026-08-24にユーザーが指定）。外部API（J-Quants/TDnet）は
呼ばず、unittest.mock.patchで差し替えてオフラインで実行する。

実行方法:
    python -m unittest discover tests
"""
from __future__ import annotations

import datetime as dt
import sys
import unittest
import warnings
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
        warnings.catch_warnings(record=True) as caught,
    ):
        warnings.simplefilter("always")
        hits = pipeline.run_screening(
            client=object(), start=dt.date(2026, 8, 1), end=dt.date(2026, 8, 5),
            selected_rules=list(selected_rules) if selected_rules is not None else None,
        )
    return hits, caught


class TestTdnetDisclosureLimit(unittest.TestCase):
    def test_within_limit_runs_title_based_rules_normally(self):
        hits, caught = _run(_facility_disclosures(config.MAX_TDNET_DISCLOSURES_FOR_SCREENING))
        self.assertFalse(hits.empty)
        self.assertEqual(len(caught), 0)

    def test_over_limit_skips_title_based_rules_and_warns(self):
        hits, caught = _run(_facility_disclosures(config.MAX_TDNET_DISCLOSURES_FOR_SCREENING + 1))
        self.assertTrue(hits.empty)
        self.assertEqual(len(caught), 1)
        message = str(caught[0].message)
        self.assertIn(str(config.MAX_TDNET_DISCLOSURES_FOR_SCREENING + 1), message)
        self.assertIn(str(config.MAX_TDNET_DISCLOSURES_FOR_SCREENING), message)

    def test_over_limit_does_not_warn_when_no_tdnet_rule_selected(self):
        # stop_high・pbr_low等、J-Quants由来のルールしか選んでいない場合は
        # TDnet開示件数がいくら多くても検索結果に影響しないため、無関係な
        # 「期間を絞り込め」警告を出してはいけない（2026-08-24のCodexレビューで
        # 指摘・修正）。この場合は従来通りタイトルベースの検出も普通に実行する。
        hits, caught = _run(
            _facility_disclosures(config.MAX_TDNET_DISCLOSURES_FOR_SCREENING + 1),
            selected_rules=["stop_high", "pbr_low"],
        )
        self.assertFalse(hits.empty)
        self.assertEqual(len(caught), 0)

    def test_over_limit_warns_when_selected_rules_is_none(self):
        # selected_rules=None（後方互換のデフォルト。全ルール対象）の場合は
        # TDnetルールも当然含まれるため、上限超過時は通常通り警告する。
        hits, caught = _run(
            _facility_disclosures(config.MAX_TDNET_DISCLOSURES_FOR_SCREENING + 1),
            selected_rules=None,
        )
        self.assertTrue(hits.empty)
        self.assertEqual(len(caught), 1)


if __name__ == "__main__":
    unittest.main()
