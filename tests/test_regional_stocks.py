"""src/regional_stocks.py の単体テスト。

「地方株」ページ向けの、地方単独上場企業の検出・株価取得・増分ストアを
検証する。TDnet/yfinanceへの実際のネットワークアクセスは行わない
（unittest.mock.patchで差し替える）。

実行方法:
    python -m unittest discover tests
"""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import regional_stocks

_MOD = "src.regional_stocks"


def _disclosure(id_, code, name, title, pubdate, markets_string, url="https://example.com/x.pdf", url_xbrl=None):
    return {
        "id": id_,
        "company_code": code,
        "company_name": name,
        "title": title,
        "pubdate": pubdate,
        "markets_string": markets_string,
        "document_url": url,
        "url_xbrl": url_xbrl,
    }


class TestRegionalStatementRules(unittest.TestCase):
    def test_profit_doubling_not_offered_yet(self):
        # profit_doublingは4年分の比較対象が蓄積されるまで判定不能なため、
        # UIの選択肢からは意図的に外している（2026-08-19の4巡目のCodexレビューで
        # 指摘: 判定不能なのに「該当なし」としか表示できず紛らわしいため）。
        self.assertNotIn("profit_doubling", regional_stocks.REGIONAL_STATEMENT_RULES)


class TestIsRegionalOnly(unittest.TestCase):
    def test_tokyo_only_is_not_regional(self):
        self.assertFalse(regional_stocks.is_regional_only("東"))

    def test_dual_listed_with_tokyo_is_not_regional_only(self):
        self.assertFalse(regional_stocks.is_regional_only("東福"))

    def test_fukuoka_only_is_regional(self):
        self.assertTrue(regional_stocks.is_regional_only("福"))

    def test_sapporo_fukuoka_dual_regional_is_regional(self):
        self.assertTrue(regional_stocks.is_regional_only("札福"))

    def test_blank_is_not_regional(self):
        self.assertFalse(regional_stocks.is_regional_only(""))
        self.assertFalse(regional_stocks.is_regional_only(None))

    def test_nan_does_not_raise(self):
        # markets_string欠損時、pandasがNaN(float)を入れることがある。
        # bool(NaN)はTrueなので、not markets_stringだけのガードでは
        # 弾けずにTypeErrorになっていた回帰を防ぐ。
        self.assertFalse(regional_stocks.is_regional_only(float("nan")))


class TestRegionalMarketsIn(unittest.TestCase):
    def test_single_market(self):
        self.assertEqual(regional_stocks.regional_markets_in("福"), ["福証"])

    def test_multiple_markets_preserve_order(self):
        self.assertEqual(regional_stocks.regional_markets_in("札福"), ["福証", "札証"])

    def test_tokyo_char_ignored(self):
        self.assertEqual(regional_stocks.regional_markets_in("東"), [])

    def test_nan_does_not_raise(self):
        self.assertEqual(regional_stocks.regional_markets_in(float("nan")), [])


class TestAllMarketsIn(unittest.TestCase):
    def test_includes_tokyo(self):
        self.assertEqual(regional_stocks.all_markets_in("東福"), ["東証", "福証"])

    def test_regional_only(self):
        self.assertEqual(regional_stocks.all_markets_in("札福"), ["福証", "札証"])

    def test_nan_does_not_raise(self):
        self.assertEqual(regional_stocks.all_markets_in(float("nan")), [])


class TestLegacyTicker(unittest.TestCase):
    def test_strips_only_trailing_suffix_digit(self):
        # 実コード自体が0で終わる場合にrstrip("0")で削り過ぎないことを確認
        # （9388のケースは末尾が0以外なので単純なrstripでも壊れないが、
        # 実コードが"7200"のようなケースでの回帰を防ぐための固定長スライス）。
        self.assertEqual(regional_stocks._legacy_ticker("93880"), "9388")

    def test_code_ending_in_zero_is_not_over_stripped(self):
        self.assertEqual(regional_stocks._legacy_ticker("72000"), "7200")

    def test_alphanumeric_code(self):
        self.assertEqual(regional_stocks._legacy_ticker("353A0"), "353A")

    def test_short_code_returned_as_is(self):
        self.assertEqual(regional_stocks._legacy_ticker("1234"), "1234")


class TestClassifyListingStage(unittest.TestCase):
    def test_application_stage(self):
        self.assertEqual(
            regional_stocks._classify_listing_stage("東京証券取引所新規上場申請及び福岡証券取引所本則市場へ市場変更申請のお知らせ"),
            "申請",
        )

    def test_approval_stage(self):
        self.assertEqual(
            regional_stocks._classify_listing_stage("名古屋証券取引所ネクスト市場への上場承認に関するお知らせ"),
            "承認",
        )

    def test_completed_stage(self):
        self.assertEqual(
            regional_stocks._classify_listing_stage("福岡証券取引所Fukuoka PRO Marketへの上場のお知らせ"),
            "上場",
        )

    def test_completed_dual_listing_stage(self):
        self.assertEqual(
            regional_stocks._classify_listing_stage("福岡証券取引所本則市場への重複上場に関するお知らせ"),
            "上場",
        )

    def test_unrelated_title_returns_none(self):
        self.assertIsNone(regional_stocks._classify_listing_stage("自己株式の取得結果に関するお知らせ"))

    def test_title_without_listing_keyword_returns_none(self):
        self.assertIsNone(regional_stocks._classify_listing_stage("業績予想の修正に関するお知らせ"))

    def test_delisting_application_is_not_a_new_listing(self):
        # 「上場廃止申請」は"上場"も"申請"も含むが、新規上場の申請ではない
        # （逆方向のイベント）。誤って"申請"段階と判定しないことを確認。
        self.assertIsNone(regional_stocks._classify_listing_stage("札幌証券取引所上場廃止申請に関するお知らせ"))

    def test_delisting_notice_is_not_a_completed_listing(self):
        self.assertIsNone(regional_stocks._classify_listing_stage("福岡証券取引所への上場廃止に関するお知らせ"))

    def test_approval_wins_when_title_also_contains_application_word(self):
        # 承認の対象として"申請"という語が埋め込まれているタイトルもある。
        # 承認済みなのに"申請"段階に格下げされないことを確認する。
        self.assertEqual(
            regional_stocks._classify_listing_stage("福岡証券取引所本則市場への上場申請の承認に関するお知らせ"),
            "承認",
        )


