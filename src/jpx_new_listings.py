"""JPX公式サイトの「新規上場会社情報」ページから、東証本体
（プライム/スタンダード/グロース）への新規上場を検出する。

https://www.jpx.co.jp/listing/stocks/new/index.html は日次更新され、
1つの表に「上場日」と「上場承認日」（上場日セル内に括弧書きで併記）が
銘柄コード単位で載っている。地方単独上場企業やTOKYO PRO Market銘柄の
新規上場検出（src/regional_stocks.py）とは別に、このページを使うことで
東証本体への完全新規IPOの「上場承認」（事前告知）と「本日上場」の両方を
1つのデータソースから検出できる（2026-08-27に実機のページ構造を確認して
設計。TDnet開示は上場前の会社自身のTDnetアカウントが無いため、投資先の
株式会社が上場承認を受けたことを出資会社側が任意で開示することがある
だけで網羅的な検出ができないことを実データで確認済み）。

ページは`<meta charset="UTF-8">`でUTF-8エンコード（2026-08-27に実機確認。
requestsが自動判定するエンコーディングはUTF-8以外になることがあるため
明示的にUTF-8でデコードする）。

表の構造（実機確認）: 1銘柄につき2行(<tr>)の組で構成される。
  1行目: 上場日セル(rowspan=2, "YYYY/MM/DD<br/>（YYYY/MM/DD）"の形式で
         上場日と上場承認日を含む)、会社名セル(rowspan=2, 先頭の<a>が
         会社名。行によっては2つ目の<a>で「代表者インタビュー」への
         リンクが同じセル内に含まれることがあるため、先頭の<a>だけを使う)、
         コードセル(`<span id="コード"></span>`の直後にコード文字列)、
         それ以降は仮条件・公募株数等の列。
  2行目: 市場区分セル（そのtrの最初のtd）、それ以降はCG報告書等の列。
"""
from __future__ import annotations

import datetime as dt
import re

import pandas as pd
import requests
from lxml import html as lxml_html

JPX_NEW_LISTING_URL = "https://www.jpx.co.jp/listing/stocks/new/index.html"
# JPXはUser-Agent無しのリクエストを403で拒否する（2026-08-27に実機確認）。
_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

_DATE_PATTERN = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})")
_INTERVIEW_LINK_TEXT = "代表者インタビュー"

NEW_LISTINGS_COLUMNS = ["Code", "CompanyName", "MarketSegment", "ListingDate", "ApprovalDate"]


def _parse_date(text: str) -> dt.date | None:
    m = _DATE_PATTERN.search(text)
    if not m:
        return None
    year, month, day = (int(g) for g in m.groups())
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def _extract_listing_and_approval_date(cell) -> tuple[dt.date | None, dt.date | None]:
    """上場日セルのテキストノードから (上場日, 上場承認日) を取り出す。

    "2026/09/25" と、括弧内の "（2026/08/24）" がそれぞれ独立したテキスト
    ノードとして<br/>で区切られているため、テキストノードを1つずつ確認する。
    """
    texts = [t.strip() for t in cell.xpath(".//text()") if t.strip()]
    listing_date = None
    approval_date = None
    for t in texts:
        parsed = _parse_date(t)
        if parsed is None:
            continue
        if "（" in t or "(" in t:
            approval_date = parsed
        elif listing_date is None:
            listing_date = parsed
    return listing_date, approval_date


def _extract_company_name(cell) -> str:
    links = cell.xpath(".//a")
    for link in links:
        text = (link.text_content() or "").strip()
        if text and text != _INTERVIEW_LINK_TEXT:
            return text
    text = (cell.text_content() or "").strip()
    return text


def parse_new_listing_table(html_text: str) -> pd.DataFrame:
    """JPX新規上場会社情報ページのHTML文字列（デコード済み）を表形式に変換する。

    ページ構造が想定と異なりデータ行を1件も抽出できなかった場合は、誤って
    「新規上場0件」と静かに扱うことを避けるため空のDataFrameではなく例外を送出する
    （呼び出し側でこれを検知してユーザーに通知する。CLAUDE.md「取得できない場合は
    『取得不可』と判断できる」参照）。
    """
    tree = lxml_html.fromstring(html_text)
    rows = tree.xpath("//tr[td[1][@rowspan]]")
    if not rows:
        raise ValueError(
            "JPX新規上場会社情報ページの表構造を解析できませんでした"
            "（ページのHTML構造が変更された可能性があります）。"
        )

    records = []
    for primary_tr in rows:
        tds = primary_tr.xpath("./td")
        if len(tds) < 3:
            continue
        listing_date, approval_date = _extract_listing_and_approval_date(tds[0])
        company_name = _extract_company_name(tds[1])
        code = (tds[2].text_content() or "").strip()
        if listing_date is None or not code:
            continue

        market_segment = ""
        secondary_tr = primary_tr.xpath("following-sibling::tr[1]")
        if secondary_tr:
            secondary_tds = secondary_tr[0].xpath("./td")
            if secondary_tds:
                market_segment = (secondary_tds[0].text_content() or "").strip()

        records.append({
            "Code": code,
            "CompanyName": company_name,
            "MarketSegment": market_segment,
            "ListingDate": listing_date,
            "ApprovalDate": approval_date,
        })

    if not records:
        # rows自体は見つかったが、セル構成の変更等で1件も抽出できなかった
        # 場合。ここで空のDataFrameを黙って返すと「新規上場0件」と区別が
        # つかず、実際には上場承認・本日上場があるのに気付けなくなる
        # （2026-08-27のCodexレビューで指摘）。
        raise ValueError(
            "JPX新規上場会社情報ページの表から1件も抽出できませんでした"
            "（ページのHTML構造が変更された可能性があります）。"
        )

    return pd.DataFrame(records, columns=NEW_LISTINGS_COLUMNS)


def fetch_new_listing_table() -> pd.DataFrame:
    """JPX公式サイトから新規上場会社情報の表を取得する（キャッシュしない。
    このページ自体が日次更新の最新スナップショットで、過去分の再現ができない
    「今の状態」の情報のため、呼び出し側で前回チェック時点との差分を取る）。
    """
    resp = requests.get(JPX_NEW_LISTING_URL, headers=_REQUEST_HEADERS, timeout=30)
    resp.raise_for_status()
    html_text = resp.content.decode("utf-8", errors="replace")
    return parse_new_listing_table(html_text)


def detect_new_listing_approvals(listings: pd.DataFrame, since: dt.date) -> pd.DataFrame:
    """上場承認日がsince以降（sinceを含む）の銘柄を返す（事前告知の通知用）。"""
    if listings.empty:
        return listings
    hit = listings.loc[listings["ApprovalDate"].notna() & (listings["ApprovalDate"] >= since)]
    return hit.reset_index(drop=True)


def detect_listings_today(listings: pd.DataFrame, today: dt.date) -> pd.DataFrame:
    """上場日がちょうどtodayの銘柄を返す（本日上場の通知用）。"""
    if listings.empty:
        return listings
    hit = listings.loc[listings["ListingDate"] == today]
    return hit.reset_index(drop=True)
