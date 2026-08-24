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


def _run(disclosures_df: pd.DataFrame):
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
            selected_rules=["new_facility_or_store"],
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


if __name__ == "__main__":
    unittest.main()
