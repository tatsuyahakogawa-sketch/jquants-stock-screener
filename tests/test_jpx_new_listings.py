"""src/jpx_new_listings.py の単体テスト。

parse_new_listing_table()のテストは、JPX公式サイト
(https://www.jpx.co.jp/listing/stocks/new/index.html)の実際のページを
2026-08-27に取得して確認した表構造（1銘柄につき2行(<tr>)の組。1行目に
rowspan=2の上場日セル(上場承認日を括弧書きで併記)・会社名セル・コードセル、
2行目の先頭セルに市場区分）を模したHTMLで検証する。

実行方法:
    python -m unittest discover tests
"""
from __future__ import annotations

import datetime as dt
import unittest

from src.jpx_new_listings import (
    detect_listings_today,
    detect_new_listing_approvals,
    parse_new_listing_table,
)

_SAMPLE_HTML = """
<html><body>
<table>
<tbody>
<tr>
  <td rowspan="2">2026/09/25<br />（2026/08/24）</td>
  <td rowspan="2"><a href="https://example.com/a">（株）レイヤード</a></td>
  <td><span id="634A"></span>634A</td>
  <td>-</td>
  <td>600</td>
  <td>100</td>
</tr>
<tr>
  <td>スタンダード</td>
  <td>-</td>
  <td>250(OA127.5)</td>
</tr>
<tr>
  <td rowspan="2">2026/08/04<br />（2026/06/30）</td>
  <td rowspan="2">
    <a href="https://example.com/b">（株）エブリー</a>
    <a href="https://example.com/interview">代表者インタビュー</a>
  </td>
  <td><span id="607A"></span>607A</td>
  <td>-</td>
  <td>1105.3</td>
  <td>100</td>
</tr>
<tr>
  <td>グロース</td>
  <td>-</td>
  <td>4815.1(OA888)</td>
</tr>
</tbody>
</table>
</body></html>
"""


class TestParseNewListingTable(unittest.TestCase):
    def test_extracts_listing_and_approval_dates(self):
        df = parse_new_listing_table(_SAMPLE_HTML)
        self.assertEqual(len(df), 2)
        row = df.loc[df["Code"] == "634A"].iloc[0]
        self.assertEqual(row["CompanyName"], "（株）レイヤード")
        self.assertEqual(row["MarketSegment"], "スタンダード")
        self.assertEqual(row["ListingDate"], dt.date(2026, 9, 25))
        self.assertEqual(row["ApprovalDate"], dt.date(2026, 8, 24))

    def test_ignores_interview_link_when_extracting_company_name(self):
        # 会社名セルに「代表者インタビュー」への別リンクが同居していても、
        # 先頭の会社名リンクだけを使う。
        df = parse_new_listing_table(_SAMPLE_HTML)
        row = df.loc[df["Code"] == "607A"].iloc[0]
        self.assertEqual(row["CompanyName"], "（株）エブリー")

    def test_unrecognizable_structure_raises_instead_of_returning_empty(self):
        # ページ構造が変わって1件も抽出できなかった場合、静かに「新規上場0件」
        # と扱うのではなく例外を送出する（CLAUDE.md「取得できない場合は
        # 『取得不可』と判断できる」参照）。
        with self.assertRaises(ValueError):
            parse_new_listing_table("<html><body><p>no table here</p></body></html>")


class TestDetectNewListingApprovals(unittest.TestCase):
    def test_approval_on_or_after_since_is_included(self):
        df = parse_new_listing_table(_SAMPLE_HTML)
        hit = detect_new_listing_approvals(df, since=dt.date(2026, 8, 24))
        self.assertEqual(list(hit["Code"]), ["634A"])

    def test_approval_before_since_is_excluded(self):
        df = parse_new_listing_table(_SAMPLE_HTML)
        hit = detect_new_listing_approvals(df, since=dt.date(2026, 8, 25))
        self.assertTrue(hit.empty)


class TestDetectListingsToday(unittest.TestCase):
    def test_listing_date_matching_today_is_included(self):
        df = parse_new_listing_table(_SAMPLE_HTML)
        hit = detect_listings_today(df, today=dt.date(2026, 9, 25))
        self.assertEqual(list(hit["Code"]), ["634A"])

    def test_listing_date_not_matching_today_is_excluded(self):
        df = parse_new_listing_table(_SAMPLE_HTML)
        hit = detect_listings_today(df, today=dt.date(2026, 9, 26))
        self.assertTrue(hit.empty)


if __name__ == "__main__":
    unittest.main()