class TestDetectRegionalListingEvents(unittest.TestCase):
    def test_detects_tokyo_application_from_regional_only_company(self):
        df = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ",
                        "東京証券取引所新規上場申請及び福岡証券取引所本則市場へ市場変更申請のお知らせ",
                        "2026-07-15 15:30:00", "福"),
        ])
        result = regional_stocks.detect_regional_listing_events(df)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["Stage"], "申請")
        self.assertTrue(result.iloc[0]["IsTokyoRelated"])

    def test_excludes_disclosures_from_tokyo_listed_companies(self):
        df = pd.DataFrame([
            _disclosure("1", "72030", "トヨタ", "株式分割に関するお知らせ", "2026-07-15 15:30:00", "東"),
        ])
        result = regional_stocks.detect_regional_listing_events(df)
        self.assertTrue(result.empty)

    def test_non_tokyo_regional_listing_is_not_flagged_as_tokyo_related(self):
        df = pd.DataFrame([
            _disclosure("1", "588A0", "Ｓ－アットマークテク",
                        "札幌証券取引所Sapporo PRO Frontier Market への上場のお知らせ",
                        "2026-06-30 08:00:00", "札"),
        ])
        result = regional_stocks.detect_regional_listing_events(df)
        self.assertEqual(len(result), 1)
        self.assertFalse(result.iloc[0]["IsTokyoRelated"])
        self.assertEqual(result.iloc[0]["Stage"], "上場")

    def test_empty_input_returns_empty_with_columns(self):
        result = regional_stocks.detect_regional_listing_events(pd.DataFrame())
        self.assertTrue(result.empty)
        self.assertListEqual(list(result.columns), regional_stocks._LISTING_EVENTS_COLUMNS)

    def test_unrelated_disclosure_from_regional_company_is_not_hit(self):
        df = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "自己株式の取得結果に関するお知らせ",
                        "2026-07-16 16:30:00", "福"),
        ])
        result = regional_stocks.detect_regional_listing_events(df)
        self.assertTrue(result.empty)

    def test_completed_tokyo_listing_is_caught_via_was_known_regional(self):
        # 東証上場が完了した開示は、その時点でmarkets_stringに既に"東"が
        # 含まれる（例:"東福"）。markets_stringだけで絞り込むと取り逃すため、
        # was_known_regionalに直前まで地方単独上場だったことを渡すことで
        # 検出できることを確認する。
        df = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "東京証券取引所への上場のお知らせ",
                        "2026-09-01 08:00:00", "東福"),
        ])
        without_context = regional_stocks.detect_regional_listing_events(df)
        self.assertTrue(without_context.empty)

        with_context = regional_stocks.detect_regional_listing_events(df, pd.Series([True], index=df.index))
        self.assertEqual(len(with_context), 1)
        self.assertEqual(with_context.iloc[0]["Stage"], "上場")
        self.assertTrue(with_context.iloc[0]["IsTokyoRelated"])

    def test_unrelated_company_not_known_regional_with_tokyo_word_is_still_excluded(self):
        # markets_stringが地方単独でもなく、was_known_regionalもFalseの
        # 銘柄は対象外のままであること（無関係な東証銘柄まで拾ってしまわ
        # ないことの確認）。
        df = pd.DataFrame([
            _disclosure("1", "72030", "トヨタ", "東京証券取引所プライム市場への上場に関するお知らせ",
                        "2026-09-01 08:00:00", "東"),
        ])
        result = regional_stocks.detect_regional_listing_events(df, pd.Series([False], index=df.index))
        self.assertTrue(result.empty)


class TestComputeWasKnownRegional(unittest.TestCase):
    def test_regional_evidence_applies_to_later_rows_only(self):
        # 地方単独上場の開示(1/10)より後の東証開示(8/1)だけがTrueになり、
        # それより前の行には適用されない（時系列を守る）。
        df = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "福岡証券取引所本則市場への重複上場に関するお知らせ",
                        "2026-01-10 08:00:00", "福"),
            _disclosure("2", "93880", "Ｑ－パパネッツ", "東京証券取引所への上場のお知らせ",
                        "2026-08-01 08:00:00", "東福"),
        ])
        result = regional_stocks.compute_was_known_regional(df)
        self.assertFalse(result.loc[0])  # 1/10時点ではまだ地方単独上場と分かっていない
        self.assertTrue(result.loc[1])   # 8/1時点では1/10の開示から分かっている

    def test_does_not_apply_regional_evidence_retroactively(self):
        # 東証開示(1/10)の方が地方単独上場の開示(8/1)より前にある場合、
        # 東証開示の時点ではまだ地方単独上場と分かっていないため、
        # バッチ全体を見ればコードが一致していても遡って適用しない。
        df = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "東京証券取引所への上場のお知らせ",
                        "2026-01-10 08:00:00", "東福"),
            _disclosure("2", "93880", "Ｑ－パパネッツ", "福岡証券取引所本則市場への重複上場に関するお知らせ",
                        "2026-08-01 08:00:00", "福"),
        ])
        result = regional_stocks.compute_was_known_regional(df)
        self.assertFalse(result.loc[0])

    def test_base_known_codes_apply_from_the_start(self):
        df = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "東京証券取引所への上場のお知らせ",
                        "2026-08-01 08:00:00", "東福"),
        ])
        result = regional_stocks.compute_was_known_regional(df, base_known_codes={"93880"})
        self.assertTrue(result.loc[0])

    def test_empty_input_returns_empty_series(self):
        result = regional_stocks.compute_was_known_regional(pd.DataFrame())
        self.assertTrue(result.empty)


class TestDetectRegionalMajorEvents(unittest.TestCase):
    def test_detects_tob_from_regional_only_company(self):
        df = pd.DataFrame([
            _disclosure("1", "48340", "キャリアバンク", "当社株式に対する公開買付け(TOB)に関するお知らせ",
                        "2026-05-01 15:00:00", "札"),
        ])
        result = regional_stocks.detect_regional_major_events(df)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["MatchedKeyword"], "TOB")

    def test_detects_japanese_only_tender_offer_title(self):
        # 実際の開示タイトルは英字"TOB"を含まず、"公開買付け"のみのことが多い。
        df = pd.DataFrame([
            _disclosure("1", "48340", "キャリアバンク", "当社株式に対する公開買付けに関するお知らせ",
                        "2026-05-01 15:00:00", "札"),
        ])
        result = regional_stocks.detect_regional_major_events(df)
        self.assertEqual(len(result), 1)

    def test_excludes_tokyo_listed_companies(self):
        df = pd.DataFrame([
            _disclosure("1", "72030", "トヨタ", "子会社化に関するお知らせ", "2026-05-01 15:00:00", "東"),
        ])
        result = regional_stocks.detect_regional_major_events(df)
        self.assertTrue(result.empty)

    def test_no_keyword_match_returns_empty(self):
        df = pd.DataFrame([
            _disclosure("1", "48340", "キャリアバンク", "定例の決算短信", "2026-05-01 15:00:00", "札"),
        ])
        result = regional_stocks.detect_regional_major_events(df)
        self.assertTrue(result.empty)


class TestLatestCompanyStatus(unittest.TestCase):
    def test_missing_markets_string_does_not_overwrite_valid_status(self):
        # 最新(日付順)の開示のmarkets_stringが欠損している場合、それを
        # そのまま採用してしまうと有効な市場情報がNaNで上書きされてしまう。
        # そのような回は無視し、直前の有効な回が採用されることを確認する。
        df = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "自己株式の取得結果に関するお知らせ",
                        "2026-08-01 08:00:00", "福"),
            _disclosure("2", "93880", "Ｑ－パパネッツ", "自己株式の取得結果に関するお知らせ",
                        "2026-08-10 08:00:00", float("nan")),
        ])
        result = regional_stocks._latest_company_status(df)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["MarketsString"], "福")

    def test_uses_only_the_latest_disclosure_per_company(self):
        df = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "自己株式の取得結果に関するお知らせ",
                        "2026-08-01 08:00:00", "福"),
            _disclosure("2", "93880", "Ｑ－パパネッツ", "臨時報告書の提出に関するお知らせ",
                        "2026-08-10 08:00:00", "福"),
        ])
        result = regional_stocks._latest_company_status(df)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["LastSeenDate"], pd.Timestamp("2026-08-10 08:00:00"))


