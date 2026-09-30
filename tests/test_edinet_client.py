"""src/edinet_client.py の単体テスト。EDINETへの実通信は行わない。

実行方法:
    python -m unittest discover tests
"""
from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import edinet_client

_MOD = "src.edinet_client"


class TestElementToText(unittest.TestCase):
    def test_normal_nested_elements_are_extracted_as_is(self):
        # 通常のケース（inline XBRL等、実際にネストした要素として入っている
        # 場合）は従来通りタグを気にせずテキストだけを結合する。
        elem = etree.fromstring("<block>当社は<b>菓子</b>小売事業を行う。</block>")
        self.assertEqual(edinet_client._element_to_text(elem), "当社は菓子小売事業を行う。")

    def test_normal_nested_block_elements_are_separated(self):
        # escapedItemTypeの再解析(reparsed)側だけでなく、通常のネスト要素・
        # inline XBRLの主経路(raw計算)でも、隣接するブロック要素間に元の
        # HTML側の区切りが無い場合に単語が連結されないようにする
        # （例: "<p>Foo</p><p>Bar</p>"→"FooBar"。2026-09-30のCodexレビューで
        # 指摘・修正。当初はescapedItemType再解析側にしか適用していなかった）。
        elem = etree.fromstring("<block><p>Foo</p><p>Bar</p></block>")
        result = edinet_client._element_to_text(elem)
        self.assertNotIn("FooBar", result)
        self.assertIn("Foo", result)
        self.assertIn("Bar", result)

    def test_namespaced_xhtml_block_elements_are_separated(self):
        # inline XBRLの<html>本文は標準のXHTML名前空間を使うことが多く、
        # その場合lxmlはタグ名を"{http://www.w3.org/1999/xhtml}p"のような
        # 修飾名として保持する。単純な文字列一致("p"等)では該当せず区切りが
        # 入らなかった（2026-09-30のCodexレビューで指摘・修正）。
        xml = (
            '<block xmlns:x="http://www.w3.org/1999/xhtml">'
            "<x:p>Foo</x:p><x:p>Bar</x:p></block>"
        )
        elem = etree.fromstring(xml)
        result = edinet_client._element_to_text(elem)
        self.assertNotIn("FooBar", result)
        self.assertIn("Foo", result)
        self.assertIn("Bar", result)

    def test_adjacent_table_cells_are_separated(self):
        # <tr>の直後だけでなく、同じ行内で隣接する<td>同士も区切る
        # （例: "<tr><td>Foo</td><td>Bar</td></tr>"→"FooBar"のまま
        # 連結されないようにする。2026-09-30のCodexレビューで指摘・修正）。
        elem = etree.fromstring("<block><tr><td>Foo</td><td>Bar</td></tr></block>")
        result = edinet_client._element_to_text(elem)
        self.assertNotIn("FooBar", result)
        self.assertIn("Foo", result)
        self.assertIn("Bar", result)

    def test_block_boundary_uses_the_separator_marker_not_a_plain_newline(self):
        # 単なる改行"\n"を区切りに使うと、呼び出し側
        # (scripts/watch_and_notify.py _collapse_whitespace)が「前後が
        # 日本語の文字かどうか」でレイアウト空白と構造的な区切りを見分ける
        # 際に、日本語の文字同士に挟まれた構造的な区切り（隣接する日本語の
        # 表セル等）までレイアウト空白と誤認して除去してしまう
        # （"国内海外"のように結合される）。除去されてはならない区切りだと
        # 判別できるよう、専用のマーカー文字(BLOCK_SEPARATOR_MARKER)を
        # 使う（2026-09-30のCodexレビューで指摘・修正）。
        elem = etree.fromstring("<block><td>国内</td><td>海外</td></block>")
        result = edinet_client._element_to_text(elem)
        self.assertIn(edinet_client.BLOCK_SEPARATOR_MARKER, result)
        self.assertNotIn("\n", result)

    def test_escaped_html_stored_as_literal_text_is_reparsed(self):
        # XBRLのescapedItemType仕様により、要素のテキスト自体が一段
        # エスケープされたXHTML文字列として格納されている場合、
        # 通常のXML解析(itertext())ではタグがそのまま文字列として
        # 残ってしまう（2026-09-29のCodexレビューで指摘・修正）。
        xml = "<block>&lt;p&gt;Foo&lt;/p&gt;&lt;p&gt;Bar&lt;/p&gt;</block>"
        elem = etree.fromstring(xml)
        result = edinet_client._element_to_text(elem)
        self.assertNotIn("<p>", result)
        self.assertIn("Foo", result)
        self.assertIn("Bar", result)

    def test_empty_element_returns_none(self):
        elem = etree.fromstring("<block>   </block>")
        self.assertIsNone(edinet_client._element_to_text(elem))

    def test_escaped_html_adjacent_block_elements_are_separated(self):
        # <p>Foo</p><p>Bar</p>のように隣接するブロック要素の間に元の
        # HTML側の区切り（空白・改行）が無い場合、単純に結合すると
        # "FooBar"のように単語が連結されてしまう。ブロック要素の直後に
        # 改行を挿入してから結合することでこれを防ぐ
        # （2026-09-29のCodexレビューで指摘・修正）。
        xml = "<block>&lt;p&gt;Foo&lt;/p&gt;&lt;p&gt;Bar&lt;/p&gt;</block>"
        elem = etree.fromstring(xml)
        result = edinet_client._element_to_text(elem)
        self.assertNotIn("FooBar", result)
        self.assertIn("Foo", result)
        self.assertIn("Bar", result)


