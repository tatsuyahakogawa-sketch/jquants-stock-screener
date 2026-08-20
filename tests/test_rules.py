"""src/rules.py の単体テスト。

実行方法:
    python -m unittest discover tests
"""
from __future__ import annotations

import unittest

import pandas as pd

from src import rules


def _row(code, per_type, per_end, disc_date, sales, odp, is_primary=None):
    row = {
        "Code": code, "CurPerType": per_type, "CurPerEn": pd.Timestamp(per_end),
        "DiscDate": pd.Timestamp(disc_date), "Sales": sales, "OdP": odp,
    }
    if is_primary is not None:
        row["IsPrimary"] = is_primary
    return row


class TestDetectTwoQuarterGrowth(unittest.TestCase):
    def test_hit_uses_only_primary_rows_for_the_disclosure_sequence(self):
        # src/tdnet_xbrl.pyの合成行(IsPrimary=False。開示に埋め込まれた前年
        # 同期実績)が「直前の開示」判定に混ざると、実在しない開示が間に
        # 挟まったことになり2期連続判定が常に不成立になっていた回帰テスト
        # （2026-08-19のCodexレビューで指摘・修正）。
        statements = pd.DataFrame([
            # 1Q(今年)の開示: 当期行+開示に埋め込まれた前年同期の合成行
            _row("1234", "1Q", "2026-06-30", "2026-08-10", 120, 12, is_primary=True),
            _row("1234", "1Q", "2025-06-30", "2026-08-10", 100, 10, is_primary=False),
            # 2Q(今年)の開示: 当期行+開示に埋め込まれた前年同期の合成行
            _row("1234", "2Q", "2026-09-30", "2026-11-10", 250, 25, is_primary=True),
            _row("1234", "2Q", "2025-09-30", "2026-11-10", 200, 20, is_primary=False),
        ])
        result = rules.detect_two_quarter_growth(statements)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["Code"], "1234")
        self.assertEqual(result.iloc[0]["Date"], pd.Timestamp("2026-11-10"))

    def test_single_real_disclosure_is_not_two_in_a_row(self):
        # 実際の開示が1回しか無い場合（合成行を含めても）、2期連続は成立しない。
        statements = pd.DataFrame([
            _row("1234", "1Q", "2026-06-30", "2026-08-10", 120, 12, is_primary=True),
            _row("1234", "1Q", "2025-06-30", "2026-08-10", 100, 10, is_primary=False),
        ])
        result = rules.detect_two_quarter_growth(statements)
        self.assertTrue(result.empty)

    def test_gap_quarter_is_not_treated_as_two_in_a_row(self):
        # 地方株はTDnet添付ファイルの保持期限切れ等で特定の四半期(例: 2Q)だけ
        # 取得できないことがある。1Qと3Qの決算期末は半年近く離れており
        # 「連続した2期」ではないため、両方が増収増益でもヒットしない
        # （2026-08-19の3巡目のCodexレビューで指摘・修正）。
        statements = pd.DataFrame([
            _row("1234", "1Q", "2025-06-30", "2025-08-10", 100, 10, is_primary=True),
            _row("1234", "1Q", "2026-06-30", "2026-08-10", 120, 12, is_primary=True),  # +20% (2Qは欠落)
            _row("1234", "3Q", "2025-12-31", "2025-11-10", 200, 20, is_primary=True),
            _row("1234", "3Q", "2026-12-31", "2026-11-10", 250, 25, is_primary=True),  # +25%
        ])
        result = rules.detect_two_quarter_growth(statements)
        self.assertTrue(result.empty)

    def test_without_isprimary_column_behaves_as_before(self):
        # J-Quants由来のデータ(1開示=1行、IsPrimary列なし)では、全行を対象にした
        # 従来通りの挙動が変わらないことを確認する（後方互換の回帰テスト）。
        statements = pd.DataFrame([
            _row("5678", "1Q", "2024-06-30", "2024-08-10", 90, 9),
            _row("5678", "2Q", "2024-09-30", "2024-11-10", 180, 18),
            _row("5678", "1Q", "2025-06-30", "2025-08-10", 100, 10),
            _row("5678", "2Q", "2025-09-30", "2025-11-10", 210, 21),
        ])
        result = rules.detect_two_quarter_growth(statements)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["Date"], pd.Timestamp("2025-11-10"))


