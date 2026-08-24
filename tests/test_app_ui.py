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
from unittest.mock import patch

import pandas as pd
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


def _regional_store_for_sort_test():
    # 「現在も地方単独上場の銘柄を先頭に」ソートの検証用データ。
    # 11110は現在も地方単独上場、22220は既に東証へ移籍完了済み
    # （過去の移籍イベントだけが①のイベント一覧に残っている）。
    company_status = pd.DataFrame([
        {"Code": "11110", "CompanyName": "現在も地方単独", "MarketsString": "福",
         "LastSeenDate": pd.Timestamp("2026-08-01"), "LastDelistingDate": pd.NaT,
         "IsDelisted": False, "CurrentPrice": None, "CurrentPriceNote": ""},
        {"Code": "22220", "CompanyName": "東証移籍済み", "MarketsString": "東福",
         "LastSeenDate": pd.Timestamp("2026-08-10"), "LastDelistingDate": pd.NaT,
         "IsDelisted": False, "CurrentPrice": None, "CurrentPriceNote": ""},
    ])
    listing_events = pd.DataFrame([
        # 発表日は22220の方が新しいため、「イベントの新しい順」なら22220が先頭になる。
        {"id": "e1", "Code": "22220", "CompanyName": "東証移籍済み", "Date": pd.Timestamp("2026-08-10"),
         "MarketsString": "東福", "TargetMarkets": "東", "Stage": "完了", "IsTokyoRelated": True,
         "Title": "東証移籍完了", "Url": None},
    ])
    major_events = pd.DataFrame(
        columns=["id", "Code", "CompanyName", "Date", "MarketsString", "Title", "Url", "MatchedKeyword"]
    )
    return {
        "company_status": company_status,
        "listing_events": listing_events,
        "major_events": major_events,
        "statements": pd.DataFrame(),
    }


class TestRegionalSortByCurrentlyRegional(unittest.TestCase):
    """「並び順」に追加した「現在も地方単独上場の銘柄を先頭に」の単体テスト
    （2026-08-24に追加。東証へ既に移籍完了した銘柄と、今も地方取引所のみに
    上場している銘柄を区別してソートできるようにするための機能）。
    """

    def test_currently_regional_company_sorted_first(self):
        with patch("src.regional_stocks.load_regional_store", return_value=_regional_store_for_sort_test()):
            at = AppTest.from_file(APP_PATH)
            at.run(timeout=30)
            at.checkbox(key="regional_only").set_value(True)
            at.run(timeout=30)
            sort_radio = next(r for r in at.radio if "現在も地方単独上場の銘柄を先頭に" in r.options)
            sort_radio.set_value("現在も地方単独上場の銘柄を先頭に")
            at.run(timeout=30)

        self.assertEqual(len(at.exception), 0)
        codes = list(at.dataframe[0].value["コード"])
        self.assertEqual(codes.index("11110"), 0)
        self.assertLess(codes.index("11110"), codes.index("22220"))

    def test_default_event_date_sort_still_puts_newer_event_first(self):
        # 既存の「イベントの新しい順」（デフォルト）は今回の変更で壊れていない
        # ことの回帰確認（22220の方が発表日が新しいため先頭になる）。
        with patch("src.regional_stocks.load_regional_store", return_value=_regional_store_for_sort_test()):
            at = AppTest.from_file(APP_PATH)
            at.run(timeout=30)
            at.checkbox(key="regional_only").set_value(True)
            at.run(timeout=30)

        self.assertEqual(len(at.exception), 0)
        codes = list(at.dataframe[0].value["コード"])
        self.assertEqual(codes[0], "22220")


def _fake_run_screening_with_message(client, start, end, selected_rules=None):
    """run_screeningの戻り値契約(hits_df, messages)に合わせたテスト用スタブ。

    以前はPython標準のwarnings.warnでメッセージを発していたが、
    warnings.catch_warnings()はプロセスグローバルな状態を書き換えるため
    スレッドセーフでなく、Streamlitの複数セッション同時実行下では別セッション
    の警告を誤って捕捉しうる。そのため戻り値として明示的に返す方式に変更した
    （2026-08-24の2巡目のCodexレビューで指摘・修正）。
    """
    hits = pd.DataFrame(columns=["Code", "CompanyName", "Sector", "Rule", "RuleLabel", "Date", "Detail"])
    return hits, ["テスト用の警告メッセージ：200件を超えました"]


class TestTdnetDisclosureLimitWarningIsVisible(unittest.TestCase):
    """run_screeningが返す注意メッセージ（TDnet開示件数の上限超過等）が、
    サーバーログだけでなくst.warningとして画面にも表示されることの確認
    （2026-08-24にユーザーが指定した機能）。
    """

    def test_run_screening_message_is_shown_as_st_warning(self):
        with patch("src.pipeline.run_screening", side_effect=_fake_run_screening_with_message), \
                patch("src.jquants_client.JQuantsClient.__init__", return_value=None):
            at = AppTest.from_file(APP_PATH)
            at.run(timeout=30)
            at.pills(key="material_events_pills").set_value(["stop_high"])
            at.run(timeout=30)
            next(b for b in at.button if b.label == "スクリーニング実行").click()
            at.run(timeout=30)

        self.assertEqual(len(at.exception), 0)
        warning_texts = [w.value for w in at.warning]
        self.assertTrue(any("200件を超えました" in t for t in warning_texts))

    def test_warning_survives_a_later_unrelated_rerun(self):
        # st.warningをボタン押下時のrunでしか出さないと、AND/OR切り替え等
        # 別ウィジェット操作による再実行でsummary(結果)はsession_stateから
        # 表示され続けるのに警告だけ消えてしまう（2026-08-24のCodexレビューで
        # 指摘・修正: summary_warningsとしてsession_stateに永続化し、結果を
        # 表示するたびに毎回re-renderするようにした）。
        with patch("src.pipeline.run_screening", side_effect=_fake_run_screening_with_message), \
                patch("src.jquants_client.JQuantsClient.__init__", return_value=None):
            at = AppTest.from_file(APP_PATH)
            at.run(timeout=30)
            at.pills(key="material_events_pills").set_value(["stop_high"])
            at.run(timeout=30)
            next(b for b in at.button if b.label == "スクリーニング実行").click()
            at.run(timeout=30)
            # ボタンを押さない別操作による再実行（AND/OR切り替え）を模す。
            at.radio(key="event_logic").set_value("AND検索")
            at.run(timeout=30)

        self.assertEqual(len(at.exception), 0)
        warning_texts = [w.value for w in at.warning]
        self.assertTrue(any("200件を超えました" in t for t in warning_texts))


if __name__ == "__main__":
    unittest.main()