class TestDelistingDatesByCode(unittest.TestCase):
    def test_marks_delisting_notice_date(self):
        df = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "福岡証券取引所への上場廃止に関するお知らせ",
                        "2026-08-01 08:00:00", "福"),
        ])
        result = regional_stocks._delisting_dates_by_code(df)
        self.assertEqual(result["93880"], pd.Timestamp("2026-08-01 08:00:00"))

    def test_ordinary_disclosure_has_no_delisting_date(self):
        df = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "自己株式の取得結果に関するお知らせ",
                        "2026-08-01 08:00:00", "福"),
        ])
        result = regional_stocks._delisting_dates_by_code(df)
        self.assertNotIn("93880", result)

    def test_delisting_date_captured_even_when_markets_string_missing(self):
        # 上場廃止の開示自体でmarkets_stringが欠損していても、
        # was_known_regionalで既知の銘柄と分かれば取り逃さないことを確認する
        # （_latest_company_status()はこのケースをmarkets_string不明として除外する）。
        df = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "福岡証券取引所への上場廃止に関するお知らせ",
                        "2026-08-01 08:00:00", float("nan")),
        ])
        without_context = regional_stocks._delisting_dates_by_code(df)
        self.assertNotIn("93880", without_context)

        with_context = regional_stocks._delisting_dates_by_code(df, pd.Series([True], index=df.index))
        self.assertEqual(with_context["93880"], pd.Timestamp("2026-08-01 08:00:00"))

    def test_empty_input_returns_empty_dict(self):
        self.assertEqual(regional_stocks._delisting_dates_by_code(pd.DataFrame()), {})


class TestFetchRegionalSharePrice(unittest.TestCase):
    def test_fukuoka_only_uses_yfinance_f_suffix(self):
        mock_yf = MagicMock()
        mock_yf.Ticker.return_value.fast_info = {"lastPrice": 1824.0}
        with patch.dict(sys.modules, {"yfinance": mock_yf}):
            price, note = regional_stocks.fetch_regional_share_price("93880", "福")
        self.assertEqual(price, 1824.0)
        self.assertEqual(note, "")
        mock_yf.Ticker.assert_called_once_with("9388.F")

    def test_nagoya_only_is_not_attempted(self):
        price, note = regional_stocks.fetch_regional_share_price("61110", "名")
        self.assertIsNone(price)
        self.assertIn("取得不可", note)

    def test_sapporo_only_is_not_attempted(self):
        price, note = regional_stocks.fetch_regional_share_price("48340", "札")
        self.assertIsNone(price)
        self.assertIn("取得不可", note)

    def test_yfinance_exception_is_handled(self):
        mock_yf = MagicMock()
        mock_yf.Ticker.side_effect = RuntimeError("boom")
        with patch.dict(sys.modules, {"yfinance": mock_yf}):
            price, note = regional_stocks.fetch_regional_share_price("93880", "福")
        self.assertIsNone(price)
        self.assertIn("取得不可", note)

    def test_yfinance_returns_no_price(self):
        mock_yf = MagicMock()
        mock_yf.Ticker.return_value.fast_info = {"lastPrice": None}
        with patch.dict(sys.modules, {"yfinance": mock_yf}):
            price, note = regional_stocks.fetch_regional_share_price("93880", "福")
        self.assertIsNone(price)
        self.assertIn("取得不可", note)


class TestFetchRegionalStatements(unittest.TestCase):
    def test_filters_to_regional_tanshin_with_xbrl_within_lookback(self):
        disclosures = pd.DataFrame([
            _disclosure("1", "33460", "ヒロタグループHD", "2026年6月期 第1四半期決算短信〔日本基準〕(連結)",
                        "2026-08-13 15:30:00", "名", url_xbrl="https://example.com/1.zip"),
            # 東証を含む(地方単独上場ではない)ため対象外
            _disclosure("2", "10000", "東証銘柄", "2026年6月期 決算短信〔日本基準〕(連結)",
                        "2026-08-13 15:30:00", "東", url_xbrl="https://example.com/2.zip"),
            # 決算短信ではないため対象外
            _disclosure("3", "33460", "ヒロタグループHD", "新工場設置に関するお知らせ",
                        "2026-08-13 15:30:00", "名", url_xbrl="https://example.com/3.zip"),
            # XBRL添付が無いため対象外
            _disclosure("4", "33460", "ヒロタグループHD", "2026年6月期 決算短信〔日本基準〕(連結)",
                        "2026-08-13 15:30:00", "名", url_xbrl=None),
            # REGIONAL_STATEMENTS_LOOKBACK_DAYSより古いため対象外
            _disclosure("5", "33460", "ヒロタグループHD", "2026年3月期 決算短信〔日本基準〕(連結)",
                        "2025-01-01 15:30:00", "名", url_xbrl="https://example.com/5.zip"),
        ])
        fake_rows = [
            {"Code": "33460", "DiscDate": dt.date(2026, 8, 13), "CurPerType": "1Q",
             "CurPerEn": pd.Timestamp("2026-06-30"), "CurFYEn": pd.Timestamp("2027-03-31"),
             "Sales": 378_000_000.0, "OP": 16_000_000.0, "OdP": 14_000_000.0, "NP": 216_000_000.0,
             "EqAR": 0.34, "FSales": None, "FOP": None, "FNP": None, "BPS": None, "IsPrimary": True},
        ]
        with patch(f"{_MOD}.tdnet_xbrl.fetch_tanshin_statement_rows", return_value=fake_rows) as mock_fetch:
            result = regional_stocks.fetch_regional_statements(disclosures, today=dt.date(2026, 8, 19))

        mock_fetch.assert_called_once_with("https://example.com/1.zip", "33460", dt.date(2026, 8, 13))
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["id"], "1_0")
        self.assertEqual(result.iloc[0]["Code"], "33460")
        self.assertListEqual(list(result.columns), regional_stocks._STATEMENTS_COLUMNS)

    def test_missing_url_xbrl_column_returns_empty_without_error(self):
        disclosures = pd.DataFrame([
            _disclosure("1", "33460", "ヒロタグループHD", "決算短信〔日本基準〕(連結)",
                        "2026-08-13 15:30:00", "名"),
        ]).drop(columns=["url_xbrl"])
        result = regional_stocks.fetch_regional_statements(disclosures, today=dt.date(2026, 8, 19))
        self.assertTrue(result.empty)
        self.assertListEqual(list(result.columns), regional_stocks._STATEMENTS_COLUMNS)

    def test_missing_markets_string_included_via_was_known_regional(self):
        # markets_stringが欠損している決算短信でも、was_known_regionalで直前
        # まで地方単独上場と分かっていれば対象に含める。含めないと、
        # update_regional_store()のウォーターマークが進んだ後は同じ開示が
        # 二度と対象にならず、その四半期の財務データが永久に欠落する
        # （_latest_company_status/_delisting_dates_by_codeと同種の欠損対応）。
        disclosures = pd.DataFrame([
            _disclosure("1", "33460", "ヒロタグループHD", "決算短信〔日本基準〕(連結)",
                        "2026-08-13 15:30:00", float("nan"), url_xbrl="https://example.com/1.zip"),
        ])
        fake_rows = [
            {"Code": "33460", "DiscDate": dt.date(2026, 8, 13), "CurPerType": "1Q",
             "CurPerEn": pd.Timestamp("2026-06-30"), "CurFYEn": pd.Timestamp("2027-03-31"),
             "Sales": 378_000_000.0, "OP": 16_000_000.0, "OdP": 14_000_000.0, "NP": 216_000_000.0,
             "EqAR": 0.34, "FSales": None, "FOP": None, "FNP": None, "BPS": None, "IsPrimary": True},
        ]
        without_context = regional_stocks.fetch_regional_statements(disclosures, today=dt.date(2026, 8, 19))
        self.assertTrue(without_context.empty)

        with patch(f"{_MOD}.tdnet_xbrl.fetch_tanshin_statement_rows", return_value=fake_rows):
            with_context = regional_stocks.fetch_regional_statements(
                disclosures, today=dt.date(2026, 8, 19), was_known_regional=pd.Series([True], index=disclosures.index)
            )
        self.assertEqual(len(with_context), 1)
        self.assertEqual(with_context.iloc[0]["Code"], "33460")

    def test_per_disclosure_fetch_failure_is_skipped_not_fatal(self):
        disclosures = pd.DataFrame([
            _disclosure("1", "33460", "ヒロタグループHD", "決算短信〔日本基準〕(連結)",
                        "2026-08-13 15:30:00", "名", url_xbrl="https://example.com/1.zip"),
            _disclosure("2", "19990", "サイタHD", "決算短信〔日本基準〕(連結)",
                        "2026-08-18 15:30:00", "福", url_xbrl="https://example.com/2.zip"),
        ])
        fake_row = [{
            "Code": "19990", "DiscDate": dt.date(2026, 8, 18), "CurPerType": "FY",
            "CurPerEn": pd.Timestamp("2026-06-30"), "CurFYEn": pd.Timestamp("2026-06-30"),
            "Sales": 7_035_000_000.0, "OP": None, "OdP": None, "NP": None,
            "EqAR": None, "FSales": None, "FOP": None, "FNP": None, "BPS": None, "IsPrimary": True,
        }]
        with patch(
            f"{_MOD}.tdnet_xbrl.fetch_tanshin_statement_rows",
            side_effect=[RuntimeError("boom"), fake_row],
        ):
            result = regional_stocks.fetch_regional_statements(disclosures, today=dt.date(2026, 8, 19))
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["Code"], "19990")


