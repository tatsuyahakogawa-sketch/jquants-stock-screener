"""src/pipeline.py の run_screening における、sales_growth_doubling
（1年で売上高2倍）が選択期間(start〜end)に関係なく常に銘柄ごとの最新開示を
対象にすることの単体テスト。

決算短信は四半期に1回しか出ないため、選択期間（UIのデフォルトは
「終了日=開始日」の1日だけ）にたまたま決算日が入っていない限りヒットしない。
「1年で2倍」は銘柄が現在その状態にあるかを知りたい用途のため、このルールだけ
選択期間による絞り込みを行わず、銘柄ごとに最新の該当開示を常に対象にする
（2026-08-25にユーザー報告・実データ19銘柄で確認の上、ユーザーの指定で
sales_growth_doublingのみをこの特別扱いにした。sales_growth_major/explosiveは
従来通り「期間内のイベント」のまま）。外部API（J-Quants/TDnet）は呼ばず、
unittest.mock.patchで差し替えてオフラインで実行する。

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


def _statement_row(code, per_type, per_end, disc_date, sales):
    return {
        "Code": code, "CurPerType": per_type, "CurPerEn": pd.Timestamp(per_end),
        "DiscDate": pd.Timestamp(disc_date), "Sales": sales,
    }


def _run(statements_df, start, end, selected_rules):
    with (
        patch(f"{_MOD}.endpoints.get_listed_info", return_value=pd.DataFrame()),
        patch(f"{_MOD}.endpoints.get_daily_quotes_range", return_value=pd.DataFrame()),
        patch(f"{_MOD}.endpoints.get_statements_range", return_value=statements_df),
        patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=pd.DataFrame()),
    ):
        return pipeline.run_screening(
            client=object(), start=start, end=end, selected_rules=selected_rules,
        )


class TestSalesGrowthDoublingIgnoresSelectedPeriod(unittest.TestCase):
    def test_old_doubling_disclosure_still_appears_with_narrow_default_range(self):
        # 決算短信は半年前に出ており、UIのデフォルトである「終了日=開始日」の
        # 1日だけの範囲には入らない。それでも1年で2倍(+150%)は常に表示される。
        statements = pd.DataFrame([
            _statement_row("10000", "1Q", "2025-06-30", "2025-08-10", 100),
            _statement_row("10000", "1Q", "2026-06-30", "2026-08-10", 250),  # +150%（半年前に開示）
        ])
        today = dt.date(2026, 8, 25)
        hits, _messages = _run(statements, start=today, end=today, selected_rules=["sales_growth_doubling"])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits.iloc[0]["Code"], "10000")
        self.assertEqual(hits.iloc[0]["Rule"], "sales_growth_doubling")
        self.assertEqual(hits.iloc[0]["Date"], pd.Timestamp("2026-08-10"))

    def test_only_the_latest_doubling_disclosure_per_code_is_shown(self):
        # 同一銘柄が過去に複数回「1年で2倍」を達成していても、最新の1件だけを表示する。
        statements = pd.DataFrame([
            _statement_row("10000", "1Q", "2024-06-30", "2024-08-10", 100),
            _statement_row("10000", "1Q", "2025-06-30", "2025-08-10", 250),  # +150%（1回目、古い）
            _statement_row("10000", "1Q", "2026-06-30", "2026-08-10", 600),  # +140%（2回目、最新）
        ])
        today = dt.date(2026, 8, 25)
        hits, _messages = _run(statements, start=today, end=today, selected_rules=["sales_growth_doubling"])
        doubling = hits.loc[hits["Rule"] == "sales_growth_doubling"]
        self.assertEqual(len(doubling), 1)
        self.assertEqual(doubling.iloc[0]["Date"], pd.Timestamp("2026-08-10"))

    def test_sales_growth_major_still_respects_selected_period(self):
        # sales_growth_major/explosiveは従来通り「期間内のイベント」のまま
        # （sales_growth_doublingだけの特別扱いであることの回帰確認）。
        statements = pd.DataFrame([
            _statement_row("20000", "1Q", "2025-06-30", "2025-08-10", 100),
            _statement_row("20000", "1Q", "2026-06-30", "2026-08-10", 130),  # +30%（大幅増収のみ、半年前に開示）
        ])
        today = dt.date(2026, 8, 25)
        hits, _messages = _run(statements, start=today, end=today, selected_rules=["sales_growth_major"])
        self.assertTrue(hits.empty)

    def test_no_doubling_when_growth_under_threshold(self):
        statements = pd.DataFrame([
            _statement_row("30000", "1Q", "2025-06-30", "2025-08-10", 100),
            _statement_row("30000", "1Q", "2026-06-30", "2026-08-10", 150),  # +50%（爆発的増収止まり）
        ])
        today = dt.date(2026, 8, 25)
        hits, _messages = _run(statements, start=today, end=today, selected_rules=["sales_growth_doubling"])
        self.assertTrue(hits.empty)


if __name__ == "__main__":
    unittest.main()