class TestGetEdinetCodeAlphanumericStockCode(unittest.TestCase):
    """2024年以降のTSEの新形式コード（数字3桁+英字1桁）への対応の回帰テスト。

    以前はcode.isdigit()のときしか末尾0を補わなかったため、新形式コードの
    銘柄（例: "634A"）はEDINETの証券コード列("634A0")と一致せずNoneを
    返していた（2026-09-28、上場承認通知への事業概要追加時に発見・修正）。
    """

    def _code_list_df(self):
        return pd.DataFrame(
            {
                "証券コード": ["634A0", "72030"],
                "ＥＤＩＮＥＴコード": ["E40756", "E00001"],
            }
        )

    def test_alphanumeric_4char_code_matches_trailing_zero_entry(self):
        with patch(f"{_MOD}._load_code_list", return_value=self._code_list_df()):
            result = edinet_client.get_edinet_code("634A")
        self.assertEqual(result, "E40756")

    def test_numeric_4char_code_still_matches(self):
        with patch(f"{_MOD}._load_code_list", return_value=self._code_list_df()):
            result = edinet_client.get_edinet_code("7203")
        self.assertEqual(result, "E00001")

    def test_unknown_code_returns_none(self):
        with patch(f"{_MOD}._load_code_list", return_value=self._code_list_df()):
            result = edinet_client.get_edinet_code("9999")
        self.assertIsNone(result)


class TestFindIpoProspectusCandidates(unittest.TestCase):
    def test_filters_by_doc_type_and_description_marker_newest_first(self):
        approval_date = dt.date(2026, 9, 1)
        docs_by_date = {
            dt.date(2026, 8, 20): [
                {
                    "edinetCode": "E40756",
                    "docTypeCode": "030",
                    "docDescription": "有価証券届出書（新規公開時）",
                    "docID": "S100AAA",
                    "submitDateTime": "2026-08-20 15:00",
                },
                # 別会社の同種届出書（対象外）
                {
                    "edinetCode": "E99999",
                    "docTypeCode": "030",
                    "docDescription": "有価証券届出書（新規公開時）",
                    "docID": "S100ZZZ",
                    "submitDateTime": "2026-08-20 15:00",
                },
                # 同じ会社だが投資信託の届出書（対象外、マーカーを含まない）
                {
                    "edinetCode": "E40756",
                    "docTypeCode": "030",
                    "docDescription": "有価証券届出書（内国投資信託受益証券）",
                    "docID": "S100BBB",
                    "submitDateTime": "2026-08-20 16:00",
                },
            ],
            dt.date(2026, 8, 25): [
                {
                    "edinetCode": "E40756",
                    "docTypeCode": "040",
                    "docDescription": "訂正有価証券届出書（新規公開時）",
                    "docID": "S100CCC",
                    "submitDateTime": "2026-08-25 15:00",
                },
            ],
        }

        def _get_documents(date, key):
            return docs_by_date.get(date, [])

        with patch(f"{_MOD}._api_key", return_value="dummy-key"), \
                patch(f"{_MOD}.today_jst", return_value=dt.date(2026, 9, 5)), \
                patch(f"{_MOD}._get_documents_for_date", side_effect=_get_documents):
            result = edinet_client.find_ipo_prospectus_candidates("E40756", approval_date)

        # 訂正後の届出書（より新しい提出日時）が先、原本が後に続く。
        self.assertEqual([doc["docID"] for doc in result], ["S100CCC", "S100AAA"])

    def test_no_matching_document_returns_empty_list(self):
        with patch(f"{_MOD}._api_key", return_value="dummy-key"), \
                patch(f"{_MOD}.today_jst", return_value=dt.date(2026, 9, 5)), \
                patch(f"{_MOD}._get_documents_for_date", return_value=[]):
            result = edinet_client.find_ipo_prospectus_candidates("E40756", dt.date(2026, 9, 1))
        self.assertEqual(result, [])