_REQUIRED_DISCLOSURE_COLUMNS_FOR_TEST = [
    "id", "company_code", "company_name", "title", "pubdate", "markets_string", "document_url", "url_xbrl",
]


def _statement_row(
    code, disc_date, per_type, per_end, fy_end, sales, prev_sales=None, eqar=None, is_primary=True,
    disclosure_id=None,
):
    return {
        "id": f"{code}_{per_end}_{disc_date}", "Code": code, "DiscDate": disc_date, "CurPerType": per_type,
        "CurPerEn": pd.Timestamp(per_end), "CurFYEn": pd.Timestamp(fy_end),
        "Sales": sales, "OP": None, "OdP": None, "NP": None, "EqAR": eqar,
        "FSales": None, "FOP": None, "FNP": None, "BPS": None, "IsPrimary": is_primary,
        "DisclosureId": disclosure_id if disclosure_id is not None else f"{code}_{disc_date}",
    }


class TestScreenRegional(unittest.TestCase):
    def test_selected_statement_rule_only(self):
        statements = pd.DataFrame([
            _statement_row("33460", dt.date(2026, 8, 13), "1Q", "2026-06-30", "2027-03-31", 378_000_000.0, eqar=0.34),
            _statement_row("33460", dt.date(2026, 8, 13), "1Q", "2025-06-30", "2026-03-31", 100_000_000.0),
        ])
        company_status = pd.DataFrame([
            {"Code": "33460", "CompanyName": "ヒロタグループHD", "MarketsString": "名",
             "LastSeenDate": pd.Timestamp("2026-08-13")},
        ])
        disclosures = pd.DataFrame(columns=list(_REQUIRED_DISCLOSURE_COLUMNS_FOR_TEST))

        hits = regional_stocks.screen_regional(disclosures, statements, company_status, ["sales_growth_major"])
        # 278%増のため大幅(+20%以上)・爆発的(+50%以上)の両方の条件を数値として
        # 満たす。detect_sales_growthは両方のruleタグを返す（rules.pyのdocstring
        # 参照。"sales_growth_major"だけを選んでも、実際には+20%以上でもある
        # 爆発的成長銘柄が漏れないようにするため）。
        self.assertEqual(len(hits), 2)
        self.assertEqual(set(hits["Rule"]), {"sales_growth_major", "sales_growth_explosive"})
        self.assertTrue((hits["Code"] == "33460").all())
        self.assertTrue((hits["CompanyName"] == "ヒロタグループHD").all())

    def test_unselected_rule_is_not_evaluated(self):
        statements = pd.DataFrame([
            _statement_row("33460", dt.date(2026, 8, 13), "1Q", "2026-06-30", "2027-03-31", 378_000_000.0, eqar=0.99),
        ])
        company_status = pd.DataFrame(columns=["Code", "CompanyName", "MarketsString", "LastSeenDate"])
        disclosures = pd.DataFrame(columns=list(_REQUIRED_DISCLOSURE_COLUMNS_FOR_TEST))

        hits = regional_stocks.screen_regional(disclosures, statements, company_status, ["sales_growth_major"])
        self.assertTrue(hits.empty)  # equity_ratio_highを選んでいないため、EqAR=0.99でもヒットしない

    def test_title_based_rule_uses_disclosures_df(self):
        statements = pd.DataFrame(columns=regional_stocks._STATEMENTS_COLUMNS)
        company_status = pd.DataFrame(columns=["Code", "CompanyName", "MarketsString", "LastSeenDate"])
        disclosures = pd.DataFrame([
            _disclosure("1", "40180", "Ｑ－ジオロケ", "新工場設置に関するお知らせ", "2026-08-13 15:30:00", "福"),
        ])

        hits = regional_stocks.screen_regional(disclosures, statements, company_status, ["new_facility_or_store"])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits.iloc[0]["Rule"], "new_facility_or_store")

    def test_empty_selection_returns_empty_with_expected_columns(self):
        statements = pd.DataFrame(columns=regional_stocks._STATEMENTS_COLUMNS)
        company_status = pd.DataFrame(columns=["Code", "CompanyName", "MarketsString", "LastSeenDate"])
        disclosures = pd.DataFrame(columns=list(_REQUIRED_DISCLOSURE_COLUMNS_FOR_TEST))

        hits = regional_stocks.screen_regional(disclosures, statements, company_status, [])
        self.assertTrue(hits.empty)
        self.assertListEqual(
            list(hits.columns), ["Code", "CompanyName", "Sector", "Rule", "RuleLabel", "Date", "Detail"]
        )

    def test_equity_ratio_uses_latest_statement_only(self):
        # 本決算のprior_row(前年同期)がEqARを持つケース。当期(最新)のEqARが
        # 閾値未満でも、古いprior_rowが閾値以上だと誤ってヒットしていた回帰
        # テスト（2026-08-19のCodexレビューで指摘、実データで確認）。
        statements = pd.DataFrame([
            _statement_row("19990", dt.date(2026, 8, 18), "FY", "2026-06-30", "2026-06-30",
                            7_035_000_000.0, eqar=0.50, is_primary=True),
            _statement_row("19990", dt.date(2026, 8, 18), "FY", "2025-06-30", "2025-06-30",
                            7_841_000_000.0, eqar=0.65, is_primary=False),
        ])
        company_status = pd.DataFrame([
            {"Code": "19990", "CompanyName": "サイタHD", "MarketsString": "福",
             "LastSeenDate": pd.Timestamp("2026-08-18")},
        ])
        disclosures = pd.DataFrame(columns=list(_REQUIRED_DISCLOSURE_COLUMNS_FOR_TEST))

        hits = regional_stocks.screen_regional(disclosures, statements, company_status, ["equity_ratio_high"])
        self.assertTrue(hits.empty)  # 最新(当期)は50%のため60%閾値に届かない

    def test_excludes_companies_no_longer_regional(self):
        # company_status上で既に東証移籍済み(MarketsStringに"東"を含む)の銘柄は、
        # statements_dfに古い財務データが残っていても地方株の財務条件結果には
        # 含めない（2026-08-19のCodexレビューで指摘、実データで確認）。
        statements = pd.DataFrame([
            _statement_row("33460", dt.date(2026, 8, 13), "1Q", "2026-06-30", "2027-03-31",
                            378_000_000.0, eqar=0.34, is_primary=True),
            _statement_row("33460", dt.date(2026, 8, 13), "1Q", "2025-06-30", "2026-03-31",
                            100_000_000.0, is_primary=False),
        ])
        company_status = pd.DataFrame([
            {"Code": "33460", "CompanyName": "ヒロタグループHD", "MarketsString": "東名",
             "LastSeenDate": pd.Timestamp("2026-08-13"), "IsDelisted": False},
        ])
        disclosures = pd.DataFrame(columns=list(_REQUIRED_DISCLOSURE_COLUMNS_FOR_TEST))

        hits = regional_stocks.screen_regional(disclosures, statements, company_status, ["sales_growth_major"])
        self.assertTrue(hits.empty)


