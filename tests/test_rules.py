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

    def test_doubling_growth_also_tagged_as_major_and_explosive(self):
        # +100%以上(1年で2倍)は、数値としてsales_growth_major(+20%以上)・
        # sales_growth_explosive(+50%以上)も満たすため、2つのruleタグを
        # すべて持つ行として返す（2026-08-24にユーザーの指定で追加）。
        # なお"sales_growth_doubling"自体はこの関数ではなく
        # detect_current_sales_doublingが別途判定する（2026-08-25のCodex
        # レビューで指摘・修正。下記TestDetectCurrentSalesDoubling参照）。
        statements = pd.DataFrame([
            _row("1234", "1Q", "2025-06-30", "2025-08-10", 100, 10),
            _row("1234", "1Q", "2026-06-30", "2026-08-10", 210, 21),  # +110%
        ])
        result = rules.detect_sales_growth(statements)
        self.assertEqual(len(result), 2)
        self.assertEqual(
            set(result["rule"]),
            {"sales_growth_major", "sales_growth_explosive"},
        )
        self.assertNotIn("sales_growth_doubling", set(result["rule"]))


class TestDetectProfitGrowthMajor(unittest.TestCase):
    def test_growth_over_threshold_is_hit(self):
        statements = pd.DataFrame([
            _row("1234", "1Q", "2025-06-30", "2025-08-10", 100, 10),
            _row("1234", "1Q", "2026-06-30", "2026-08-10", 130, 16),  # 経常利益 +60%
        ])
        result = rules.detect_profit_growth_major(statements)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["Code"], "1234")
        self.assertEqual(result.iloc[0]["rule"], "profit_growth_major")
        self.assertEqual(result.iloc[0]["Date"], pd.Timestamp("2026-08-10"))

    def test_growth_under_threshold_not_hit(self):
        statements = pd.DataFrame([
            _row("1234", "1Q", "2025-06-30", "2025-08-10", 100, 10),
            _row("1234", "1Q", "2026-06-30", "2026-08-10", 130, 13),  # 経常利益 +30%
        ])
        result = rules.detect_profit_growth_major(statements)
        self.assertTrue(result.empty)

    def test_previous_year_loss_is_not_hit(self):
        # 前年同期が赤字（0以下）の場合、「何倍」という比率に意味が無いため対象外
        statements = pd.DataFrame([
            _row("1234", "1Q", "2025-06-30", "2025-08-10", 100, -10),
            _row("1234", "1Q", "2026-06-30", "2026-08-10", 130, 16),
        ])
        result = rules.detect_profit_growth_major(statements)
        self.assertTrue(result.empty)

    def test_hit_uses_only_primary_rows(self):
        # ヒット自体は実際の開示行に限定する（_is_primary_mask docstring参照）。
        statements = pd.DataFrame([
            _row("1234", "1Q", "2026-06-30", "2026-08-10", 130, 16, is_primary=True),
            _row("1234", "1Q", "2025-06-30", "2026-08-10", 100, 10, is_primary=False),
        ])
        result = rules.detect_profit_growth_major(statements)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["Date"], pd.Timestamp("2026-08-10"))

    def test_correction_below_threshold_supersedes_original_hit(self):
        # 同一期(Code, CurPerType, CurPerEn)が訂正決算短信で再度開示された
        # 場合、開示日が最新の行(訂正後の値)だけを比較対象にする。訂正前の
        # 値がgroupby().shift(1)の並び順次第でヒットに残ってしまわないことを
        # 確認する（2026-08-27のCodexレビューで指摘・修正）。
        statements = pd.DataFrame([
            _row("1234", "1Q", "2025-06-30", "2025-08-10", 100, 10),
            _row("1234", "1Q", "2026-06-30", "2026-08-10", 130, 16),  # 訂正前 +60%
            _row("1234", "1Q", "2026-06-30", "2026-08-20", 130, 11),  # 訂正後 +10%（閾値未満）
        ])
        result = rules.detect_profit_growth_major(statements)
        self.assertTrue(result.empty)

    def test_same_day_tie_breaks_deterministically_by_input_order(self):
        # 同一期(Code, CurPerType, CurPerEn)が同一日に複数回開示された場合
        # （同日中の訂正等）、DiscDateだけではタイになる。安定ソート
        # (kind="stable")により、取得元が返した順序で最後に並んでいる行
        # （＝APIの返却順で後に処理された、より新しい情報とみなせる行）が
        # 決定的に優先されることを確認する（2026-08-27の3巡目のCodexレビューで
        # 指摘・修正）。
        statements = pd.DataFrame([
            _row("1234", "1Q", "2025-06-30", "2025-08-10", 100, 10),
            _row("1234", "1Q", "2026-06-30", "2026-08-10", 130, 12),  # 同日1件目 +20%（閾値未満）
            _row("1234", "1Q", "2026-06-30", "2026-08-10", 130, 16),  # 同日2件目 +60%（入力順で後）
        ])
        result = rules.detect_profit_growth_major(statements)
        self.assertEqual(len(result), 1)

    def test_correction_above_threshold_is_hit_with_corrected_date(self):
        statements = pd.DataFrame([
            _row("1234", "1Q", "2025-06-30", "2025-08-10", 100, 10),
            _row("1234", "1Q", "2026-06-30", "2026-08-10", 130, 12),  # 訂正前 +20%（閾値未満）
            _row("1234", "1Q", "2026-06-30", "2026-08-20", 130, 16),  # 訂正後 +60%
        ])
        result = rules.detect_profit_growth_major(statements)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["Date"], pd.Timestamp("2026-08-20"))