class TestDetectSalesGrowth(unittest.TestCase):
    def test_explosive_growth_also_tagged_as_major(self):
        # sales_growth_major(ラベル表記「+20%以上」)は数値としては
        # sales_growth_explosive(+50%以上)を包含するため、+50%以上の成長は
        # 両方のruleタグを持つ行として返す。片方だけを選んだユーザーの絞り込み
        # から、実際には+20%以上でもある爆発的成長銘柄が漏れないようにする
        # ため（2026-08-19のCodexレビューで指摘・修正）。
        statements = pd.DataFrame([
            _row("1234", "1Q", "2025-06-30", "2025-08-10", 100, 10),
            _row("1234", "1Q", "2026-06-30", "2026-08-10", 160, 16),  # +60%
        ])
        result = rules.detect_sales_growth(statements)
        self.assertEqual(len(result), 2)
        self.assertEqual(set(result["rule"]), {"sales_growth_major", "sales_growth_explosive"})
        self.assertTrue((result["Code"] == "1234").all())
        self.assertTrue((result["Date"] == pd.Timestamp("2026-08-10")).all())

    def test_moderate_growth_tagged_as_major_only(self):
        statements = pd.DataFrame([
            _row("1234", "1Q", "2025-06-30", "2025-08-10", 100, 10),
            _row("1234", "1Q", "2026-06-30", "2026-08-10", 130, 13),  # +30%
        ])
        result = rules.detect_sales_growth(statements)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["rule"], "sales_growth_major")

    def test_excludes_synthetic_rows_from_hits(self):
        # 2026年の実際の開示が一度も取得できず(XBRL保持期限切れ等)、2025年の
        # 実開示(埋め込み前年同期=2024年)と2027年の実開示(埋め込み前年同期=
        # 2026年)だけが残っている状況。2026年の合成行(IsPrimary=False、
        # DiscDateは2027年の開示日を借用)が2025年の実開示と比べて閾値以上
        # 増収していても、それ自体をヒットとして出してはいけない。ヒットの
        # 日付が2027年の実開示日になるため、実際は2027年に減収したにも
        # 関わらず「増収」と表示されてしまう（2026-08-19の5巡目のCodexレビューで
        # 指摘・修正）。
        statements = pd.DataFrame([
            _row("1234", "1Q", "2024-06-30", "2025-08-10", 80, 8, is_primary=False),
            _row("1234", "1Q", "2025-06-30", "2025-08-10", 90, 9, is_primary=True),
            # 2026年の合成行: 2025年比+25%(閾値超え)だが実際の開示ではない
            _row("1234", "1Q", "2026-06-30", "2027-08-10", 100, 10, is_primary=False),
            # 2027年の実開示: 2026年比で実際には減収
            _row("1234", "1Q", "2027-06-30", "2027-08-10", 70, 7, is_primary=True),
        ])
        result = rules.detect_sales_growth(statements)
        self.assertTrue(result.empty)


class TestDetectEarningsBeat(unittest.TestCase):
    def test_excludes_synthetic_rows_from_hits(self):
        # 2025年の実際のFY開示が一度も取得できず、Q3(2025)の実開示(会社予想
        # FNP=100)と、2026年のFY開示に埋め込まれた2025年の合成行(実績NP=150、
        # DiscDateは2026年の開示日を借用)だけが残っている状況。この合成行が
        # Q3時点の予想を上回っていても、それ自体をヒットとして出してはいけない
        # （2026-08-19の5巡目のCodexレビューで指摘されたdetect_sales_growthと
        # 同種の問題が、同じ構造を持つdetect_earnings_beatにもあったため修正）。
        statements = pd.DataFrame([
            {
                "Code": "1234", "CurPerType": "3Q", "CurFYEn": pd.Timestamp("2025-06-30"),
                "DiscDate": pd.Timestamp("2025-02-10"), "NP": None, "FNP": 100,
            },
            {
                "Code": "1234", "CurPerType": "FY", "CurFYEn": pd.Timestamp("2025-06-30"),
                "DiscDate": pd.Timestamp("2026-08-10"), "NP": 150, "FNP": None, "IsPrimary": False,
            },
        ])
        result = rules.detect_earnings_beat(statements)
        self.assertTrue(result.empty)


if __name__ == "__main__":
    unittest.main()