class TestLatestPrimaryStatements(unittest.TestCase):
    def test_keeps_only_latest_primary_row_per_company(self):
        statements = pd.DataFrame([
            _statement_row("19990", dt.date(2026, 8, 18), "FY", "2026-06-30", "2026-06-30",
                            7_035_000_000.0, eqar=0.50, is_primary=True),
            _statement_row("19990", dt.date(2025, 8, 20), "FY", "2025-06-30", "2025-06-30",
                            7_841_000_000.0, eqar=0.65, is_primary=True),
            _statement_row("19990", dt.date(2026, 8, 18), "FY", "2025-06-30", "2025-06-30",
                            7_841_000_000.0, eqar=0.65, is_primary=False),
        ])
        result = regional_stocks._latest_primary_statements(statements)
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result.iloc[0]["EqAR"], 0.50)

    def test_empty_input_returns_empty(self):
        result = regional_stocks._latest_primary_statements(pd.DataFrame(columns=regional_stocks._STATEMENTS_COLUMNS))
        self.assertTrue(result.empty)


class TestCurrentlyRegionalCodes(unittest.TestCase):
    def test_excludes_tokyo_and_delisted(self):
        company_status = pd.DataFrame([
            {"Code": "1", "MarketsString": "福", "IsDelisted": False},
            {"Code": "2", "MarketsString": "東福", "IsDelisted": False},
            {"Code": "3", "MarketsString": "名", "IsDelisted": True},
        ])
        result = regional_stocks._currently_regional_codes(company_status)
        self.assertEqual(result, {"1"})

    def test_missing_is_delisted_column_treated_as_not_delisted(self):
        company_status = pd.DataFrame([{"Code": "1", "MarketsString": "福"}])
        result = regional_stocks._currently_regional_codes(company_status)
        self.assertEqual(result, {"1"})

    def test_empty_input_returns_none(self):
        self.assertIsNone(regional_stocks._currently_regional_codes(pd.DataFrame()))