class TestDetectCurrentSalesDoubling(unittest.TestCase):
    def test_growth_over_threshold_on_latest_disclosure_is_hit(self):
        statements = pd.DataFrame([
            _row("1234", "1Q", "2025-06-30", "2025-08-10", 100, 10),
            _row("1234", "1Q", "2026-06-30", "2026-08-10", 210, 21),  # +110%
        ])
        result = rules.detect_current_sales_doubling(statements)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["Code"], "1234")
        self.assertEqual(result.iloc[0]["rule"], "sales_growth_doubling")

    def test_growth_just_under_doubling_not_tagged_as_doubling(self):
        statements = pd.DataFrame([
            _row("1234", "1Q", "2025-06-30", "2025-08-10", 100, 10),
            _row("1234", "1Q", "2026-06-30", "2026-08-10", 190, 19),  # +90%
        ])
        result = rules.detect_current_sales_doubling(statements)
        self.assertTrue(result.empty)

    def test_growth_cooled_down_since_a_past_doubling_disclosure_is_not_a_hit(self):
        # 過去に一度+150%(2倍)を達成していても、最新の開示時点の前年同期比が
        # 閾値未満まで鈍化していれば、もうヒットしない（銘柄が"現在"2倍成長
        # という状態にあるかを判定するための関数であり、過去に一度でも
        # 閾値を満たした開示がいつまでも残り続けるバグの回帰テスト。
        # 2026-08-25のCodexレビューで指摘・修正）。
        statements = pd.DataFrame([
            _row("1234", "1Q", "2024-06-30", "2024-08-10", 100, 10),
            _row("1234", "1Q", "2025-06-30", "2025-08-10", 250, 25),  # +150%(過去の開示)
            _row("1234", "1Q", "2026-06-30", "2026-08-10", 260, 26),  # +4%(最新の開示)
        ])
        result = rules.detect_current_sales_doubling(statements)
        self.assertTrue(result.empty)

    def test_only_the_latest_disclosure_per_code_is_considered(self):
        statements = pd.DataFrame([
            _row("1234", "1Q", "2025-06-30", "2025-08-10", 100, 10),
            _row("1234", "1Q", "2026-06-30", "2026-08-10", 210, 21),  # +110%(最新)
            _row("5678", "1Q", "2025-06-30", "2025-08-10", 100, 10),
            _row("5678", "1Q", "2026-06-30", "2026-08-10", 130, 13),  # +30%(2倍未満)
        ])
        result = rules.detect_current_sales_doubling(statements)
        self.assertEqual(set(result["Code"]), {"1234"})

    def test_missing_sales_in_latest_period_does_not_fall_back_to_an_older_hit(self):
        # 直近の決算期(2026年1Q)の開示がSalesを欠いている場合、その1件は
        # 判定不能（ヒットなし）として扱うべきで、数値が揃っている1つ古い
        # 決算期(2025年1Q, +110%)にフォールバックして誤ってヒットさせては
        # いけない。Salesの欠損チェックを「直近の決算期を選ぶ前」に行うと
        # このフォールバックが起きてしまう（2026-08-25の4巡目のCodex
        # レビューで指摘・修正）。
        statements = pd.DataFrame([
            _row("1234", "1Q", "2024-06-30", "2024-08-10", 100, 10),
            _row("1234", "1Q", "2025-06-30", "2025-08-10", 210, 21),  # +110%(1つ古い決算期)
            _row("1234", "1Q", "2026-06-30", "2026-08-10", None, 30),  # 直近の決算期、Sales欠損
        ])
        result = rules.detect_current_sales_doubling(statements)
        self.assertTrue(result.empty)

    def test_correction_of_an_older_period_does_not_override_the_latest_period(self):
        # 2026年1Q(直近の決算期)が開示された後に、それより古い2025年1Qの
        # 数値が訂正された場合、訂正開示の開示日(DiscDate)の方が新しいため
        # 開示日基準で「最新」を選ぶと、古い決算期同士(2025 vs 2024)の
        # 比較結果を返してしまう。今知りたいのはあくまで直近の決算期
        # (2026年1Q)の成長率であるべき（2026-08-25の3巡目のCodexレビューで
        # 指摘・修正）。
        statements = pd.DataFrame([
            _row("1234", "1Q", "2024-06-30", "2024-08-10", 100, 10),
            _row("1234", "1Q", "2025-06-30", "2025-08-10", 100, 10),  # 当初開示
            _row("1234", "1Q", "2026-06-30", "2026-08-10", 250, 25),  # 直近の決算期 (vs 訂正後2025: +25%)
            _row("1234", "1Q", "2025-06-30", "2026-08-20", 200, 20),  # 2025年1Qの訂正開示(開示日は最新)
        ])
        result = rules.detect_current_sales_doubling(statements)
        # 直近の決算期(2026年1Q)の成長率は250/200-1=25%で閾値未満のためヒットしない
        # （訂正開示の開示日が最新という理由だけで2025年1Q(vs 2024年1Q, +100%)が
        # 選ばれてしまうと誤ってヒットしてしまう）。
        self.assertTrue(result.empty)

    def test_stale_synthetic_comparator_is_superseded_by_a_corrected_primary_disclosure(self):
        # 2026年1Qの前年同期比較の基準となる2025年1Qについて、2026年1Q開示に
        # 埋め込まれた合成行(IsPrimary=False、訂正前の100)と、別途出た
        # 2025年1Q自体の訂正開示(IsPrimary=True、訂正後の200)の両方が
        # 存在する場合、実際の開示である訂正後の200を優先すべき。合成行を
        # 残したままだと、同じ決算期(PeriodEnd)が重複したままshift(1)する
        # 際にたまたま古い合成行の方が直前の行として選ばれてしまい、訂正後
        # の正しい成長率(25%)ではなく訂正前の古い数値と比較した誤った
        # 成長率(150%)でヒットしてしまう（2026-08-25の6巡目のCodexレビューで
        # 指摘・修正）。
        statements = pd.DataFrame([
            _row("1234", "1Q", "2025-06-30", "2026-08-10", 100, 10, is_primary=False),  # 2026年1Q開示の合成行(訂正前)
            _row("1234", "1Q", "2025-06-30", "2026-08-20", 200, 20, is_primary=True),  # 2025年1Qの訂正開示(実際の開示)
            _row("1234", "1Q", "2026-06-30", "2026-08-10", 250, 25, is_primary=True),  # 2026年1Qの実際の開示
        ])
        result = rules.detect_current_sales_doubling(statements)
        # 訂正後の200と比較した成長率は25%で閾値未満のためヒットしない
        self.assertTrue(result.empty)

    def test_restated_synthetic_comparator_supersedes_an_older_primary_disclosure(self):
        # 上のテストとは逆に、2025年1Qの実際の開示(当初の100)よりも、
        # 2026年1Q開示に埋め込まれた合成行(遡及的に修正された200)の方が
        # 開示日が新しい場合、この合成行の方を比較対象として優先すべき。
        # 合成行のDiscDateは、それが埋め込まれていた親の開示（この場合は
        # 2026年1Q開示）の開示日を表すため、実際の開示より新しい情報を
        # 反映していることがある。「実際の開示なら常に優先」という単純な
        # ルールだと、この場合は逆に古い当初の100を使ってしまい、
        # 誤って+150%(2倍)と判定してしまう（2026-08-25の7巡目のCodexレビュー
        # で指摘・修正）。
        statements = pd.DataFrame([
            _row("1234", "1Q", "2025-06-30", "2025-08-10", 100, 10, is_primary=True),  # 2025年1Qの当初開示
            _row("1234", "1Q", "2025-06-30", "2026-08-10", 200, 20, is_primary=False),  # 2026年1Q開示に埋め込まれた合成行(遡及修正後)
            _row("1234", "1Q", "2026-06-30", "2026-08-10", 250, 25, is_primary=True),  # 2026年1Qの実際の開示
        ])
        result = rules.detect_current_sales_doubling(statements)
        # 遡及修正後の200と比較した成長率は25%で閾値未満のためヒットしない
        self.assertTrue(result.empty)

    def test_amended_disclosure_for_same_period_does_not_break_yoy_comparison(self):
        # 同一決算期(1Q 2026-06-30)について訂正開示が後から出た場合
        # （当初開示と訂正開示の2行が同じ決算期に存在する）、訂正後の行が
        # 「同じ決算期の当初開示行」と比較されてしまい期間差0日になり、
        # 本来比較可能なはずの前年同期比較が不能と判定されるバグの回帰
        # テスト（2026-08-25のCodexレビューで指摘・修正）。
        statements = pd.DataFrame([
            _row("1234", "1Q", "2025-06-30", "2025-08-10", 100, 10),
            _row("1234", "1Q", "2026-06-30", "2026-08-10", 200, 20),  # 当初開示 +100%
            _row("1234", "1Q", "2026-06-30", "2026-08-20", 220, 22),  # 訂正開示 +120%
        ])
        result = rules.detect_current_sales_doubling(statements)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["Code"], "1234")
        self.assertEqual(result.iloc[0]["Date"], pd.Timestamp("2026-08-20"))

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


