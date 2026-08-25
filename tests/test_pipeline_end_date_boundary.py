"""src/pipeline.py の run_screening における、選択期間の終了日(end)境界の
単体テスト。

TDnet由来のrule（stock_split・new_facility_or_store等）のDateはpubdateの
時刻情報を保持している（例:"2026-08-05 16:30:00"）。end<=比較ではend当日
0時0分より後の開示が全て弾かれてしまい、UIのデフォルト「終了日=開始日」の
1日だけの範囲では対象ルールが実質機能していなかった
（2026-08-24のCodexレビューで指摘・修正）。外部API（J-Quants/TDnet）は
呼ばず、unittest.mock.patchで差し替えてオフラインで実行する。

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


def _run_with_disclosure_on_end_day(pubdate: str):
    disclosures = pd.DataFrame([
        {"company_code": "10000", "title": "新工場の稼働開始に関するお知らせ", "pubdate": pubdate},
    ])
    with (
        patch(f"{_MOD}.endpoints.get_listed_info", return_value=pd.DataFrame()),
        patch(f"{_MOD}.endpoints.get_daily_quotes_range", return_value=pd.DataFrame()),
        patch(f"{_MOD}.endpoints.get_statements_range", return_value=pd.DataFrame()),
        patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=disclosures),
    ):
        hits, _messages = pipeline.run_screening(
            client=object(), start=dt.date(2026, 8, 5), end=dt.date(2026, 8, 5),
            selected_rules=["new_facility_or_store"],
        )
    return hits


class TestEndDateBoundaryIncludesWholeDay(unittest.TestCase):
    def test_disclosure_with_afternoon_timestamp_on_end_day_is_included(self):
        # UIのデフォルトの「終了日=開始日」の1日だけの範囲で、当日午後に
        # 出た開示が結果から消えていた不具合の回帰テスト。
        hits = _run_with_disclosure_on_end_day("2026-08-05 16:30:00")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits.iloc[0]["Code"], "10000")

    def test_disclosure_at_midnight_on_end_day_is_still_included(self):
        hits = _run_with_disclosure_on_end_day("2026-08-05 00:00:00")
        self.assertEqual(len(hits), 1)

    def test_disclosure_on_day_after_end_is_still_excluded(self):
        hits = _run_with_disclosure_on_end_day("2026-08-06 00:00:01")
        self.assertTrue(hits.empty)


if __name__ == "__main__":
    unittest.main()