class TestDedupeSupersededStatements(unittest.TestCase):
    def test_keeps_latest_primary_row_for_same_period(self):
        # 訂正決算短信で同じ期(CurPerType, CurPerEn)の実際の開示行(IsPrimary=True)
        # が複数ある場合、最新のDiscDateだけを残す。
        statements = pd.DataFrame([
            _statement_row("19990", dt.date(2026, 8, 18), "FY", "2026-06-30", "2026-06-30",
                            7_035_000_000.0, is_primary=True),
            _statement_row("19990", dt.date(2026, 8, 25), "FY", "2026-06-30", "2026-06-30",
                            7_050_000_000.0, is_primary=True),  # 訂正後の数値
        ])
        result = regional_stocks._dedupe_superseded_statements(statements)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["Sales"], 7_050_000_000.0)

    def test_does_not_merge_synthetic_rows_from_different_disclosures(self):
        # 今年の開示のprior_row(前年同期の合成行)と、去年の開示のcur_rowが
        # 同じCurPerEnを指すのは正当なケースであり、統合してはいけない
        # （開示日ベースの時系列比較が壊れるため）。
        statements = pd.DataFrame([
            _statement_row("19990", dt.date(2025, 8, 20), "FY", "2025-06-30", "2025-06-30",
                            7_841_000_000.0, is_primary=True),
            _statement_row("19990", dt.date(2026, 8, 18), "FY", "2025-06-30", "2025-06-30",
                            7_841_000_000.0, is_primary=False),
        ])
        result = regional_stocks._dedupe_superseded_statements(statements)
        self.assertEqual(len(result), 2)

    def test_missing_isprimary_column_returns_unchanged(self):
        statements = pd.DataFrame([{"Code": "1", "CurPerType": "FY", "CurPerEn": pd.Timestamp("2026-06-30")}])
        result = regional_stocks._dedupe_superseded_statements(statements)
        self.assertEqual(len(result), 1)

    def test_removes_synthetic_guidance_row_belonging_to_superseded_disclosure(self):
        # 訂正決算短信で置き換えられた(古い方の)実際の開示行だけでなく、その
        # 開示に埋め込まれていた翌期予想の合成行(guidance_row、同じDiscDate)も
        # 一緒に取り除く。残したままだと、訂正で修正されたはずの誤った予想値が
        # detect_downward_revision()の比較対象に残ってしまう
        # （2026-08-19の3巡目のCodexレビューで指摘）。
        original_disc_date = dt.date(2026, 8, 18)
        corrected_disc_date = dt.date(2026, 8, 25)
        statements = pd.DataFrame([
            {
                "id": "orig_cur", "Code": "19990", "DiscDate": original_disc_date, "CurPerType": "FY",
                "CurPerEn": pd.Timestamp("2026-06-30"), "CurFYEn": pd.Timestamp("2026-06-30"),
                "Sales": 7_035_000_000.0, "OP": None, "OdP": None, "NP": None, "EqAR": None,
                "FSales": None, "FOP": None, "FNP": None, "BPS": None, "IsPrimary": True,
                "DisclosureId": "orig_disclosure",
            },
            {
                # 訂正前の(誤った)翌期予想の合成行。同じDisclosureIdを共有する。
                "id": "orig_guidance", "Code": "19990", "DiscDate": original_disc_date, "CurPerType": "FY",
                "CurPerEn": pd.NaT, "CurFYEn": pd.Timestamp("2027-06-30"),
                "Sales": None, "OP": None, "OdP": None, "NP": None, "EqAR": None,
                "FSales": None, "FOP": None, "FNP": 999_000_000.0, "BPS": None, "IsPrimary": False,
                "DisclosureId": "orig_disclosure",
            },
            {
                "id": "corrected_cur", "Code": "19990", "DiscDate": corrected_disc_date, "CurPerType": "FY",
                "CurPerEn": pd.Timestamp("2026-06-30"), "CurFYEn": pd.Timestamp("2026-06-30"),
                "Sales": 7_050_000_000.0, "OP": None, "OdP": None, "NP": None, "EqAR": None,
                "FSales": None, "FOP": None, "FNP": None, "BPS": None, "IsPrimary": True,
                "DisclosureId": "corrected_disclosure",
            },
        ])
        result = regional_stocks._dedupe_superseded_statements(statements)
        self.assertEqual(set(result["id"]), {"corrected_cur"})

    def test_same_day_correction_does_not_remove_the_corrected_release_own_rows(self):
        # 元の開示と訂正決算短信が同じ暦日に公開された場合、DiscDate(時刻を
        # 切り捨てた日付のみ)だけでは区別できない。DisclosureId(TDnet開示の
        # ID)で区別することで、訂正側の正しい合成行まで巻き添えで削除しない
        # ことを確認する（2026-08-19の4巡目のCodexレビューで指摘・修正）。
        # TDnetの開示IDは概ね発行順に大きくなる数字文字列のため、テストでも
        # その形式(後の開示ほど大きい値)に合わせる。
        same_day = dt.date(2026, 8, 18)
        statements = pd.DataFrame([
            {
                "id": "orig_cur", "Code": "19990", "DiscDate": same_day, "CurPerType": "FY",
                "CurPerEn": pd.Timestamp("2026-06-30"), "CurFYEn": pd.Timestamp("2026-06-30"),
                "Sales": 7_035_000_000.0, "OP": None, "OdP": None, "NP": None, "EqAR": None,
                "FSales": None, "FOP": None, "FNP": None, "BPS": None, "IsPrimary": True,
                "DisclosureId": "202608180001",
            },
            {
                "id": "corrected_cur", "Code": "19990", "DiscDate": same_day, "CurPerType": "FY",
                "CurPerEn": pd.Timestamp("2026-06-30"), "CurFYEn": pd.Timestamp("2026-06-30"),
                "Sales": 7_050_000_000.0, "OP": None, "OdP": None, "NP": None, "EqAR": None,
                "FSales": None, "FOP": None, "FNP": None, "BPS": None, "IsPrimary": True,
                "DisclosureId": "202608180002",
            },
            {
                # 訂正側(202608180002)自身の翌期予想合成行。DiscDateはcur行と
                # 同じ同日だが、DisclosureIdは正しく紐付いている。
                "id": "corrected_guidance", "Code": "19990", "DiscDate": same_day, "CurPerType": "FY",
                "CurPerEn": pd.NaT, "CurFYEn": pd.Timestamp("2027-06-30"),
                "Sales": None, "OP": None, "OdP": None, "NP": None, "EqAR": None,
                "FSales": None, "FOP": None, "FNP": 500_000_000.0, "BPS": None, "IsPrimary": False,
                "DisclosureId": "202608180002",
            },
        ])
        result = regional_stocks._dedupe_superseded_statements(statements)
        self.assertEqual(set(result["id"]), {"corrected_cur", "corrected_guidance"})