class TestFetchIpoBusinessOverview(unittest.TestCase):
    def test_happy_path_returns_extracted_text(self):
        with patch(f"{_MOD}.get_edinet_code", return_value="E40756"), \
                patch(f"{_MOD}.find_ipo_prospectus_candidates", return_value=[{"docID": "S100CCC"}]), \
                patch(f"{_MOD}._api_key", return_value="dummy-key"), \
                patch(f"{_MOD}._download_xbrl_zip", return_value=b"zip-bytes"), \
                patch(f"{_MOD}._find_text_block", return_value="当社は、菓子小売事業を行っております。") as mock_find:
            result = edinet_client.fetch_ipo_business_overview("634A", dt.date(2026, 9, 1))

        self.assertEqual(result, "当社は、菓子小売事業を行っております。")
        mock_find.assert_called_once_with(b"zip-bytes", edinet_client._BUSINESS_OVERVIEW_TAGS)

    def test_falls_back_to_older_filing_when_latest_correction_lacks_the_block(self):
        # 訂正有価証券届出書（新しい方）が訂正箇所だけの差分で「事業の内容」欄
        # 自体を含まない場合、より古い（原本を含む）書類にフォールバックする
        # （2026-09-29のCodexレビューで指摘・修正）。
        candidates = [{"docID": "S100CCC"}, {"docID": "S100AAA"}]

        def _download(doc_id, key):
            return f"zip-bytes-{doc_id}".encode()

        def _find_text(zip_bytes, tags):
            if zip_bytes == b"zip-bytes-S100AAA":
                return "当社は、菓子小売事業を行っております。"
            return None

        with patch(f"{_MOD}.get_edinet_code", return_value="E40756"), \
                patch(f"{_MOD}.find_ipo_prospectus_candidates", return_value=candidates), \
                patch(f"{_MOD}._api_key", return_value="dummy-key"), \
                patch(f"{_MOD}._download_xbrl_zip", side_effect=_download), \
                patch(f"{_MOD}._find_text_block", side_effect=_find_text):
            result = edinet_client.fetch_ipo_business_overview("634A", dt.date(2026, 9, 1))

        self.assertEqual(result, "当社は、菓子小売事業を行っております。")

    def test_no_edinet_code_returns_none_without_further_calls(self):
        with patch(f"{_MOD}.get_edinet_code", return_value=None), \
                patch(f"{_MOD}.find_ipo_prospectus_candidates") as mock_find_doc:
            result = edinet_client.fetch_ipo_business_overview("634A", dt.date(2026, 9, 1))

        self.assertIsNone(result)
        mock_find_doc.assert_not_called()

    def test_no_matching_document_returns_none(self):
        with patch(f"{_MOD}.get_edinet_code", return_value="E40756"), \
                patch(f"{_MOD}.find_ipo_prospectus_candidates", return_value=[]), \
                patch(f"{_MOD}._download_xbrl_zip") as mock_download:
            result = edinet_client.fetch_ipo_business_overview("634A", dt.date(2026, 9, 1))

        self.assertIsNone(result)
        mock_download.assert_not_called()

    def test_auth_error_is_swallowed(self):
        with patch(f"{_MOD}.get_edinet_code", side_effect=edinet_client.EdinetAuthError("no key")):
            result = edinet_client.fetch_ipo_business_overview("634A", dt.date(2026, 9, 1))
        self.assertIsNone(result)

    def test_unexpected_error_is_swallowed(self):
        with patch(f"{_MOD}.get_edinet_code", side_effect=RuntimeError("network error")):
            result = edinet_client.fetch_ipo_business_overview("634A", dt.date(2026, 9, 1))
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
