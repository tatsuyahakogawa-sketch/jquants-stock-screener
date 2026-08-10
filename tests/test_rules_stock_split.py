"""src/rules.py の detect_stock_split（株式分割・併合の新規発表検出）の単体テスト。

「株式分割」「株式併合」というキーワードが含まれるだけで判定していた旧ロジックの
誤検出（配当予想修正・株主優待変更等の後日談を新規発表として拾ってしまう）を
防ぐための_classify_stock_split_titleを中心に検証する。実際のTDnet開示タイトル
（2026年8月時点）で確認済みの実例をそのまま使用。

実行方法:
    python -m unittest discover tests
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import rules


class TestClassifyStockSplitTitleIncludes(unittest.TestCase):
    """新規の決定・発表として検出すべきタイトル。"""

    def test_simple_split_notice(self):
        is_new, _ = rules._classify_stock_split_title("株式分割に関するお知らせ")
        self.assertTrue(is_new)

    def test_simple_consolidation_notice(self):
        is_new, _ = rules._classify_stock_split_title("株式併合に関するお知らせ")
        self.assertTrue(is_new)

    def test_split_with_articles_amendment_joined_by_oyobi(self):
        is_new, _ = rules._classify_stock_split_title("株式分割及び定款の一部変更に関するお知らせ")
        self.assertTrue(is_new)

    def test_board_resolution_wording(self):
        is_new, _ = rules._classify_stock_split_title("取締役会で株式分割を決議した旨の開示")
        self.assertTrue(is_new)

    def test_shareholder_meeting_agenda_wording(self):
        is_new, _ = rules._classify_stock_split_title("株主総会に株式併合を付議する旨の開示")
        self.assertTrue(is_new)

    def test_real_example_yagi_original_announcement(self):
        # 7460 ヤギ 2026-05-11（分割そのものの新規発表。定款変更を伴っていても新規発表扱い）
        is_new, reason = rules._classify_stock_split_title(
            "株式分割及び株式分割に伴う定款の一部変更に関するお知らせ"
        )
        self.assertTrue(is_new)
        self.assertIn("単独で出現", reason)

    def test_real_example_bundled_split_articles_dividend(self):
        # 5706/6622/1926/1965/4667/7716等で共通の実際のタイトル形式
        is_new, _ = rules._classify_stock_split_title(
            "株式分割および株式分割に伴う定款の一部変更ならびに配当予想の修正に関するお知らせ"
        )
        self.assertTrue(is_new)

    def test_real_example_comma_listed_topics(self):
        # 5232 住友大阪セメント
        is_new, _ = rules._classify_stock_split_title("株式分割、定款の一部変更、配当予想の修正等に関するお知らせ")
        self.assertTrue(is_new)

    def test_real_example_consolidation_shareholder_meeting(self):
        # 7082 ジモティー（株式併合の付議。ユーザーの一般規則には合致するが個別の
        # 期待値と食い違うため、変更後もこの挙動になることをレポートで明示する）
        is_new, _ = rules._classify_stock_split_title(
            "株式併合並びに単元株式数の定めの廃止及び定款一部変更に関する臨時株主総会開催のお知らせ"
        )
        self.assertTrue(is_new)


class TestClassifyStockSplitTitleExcludes(unittest.TestCase):
    """新規発表ではない（後日談・事務的な知らせ）として除外すべきタイトル。"""

    def test_dividend_forecast_revision_due_to_split(self):
        # 7460 ヤギ 2026-08-03（今回の主な誤検出例）
        is_new, reason = rules._classify_stock_split_title("株式分割に伴う配当予想の修正に関するお知らせ")
        self.assertFalse(is_new)
        self.assertIn("配当予想の修正", reason)

    def test_shareholder_benefit_change_due_to_split(self):
        is_new, _ = rules._classify_stock_split_title("株式分割に伴う株主優待制度の変更について")
        self.assertFalse(is_new)

    def test_articles_amendment_only_due_to_split(self):
        is_new, _ = rules._classify_stock_split_title("株式分割に伴う定款変更のみに関するお知らせ")
        self.assertFalse(is_new)

    def test_stock_acquisition_right_adjustment_due_to_split(self):
        is_new, _ = rules._classify_stock_split_title("株式分割に伴う新株予約権の調整に関するお知らせ")
        self.assertFalse(is_new)

    def test_shares_outstanding_change_due_to_split(self):
        is_new, _ = rules._classify_stock_split_title("株式分割後の発行済株式数に関するお知らせ")
        self.assertFalse(is_new)

    def test_real_example_conversion_price_adjustment(self):
        is_new, _ = rules._classify_stock_split_title("株式分割に伴う転換価額及び行使価額等の調整に関するお知らせ")
        self.assertFalse(is_new)

    def test_no_keyword_at_all(self):
        is_new, reason = rules._classify_stock_split_title("業績予想の修正に関するお知らせ")
        self.assertFalse(is_new)
        self.assertEqual(reason, "")


class TestDetectStockSplitDataFrame(unittest.TestCase):
    def _disclosures(self, rows):
        return pd.DataFrame(rows, columns=["company_code", "title", "pubdate", "document_url"])

    def test_returns_only_new_announcements_with_debug_columns(self):
        df = self._disclosures([
            {"company_code": "74600", "title": "株式分割及び株式分割に伴う定款の一部変更に関するお知らせ",
             "pubdate": "2026-05-11 16:00:00", "document_url": "https://example.com/1"},
            {"company_code": "74600", "title": "株式分割に伴う配当予想の修正に関するお知らせ",
             "pubdate": "2026-08-03 16:00:00", "document_url": "https://example.com/2"},
        ])
        result = rules.detect_stock_split(df)
        self.assertEqual(len(result), 1)
        row = result.iloc[0]
        self.assertEqual(row["Code"], "74600")
        self.assertEqual(row["rule"], "stock_split")
        self.assertEqual(row["event_type"], "stock_split")
        self.assertEqual(row["source_url"], "https://example.com/1")
        self.assertIn("単独で出現", row["match_reason"])
        self.assertEqual(row["event_date"], row["Date"])

    def test_empty_input_returns_empty_with_expected_columns(self):
        result = rules.detect_stock_split(pd.DataFrame())
        self.assertTrue(result.empty)
        self.assertEqual(list(result.columns), ["Code", "Date", "rule", "detail"])

    def test_no_matching_titles_returns_empty(self):
        df = self._disclosures([
            {"company_code": "10000", "title": "業績予想の修正に関するお知らせ",
             "pubdate": "2026-08-01", "document_url": None},
        ])
        result = rules.detect_stock_split(df)
        self.assertTrue(result.empty)


class TestStopHighUnaffected(unittest.TestCase):
    """今回の修正はstock_splitのみが対象。stop_highの既存挙動を変えていないことを確認する。"""

    def test_stop_high_detection_still_works(self):
        quotes = pd.DataFrame({
            "Code": ["1000", "2000"],
            "Date": ["2026-08-03", "2026-08-03"],
            "C": [500.0, 300.0],
            "UL": [1, 0],
        })
        result = rules.detect_stop_high(quotes)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["Code"], "1000")
        self.assertEqual(result.iloc[0]["rule"], "stop_high")


if __name__ == "__main__":
    unittest.main()