class TestUpdateRegionalStore(unittest.TestCase):
    """cache.pyのCACHE_DIRを一時ディレクトリに差し替えて、実際にparquet往復させる。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._cache_dir_patcher = patch("src.cache.CACHE_DIR", self._tmpdir.name)
        self._cache_dir_patcher.start()
        self._supabase_patcher = patch("src.cache._supabase_config", return_value=None)
        self._supabase_patcher.start()

    def tearDown(self):
        self._supabase_patcher.stop()
        self._cache_dir_patcher.stop()
        self._tmpdir.cleanup()

    def test_first_run_uses_bootstrap_lookback_and_persists(self):
        disclosures = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ",
                        "福岡証券取引所本則市場への重複上場に関するお知らせ",
                        "2026-08-01 08:00:00", "福"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=disclosures) as mock_get, \
                patch(f"{_MOD}.fetch_regional_share_price", return_value=(None, "取得不可（テスト）")):
            today = dt.date(2026, 8, 13)
            result = regional_stocks.update_regional_store(today=today)

        # 前日までの安定した期間と、当日分(force_refresh)の2回に分けて呼ぶ
        self.assertEqual(len(mock_get.call_args_list), 2)
        stable_start, stable_end = mock_get.call_args_list[0].args
        self.assertEqual(stable_end, today - dt.timedelta(days=1))
        self.assertLess(stable_start, today - dt.timedelta(days=365))  # 3年遡る初回ブートストラップ
        today_start, today_end = mock_get.call_args_list[1].args
        self.assertEqual((today_start, today_end), (today, today))
        self.assertTrue(mock_get.call_args_list[1].kwargs.get("force_refresh"))
        self.assertEqual(len(result["listing_events"]), 1)
        self.assertEqual(result["company_status"].iloc[0]["Code"], "93880")

    def test_watermark_stays_one_day_behind_today(self):
        # 更新ボタンが押された当日分のTDnet開示はまだ全件公開されていない
        # 可能性があるため、ウォーターマークはtodayではなくtoday-1にする
        # （次回はtodayを再スキャンする）。
        disclosures = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "福岡証券取引所本則市場への重複上場に関するお知らせ",
                        "2026-08-01 08:00:00", "福"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=disclosures), \
                patch(f"{_MOD}.fetch_regional_share_price", return_value=(None, "取得不可（テスト）")):
            regional_stocks.update_regional_store(today=dt.date(2026, 8, 13))

        second_batch = pd.DataFrame([
            _disclosure("2", "48340", "キャリアバンク", "当社株式に対するTOBに関するお知らせ",
                        "2026-08-14 08:00:00", "札"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=second_batch) as mock_get, \
                patch(f"{_MOD}.fetch_regional_share_price", return_value=(None, "取得不可（テスト）")):
            result = regional_stocks.update_regional_store(today=dt.date(2026, 8, 14))

        # 前回のwatermark=8/12(=today-1)の翌日=8/13から再取得する（8/13を再スキャン）
        stable_start, stable_end = mock_get.call_args_list[0].args
        self.assertEqual((stable_start, stable_end), (dt.date(2026, 8, 13), dt.date(2026, 8, 13)))
        today_start, today_end = mock_get.call_args_list[1].args
        self.assertEqual((today_start, today_end), (dt.date(2026, 8, 14), dt.date(2026, 8, 14)))
        # 1回目の検出結果が消えずに残っていること（増分マージの確認）
        self.assertEqual(len(result["listing_events"]), 1)
        self.assertEqual(len(result["major_events"]), 1)
        self.assertEqual(set(result["company_status"]["Code"]), {"93880", "48340"})

    def test_same_batch_regional_to_tokyo_transition_is_captured(self):
        # 初回ブートストラップのように複数年分を一度に取得すると、同じバッチ
        # の中で「地方単独上場」→「東証上場完了」まで進んでいる銘柄がある
        # 場合がある。was_known_regionalはストア（更新前なので空）だけでは
        # なく、このバッチ内での発見も反映する必要があることを確認する。
        combined_batch = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ",
                        "福岡証券取引所本則市場への重複上場に関するお知らせ",
                        "2023-01-10 08:00:00", "福"),
            _disclosure("2", "93880", "Ｑ－パパネッツ", "東京証券取引所への上場のお知らせ",
                        "2026-08-01 08:00:00", "東福"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", side_effect=[combined_batch, pd.DataFrame()]), \
                patch(f"{_MOD}.fetch_regional_share_price", return_value=(None, "取得不可（テスト）")):
            result = regional_stocks.update_regional_store(today=dt.date(2026, 8, 13))

        stages = set(result["listing_events"]["Stage"])
        self.assertEqual(stages, {"上場"})
        self.assertTrue(result["listing_events"]["IsTokyoRelated"].any())
        self.assertEqual(result["company_status"].iloc[0]["MarketsString"], "東福")

    def test_load_regional_store_without_update_reads_persisted_data(self):
        disclosures = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "福岡証券取引所本則市場への重複上場に関するお知らせ",
                        "2026-08-01 08:00:00", "福"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=disclosures), \
                patch(f"{_MOD}.fetch_regional_share_price", return_value=(None, "取得不可（テスト）")):
            regional_stocks.update_regional_store(today=dt.date(2026, 8, 13))

        store = regional_stocks.load_regional_store()
        self.assertEqual(len(store["listing_events"]), 1)
        self.assertEqual(store["company_status"].iloc[0]["Code"], "93880")

    def test_empty_store_round_trip_keeps_expected_columns(self):
        # 0件の結果を保存・再読込した場合でも、列名が失われず後続処理が
        # KeyErrorにならないことを確認する（cache.pyが空フレームを
        # マーカー形式で保存する仕様のための回帰テスト）。
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=pd.DataFrame()):
            regional_stocks.update_regional_store(today=dt.date(2026, 8, 13))

        store = regional_stocks.load_regional_store()
        self.assertListEqual(list(store["listing_events"].columns), regional_stocks._LISTING_EVENTS_COLUMNS)
        self.assertListEqual(list(store["major_events"].columns), regional_stocks._MAJOR_EVENTS_COLUMNS)
        self.assertListEqual(list(store["company_status"].columns), regional_stocks._COMPANY_STATUS_COLUMNS)

    def test_company_that_moved_to_tokyo_is_not_re_enriched(self):
        # 1回目の更新で93880を地方単独上場(福)として認識させ、2回目の更新で
        # 東証重複上場(東福)に移った開示が来た場合、company_statusの
        # MarketsStringが更新される一方、株価取得ループの対象からは外れる
        # （is_regional_only("東福")==False）ことを確認する。
        first_batch = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "福岡証券取引所本則市場への重複上場に関するお知らせ",
                        "2026-08-01 08:00:00", "福"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=first_batch), \
                patch(f"{_MOD}.fetch_regional_share_price", return_value=(1824.0, "")):
            regional_stocks.update_regional_store(today=dt.date(2026, 8, 13))

        second_batch = pd.DataFrame([
            _disclosure("2", "93880", "Ｑ－パパネッツ", "東京証券取引所への上場のお知らせ",
                        "2026-08-15 08:00:00", "東福"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=second_batch), \
                patch(f"{_MOD}.fetch_regional_share_price") as mock_fetch:
            result = regional_stocks.update_regional_store(today=dt.date(2026, 8, 15))

        mock_fetch.assert_not_called()
        status_row = result["company_status"].set_index("Code").loc["93880"]
        self.assertEqual(status_row["MarketsString"], "東福")

    def test_delisted_company_is_excluded_from_share_price_refresh(self):
        # 上場廃止の開示が来た銘柄は、markets_stringがまだその取引所名を
        # 含んでいても「現在も地方単独上場中」として株価取得を試みない
        # ことを確認する（IsDelisted=Trueで除外）。
        disclosures = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "福岡証券取引所への上場廃止に関するお知らせ",
                        "2026-08-01 08:00:00", "福"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=disclosures), \
                patch(f"{_MOD}.fetch_regional_share_price") as mock_fetch:
            result = regional_stocks.update_regional_store(today=dt.date(2026, 8, 13))

        mock_fetch.assert_not_called()
        self.assertTrue(bool(result["company_status"].iloc[0]["IsDelisted"]))

    def test_is_delisted_stays_true_even_if_a_later_disclosure_looks_ordinary(self):
        # 上場廃止と判定された後、同じバッチ内(または別の更新)で通常開示の
        # 方が日付として最新になった場合でも、IsDelistedはFalseに戻らない
        # ことを確認する（上場廃止は終端的な状態として扱う）。
        disclosures = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "福岡証券取引所への上場廃止に関するお知らせ",
                        "2026-08-01 08:00:00", "福"),
            _disclosure("2", "93880", "Ｑ－パパネッツ", "自己株式の取得結果に関するお知らせ",
                        "2026-08-05 08:00:00", "福"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=disclosures), \
                patch(f"{_MOD}.fetch_regional_share_price") as mock_fetch:
            result = regional_stocks.update_regional_store(today=dt.date(2026, 8, 13))

        mock_fetch.assert_not_called()
        self.assertTrue(bool(result["company_status"].iloc[0]["IsDelisted"]))

    def test_relisting_after_delisting_clears_is_delisted(self):
        # 上場廃止の後に、別の更新で本当に「上場のお知らせ」（重複上場等）が
        # 来た場合はIsDelistedがFalseに戻り、株価取得の対象に戻ることを確認する
        # （上場廃止を無条件にTrue固定にすると再上場を正しく扱えないため）。
        first_batch = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "福岡証券取引所への上場廃止に関するお知らせ",
                        "2026-08-01 08:00:00", "福"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=first_batch), \
                patch(f"{_MOD}.fetch_regional_share_price") as mock_fetch:
            regional_stocks.update_regional_store(today=dt.date(2026, 8, 13))
        mock_fetch.assert_not_called()

        second_batch = pd.DataFrame([
            _disclosure("2", "93880", "Ｑ－パパネッツ", "福岡証券取引所への上場のお知らせ",
                        "2026-09-01 08:00:00", "福"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=second_batch), \
                patch(f"{_MOD}.fetch_regional_share_price", return_value=(1234.0, "")):
            result = regional_stocks.update_regional_store(today=dt.date(2026, 9, 1))

        self.assertFalse(bool(result["company_status"].iloc[0]["IsDelisted"]))
        self.assertEqual(result["company_status"].iloc[0]["CurrentPrice"], 1234.0)

    def test_delisting_with_missing_markets_string_still_marks_known_company(self):
        # 既知の(=以前から地方単独上場として認識済みの)銘柄について、上場廃止
        # 開示自体にmarkets_stringが欠損していても、IsDelistedが正しくTrueに
        # なり株価取得の対象から外れることを確認する。
        first_batch = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "福岡証券取引所本則市場への重複上場に関するお知らせ",
                        "2026-08-01 08:00:00", "福"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=first_batch), \
                patch(f"{_MOD}.fetch_regional_share_price", return_value=(1824.0, "")):
            regional_stocks.update_regional_store(today=dt.date(2026, 8, 13))

        second_batch = pd.DataFrame([
            _disclosure("2", "93880", "Ｑ－パパネッツ", "福岡証券取引所への上場廃止に関するお知らせ",
                        "2026-09-01 08:00:00", float("nan")),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=second_batch), \
                patch(f"{_MOD}.fetch_regional_share_price") as mock_fetch:
            result = regional_stocks.update_regional_store(today=dt.date(2026, 9, 1))

        mock_fetch.assert_not_called()
        self.assertTrue(bool(result["company_status"].iloc[0]["IsDelisted"]))

    def test_tokyo_disclosure_before_any_regional_evidence_is_not_misclassified(self):
        # 同じバッチ内で、東証関連の開示の方が地方単独上場の開示より前の
        # 日付にある場合、その東証開示の時点ではまだ地方単独上場だったと
        # 分かっていないため「地方→東証移籍」としては検出されないことを
        # 確認する（時系列を無視した誤分類の防止）。
        disclosures = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "東京証券取引所への上場のお知らせ",
                        "2026-01-10 08:00:00", "東福"),
            _disclosure("2", "93880", "Ｑ－パパネッツ", "福岡証券取引所本則市場への重複上場に関するお知らせ",
                        "2026-08-01 08:00:00", "福"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=disclosures), \
                patch(f"{_MOD}.fetch_regional_share_price", return_value=(None, "取得不可（テスト）")):
            result = regional_stocks.update_regional_store(today=dt.date(2026, 8, 13))

        # 1/10の東証開示はwas_known_regional=Falseなので検出されず、
        # 8/1の重複上場開示だけがヒットする。
        self.assertEqual(len(result["listing_events"]), 1)
        self.assertEqual(result["listing_events"].iloc[0]["Date"], pd.Timestamp("2026-08-01 08:00:00"))

    def test_watermark_not_advanced_when_a_table_save_fails(self):
        # Supabase設定済みの環境で一部テーブルの保存だけ失敗すると、次回
        # 再デプロイ後にそのテーブルだけ永久に欠落する恐れがある
        # （ウォーターマークだけ進んでしまい、二度と再取得されないため）。
        # cache.save()がFalseを返した場合はウォーターマークを進めない。
        disclosures = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "福岡証券取引所本則市場への重複上場に関するお知らせ",
                        "2026-08-01 08:00:00", "福"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=disclosures), \
                patch(f"{_MOD}.fetch_regional_share_price", return_value=(None, "取得不可（テスト）")), \
                patch(f"{_MOD}.cache.save", return_value=False), \
                patch(f"{_MOD}._save_watermark") as mock_save_watermark:
            regional_stocks.update_regional_store(today=dt.date(2026, 8, 13))

        mock_save_watermark.assert_not_called()

    def test_statements_watermark_not_advanced_when_a_table_save_fails(self):
        disclosures = pd.DataFrame([
            _disclosure("1", "93880", "Ｑ－パパネッツ", "福岡証券取引所本則市場への重複上場に関するお知らせ",
                        "2026-08-01 08:00:00", "福"),
        ])
        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=disclosures), \
                patch(f"{_MOD}.fetch_regional_share_price", return_value=(None, "取得不可（テスト）")), \
                patch(f"{_MOD}.cache.save", return_value=False), \
                patch(f"{_MOD}._save_statements_watermark") as mock_save_statements_watermark:
            regional_stocks.update_regional_store(today=dt.date(2026, 8, 13))

        mock_save_statements_watermark.assert_not_called()

    def test_statements_backfill_when_listing_watermark_already_advanced(self):
        # 上場イベント用のwatermarkは既に進んでいるが、statements機能を後から
        # 追加した直後でstatements_watermarkがまだ無い状況（既存デプロイへの
        # 機能追加）。REGIONAL_STATEMENTS_LOOKBACK_DAYS分だけ別途遡って取得し、
        # まだ取得可能な直近の決算短信を取りこぼさないことを確認する
        # （2026-08-19の3巡目のCodexレビューで指摘）。
        regional_stocks._save_watermark(dt.date(2026, 8, 13))  # 上場イベント側は既に8/13まで進んでいる

        backfill_disclosures = pd.DataFrame([
            _disclosure("1", "33460", "ヒロタグループHD", "決算短信〔日本基準〕(連結)",
                        "2026-07-01 15:30:00", "名", url_xbrl="https://example.com/1.zip"),
        ])
        fake_rows = [
            {"Code": "33460", "DiscDate": dt.date(2026, 7, 1), "CurPerType": "1Q",
             "CurPerEn": pd.Timestamp("2026-06-30"), "CurFYEn": pd.Timestamp("2027-03-31"),
             "Sales": 378_000_000.0, "OP": None, "OdP": None, "NP": None,
             "EqAR": None, "FSales": None, "FOP": None, "FNP": None, "BPS": None, "IsPrimary": True},
        ]

        call_ranges = []

        def fake_get_disclosures_range(start, end, force_refresh=False):
            call_ranges.append((start, end))
            if len(call_ranges) == 3:  # stable, today分に続く3回目がstatements用バックフィル
                return backfill_disclosures
            return pd.DataFrame()

        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", side_effect=fake_get_disclosures_range), \
                patch(f"{_MOD}.fetch_regional_share_price", return_value=(None, "取得不可（テスト）")), \
                patch(f"{_MOD}.tdnet_xbrl.fetch_tanshin_statement_rows", return_value=fake_rows):
            result = regional_stocks.update_regional_store(today=dt.date(2026, 8, 19))

        self.assertEqual(len(call_ranges), 3)
        # 3回目の呼び出しがstatements専用の遡り取得(today-60〜listing側watermarkの前日)
        self.assertEqual(call_ranges[2], (dt.date(2026, 6, 20), dt.date(2026, 8, 13)))
        self.assertEqual(len(result["statements"]), 1)
        self.assertEqual(result["statements"].iloc[0]["Code"], "33460")

    def test_no_statements_backfill_once_watermarks_are_in_sync(self):
        # 両方のwatermarkが揃って進んでいる通常運用時は、statements用の
        # 追加バックフィル取得(3回目の呼び出し)が発生しないことを確認する
        # （毎回同じ60日分を再取得する無駄を避けるため）。
        regional_stocks._save_watermark(dt.date(2026, 8, 13))
        regional_stocks._save_statements_watermark(dt.date(2026, 8, 13))

        with patch(f"{_MOD}.tdnet_client.get_disclosures_range", return_value=pd.DataFrame()) as mock_get, \
                patch(f"{_MOD}.fetch_regional_share_price", return_value=(None, "取得不可（テスト）")):
            regional_stocks.update_regional_store(today=dt.date(2026, 8, 15))

        self.assertEqual(len(mock_get.call_args_list), 2)


if __name__ == "__main__":
    unittest.main()
