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

import pandas as pd

from src.jpx_new_listings import (
    detect_listings_tomorrow,
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

    def test_rows_present_but_all_unparseable_raises(self):
        # rowspanを持つtrは見つかるが、セル構成の変更等で日付・コードが
        # 1件も取れなかった場合、空のDataFrameを「新規上場0件」として黙って
        # 返すと実際にある上場承認・本日上場を見逃す（2026-08-27のCodexレビューで
        # 指摘・修正）。
        html = """
        <html><body><table><tbody>
        <tr>
          <td rowspan="2">日付形式が変わって取れない</td>
          <td rowspan="2"><a href="https://example.com">何かの会社</a></td>
        </tr>
        <tr><td>スタンダード</td></tr>
        </tbody></table></body></html>
        """
        with self.assertRaises(ValueError):
            parse_new_listing_table(html)

    def test_one_unparseable_row_among_others_still_raises(self):
        # 他の行は正常に解析できていても、1件でも解析できない行があれば
        # 例外を送出する。件数ベースで「全滅」しか検知しないと、この
        # ケース（1件だけ読み飛ばし）が検出漏れのまま静かに処理され、
        # watch_and_notify.pyがそのままスキャン成功とみなしてipo_watermarkを
        # 進めてしまい、その銘柄の上場承認・本日上場を永久に見逃す
        # （2026-08-28の9巡目のCodexレビューで指摘・修正）。
        html = _SAMPLE_HTML.replace(
            '<td rowspan="2">2026/08/04<br />（2026/06/30）</td>',
            '<td rowspan="2">日付形式が変わって取れない</td>',
        )
        with self.assertRaises(ValueError):
            parse_new_listing_table(html)


class TestDetectNewListingApprovals(unittest.TestCase):
    def test_approval_on_or_after_since_is_included(self):
        df = parse_new_listing_table(_SAMPLE_HTML)
        hit = detect_new_listing_approvals(df, since=dt.date(2026, 8, 24))
        self.assertEqual(list(hit["Code"]), ["634A"])

    def test_approval_before_since_is_excluded(self):
        df = parse_new_listing_table(_SAMPLE_HTML)
        hit = detect_new_listing_approvals(df, since=dt.date(2026, 8, 25))
        self.assertTrue(hit.empty)


class TestDetectListingsTomorrow(unittest.TestCase):
    def test_listing_date_matching_tomorrow_is_included(self):
        # サンプルデータの634Aの上場日は2026-09-25。today=2026-09-24なら
        # その翌日(明日)に当たるため、前日リマインダーの対象になる。
        df = parse_new_listing_table(_SAMPLE_HTML)
        hit = detect_listings_tomorrow(df, today=dt.date(2026, 9, 24))
        self.assertEqual(list(hit["Code"]), ["634A"])

    def test_listing_date_matching_today_is_excluded(self):
        # 上場当日に知らせても既に取引が始まっており手遅れなため、
        # today自身と一致する上場日は対象外（前日にのみ知らせる）。
        df = parse_new_listing_table(_SAMPLE_HTML)
        hit = detect_listings_tomorrow(df, today=dt.date(2026, 9, 25))
        self.assertTrue(hit.empty)

    def test_listing_date_further_in_the_future_is_excluded(self):
        df = parse_new_listing_table(_SAMPLE_HTML)
        hit = detect_listings_tomorrow(df, today=dt.date(2026, 9, 17))
        self.assertTrue(hit.empty)

    def test_listing_date_in_the_past_is_excluded(self):
        df = parse_new_listing_table(_SAMPLE_HTML)
        hit = detect_listings_tomorrow(df, today=dt.date(2026, 9, 26))
        self.assertTrue(hit.empty)

    def test_missed_reminder_is_not_caught_up_later(self):
        # 意図的にcatch-upしない。前日(2026-09-24)に一時的な失敗で送れな
        # かった場合、後日(2026-09-26)の実行でも再送されない。上場当日以降に
        # 「前日リマインダー」を送ってもユーザーにとって無意味なため、遅れて
        # 送るよりは送らない方がよいとユーザーが明言した（2026-09-17）。
        df = parse_new_listing_table(_SAMPLE_HTML)
        hit = detect_listings_tomorrow(df, today=dt.date(2026, 9, 26))
        self.assertTrue(hit.empty)

    def test_monday_listing_is_reminded_on_preceding_friday(self):
        # 単純な「today+1日」との一致だと、月曜上場の前日は日曜(非営業日)に
        # 当たり、ワークフロー自体が動かないため永久に検出できない
        # （2026-09-17のCodexレビューで指摘・修正）。2026-08-31(月)は
        # 上場日、その直前の営業日は2026-08-28(金)（間の8/29・30は土日）。
        monday_listing = dt.date(2026, 8, 31)
        listings = pd.DataFrame([
            {
                "Code": "999A", "CompanyName": "月曜上場テスト", "MarketSegment": "グロース",
                "ListingDate": monday_listing, "ApprovalDate": None,
            },
        ])
        hit = detect_listings_tomorrow(listings, today=dt.date(2026, 8, 28))
        self.assertEqual(list(hit["Code"]), ["999A"])
        # 金曜より前(木曜)や土日自体では、まだ「次の営業日」が月曜にならない
        # ため対象外（木曜の次の営業日は金曜）。
        self.assertTrue(detect_listings_tomorrow(listings, today=dt.date(2026, 8, 27)).empty)

    def test_listing_after_multi_day_holiday_bridge_is_reminded_on_preceding_trading_day(self):
        # 2026-09-21(月・敬老の日)〜09-23(水・秋分の日)は祝日が連続する
        # （間の09-22(火)も国民の休日）。この間ワークフローは一度も動かない
        # ため、直前の営業日である09-18(金)が「次の営業日」を計算する起点に
        # なり、その次の営業日である09-24(木)上場の銘柄がここでリマインド
        # される。
        listing_after_holidays = dt.date(2026, 9, 24)
        listings = pd.DataFrame([
            {
                "Code": "888A", "CompanyName": "連休明け上場テスト", "MarketSegment": "スタンダード",
                "ListingDate": listing_after_holidays, "ApprovalDate": None,
            },
        ])
        hit = detect_listings_tomorrow(listings, today=dt.date(2026, 9, 18))
        self.assertEqual(list(hit["Code"]), ["888A"])


if __name__ == "__main__":
    unittest.main()