class TestDetectJpxNikkei400Selection(unittest.TestCase):
    """「JPX日経インデックス400」構成銘柄への選定・採用の検出（2026-08-24に
    ユーザーの指定で追加）。実際のTDnet開示タイトル（2021〜2026年、26件）で
    確認済みの実例と、全角/半角表記ゆれ、無関係な開示（ETF自体の決算短信・
    「JPX日経中小型株指数」）を使う。
    """

    def _disclosures(self, rows):
        return pd.DataFrame(rows, columns=["company_code", "title", "pubdate", "document_url"])

    def test_real_example_selection_notice(self):
        # 東和薬品 2021-08-06
        df = self._disclosures([
            {"company_code": "45050", "title": "「JPX日経インデックス400」構成銘柄への採用に関するお知らせ",
             "pubdate": "2021-08-06 16:30:00", "document_url": None},
        ])
        result = rules.detect_jpx_nikkei_400_selection(df)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["rule"], "jpx_nikkei_400")

    def test_fullwidth_and_spaced_variants_are_matched(self):
        # アインＨＤ 2021-08-11（全角数字）、フェローテック 2022-08-08（半角スペース入り）
        df = self._disclosures([
            {"company_code": "90670", "title": "「JPX日経インデックス４００」構成銘柄選定継続のお知らせ",
             "pubdate": "2021-08-11 13:00:00", "document_url": None},
            {"company_code": "63690", "title": "当社株式の「JPX 日経インデックス 400」構成銘柄選定に関するお知らせ",
             "pubdate": "2022-08-08 12:00:00", "document_url": None},
        ])
        result = rules.detect_jpx_nikkei_400_selection(df)
        self.assertEqual(len(result), 2)

    def test_etf_fund_settlement_report_is_not_matched(self):
        # ＮＦＪＰＸ４００・ＭＸＳ４００等、指数連動型ETF自身の決算短信は
        # 「構成銘柄」を含まないため誤検出しない。
        df = self._disclosures([
            {"company_code": "13850", "title": "NEXT FUNDS JPX日経インデックス400連動型上場投信 決算短信",
             "pubdate": "2021-11-17 13:00:00", "document_url": None},
        ])
        result = rules.detect_jpx_nikkei_400_selection(df)
        self.assertTrue(result.empty)

    def test_jpx_nikkei_mid_small_index_alone_is_not_matched(self):
        # 「JPX日経中小型株指数」は別指数のため、「400」を含まない限り誤検出しない。
        df = self._disclosures([
            {"company_code": "99999", "title": "「JPX日経中小型株指数」構成銘柄への選定に関するお知らせ",
             "pubdate": "2021-08-10 11:30:00", "document_url": None},
        ])
        result = rules.detect_jpx_nikkei_400_selection(df)
        self.assertTrue(result.empty)

    def test_empty_input_returns_empty_with_expected_columns(self):
        result = rules.detect_jpx_nikkei_400_selection(pd.DataFrame())
        self.assertTrue(result.empty)
        self.assertEqual(list(result.columns), ["Code", "Date", "rule", "detail"])

    def test_removal_notice_is_not_matched_as_selection(self):
        # 「構成銘柄からの除外」のように選定と逆方向の発表は誤検出しない
        # （2026-08-24のCodexレビューで指摘・修正。実データでは除外系の自社
        # 開示は確認できなかったが、将来的な発生に備えて防御的に除外する）。
        df = self._disclosures([
            {"company_code": "10000", "title": "「JPX日経インデックス400」構成銘柄からの除外に関するお知らせ",
             "pubdate": "2026-08-01 15:00:00", "document_url": None},
        ])
        result = rules.detect_jpx_nikkei_400_selection(df)
        self.assertTrue(result.empty)


if __name__ == "__main__":
    unittest.main()
