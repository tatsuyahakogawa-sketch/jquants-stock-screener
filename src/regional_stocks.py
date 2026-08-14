"""地方証券取引所（札幌・福岡・名古屋）単独上場企業の検出・追跡。

J-Quantsは東証(TSE)とTOKYO PRO Marketのみが対象で、地方取引所独自市場
（Q-Board、アンビシャス等）の銘柄は上場銘柄マスタ・株価データから完全に
除外される（CLAUDE.md「データ対象範囲の制約」参照）。そのため本モジュールは
J-Quantsを使わず、TDnet開示の`markets_string`列（開示時点でどの取引所に
上場していたかを示す1〜数文字: 東=東証, 福=福証, 名=名証, 札=札証。
実データで確認済み、2026-08-13）を手がかりに地方単独上場企業を特定する。

検出できること:
  - 地方単独上場企業の新規上場・重複上場（申請/承認/実施の段階を区別）
  - 東証への新規上場・重複上場・市場変更（最重要イベントとして区別）
  - M&A・TOB・資本業務提携等の大型企業イベント

検出できないこと（既知の制約、誤った推測値を出さないためあえて空欄にする）:
  - 地方単独上場企業の時価総額はJ-Quantsに株価データが無いため計算不能。
    福証単独上場企業のみyfinanceの`.F`サフィックスで現在値を取得できることを
    実機確認済み（2026-08-13、9388で確認）。名証・札証単独上場企業や、
    新規上場直後の英数字コード銘柄（482A0等）はyfinanceでも取得できない
    ことを同日に確認済みのため、その場合は時価総額を「取得不可」とする。

毎回全期間のTDnetデータを再走査すると重くなるため、前回スキャン済み日付
（ウォーターマーク）をキャッシュに保存し、次回はその翌日からだけ追加取得する
（初回のみREGIONAL_LISTING_LOOKBACK_YEARS年分を遡る）。
"""
from __future__ import annotations

import datetime as dt
import logging

import pandas as pd

from src import cache, tdnet_client
from src.config import REGIONAL_LISTING_LOOKBACK_YEARS

logger = logging.getLogger(__name__)

REGIONAL_MARKET_LABELS = {"福": "福証", "名": "名証", "札": "札証"}
TOKYO_MARKET_CHAR = "東"

_STORE_ENDPOINT = "regional_stocks"
_COMPANY_STATUS_KEY = "company_status"
_LISTING_EVENTS_KEY = "listing_events"
_MAJOR_EVENTS_KEY = "major_events"
_WATERMARK_KEY = "watermark"

# yfinanceで現在値取得を実機確認済みのサフィックス（2026-08-13、9388で確認）。
# 名証・札証単独上場企業は同日に実機確認の上、同サフィックス方式では取得できな
# かったため対象外（取得不可のまま扱う。README参照）。
_YFINANCE_SUFFIX_BY_MARKET = {"福": "F"}

# 株価に大きな影響を与えうる大型企業イベントのキーワード（ユーザー指定）。
# タイトルのキーワード一致による検出のため、実際の内容はUrl先で必ず確認すること
# （既存のTDNET_TITLE_BASED_RULESと同じ注意点）。
MAJOR_EVENT_KEYWORDS = [
    "M&A", "買収", "子会社化", "TOB", "MBO", "資本業務提携", "出資",
    "大型受注", "大型契約",
]

_LISTING_KEYWORD = "上場"
_APPLICATION_KEYWORDS = ["申請"]
_APPROVAL_KEYWORDS = ["承認"]
_TOKYO_KEYWORDS = ["東京証券取引所", "東証"]
_MARKET_NAME_KEYWORDS = ["東京証券取引所", "名古屋証券取引所", "札幌証券取引所", "福岡証券取引所"]

_LISTING_EVENTS_COLUMNS = [
    "id", "Code", "CompanyName", "Date", "MarketsString", "TargetMarkets",
    "Stage", "IsTokyoRelated", "Title", "Url",
]
_MAJOR_EVENTS_COLUMNS = [
    "id", "Code", "CompanyName", "Date", "MarketsString", "Title", "Url", "MatchedKeyword",
]
_COMPANY_STATUS_COLUMNS = [
    "Code", "CompanyName", "MarketsString", "LastSeenDate", "MarketCap", "MarketCapNote",
]

_REQUIRED_DISCLOSURE_COLUMNS = {
    "id", "company_code", "company_name", "title", "pubdate", "markets_string", "document_url",
}


def is_regional_only(markets_string: str | None) -> bool:
    """開示時点でのmarkets_stringが「東証を含まない」=地方単独上場かどうか。"""
    if not markets_string:
        return False
    return TOKYO_MARKET_CHAR not in markets_string


def regional_markets_in(markets_string: str | None) -> list[str]:
    """markets_stringに含まれる地方取引所名（表示用）を出現順に返す。"""
    if not markets_string:
        return []
    return [label for char, label in REGIONAL_MARKET_LABELS.items() if char in markets_string]


def _legacy_ticker(code: str) -> str:
    """J-Quants/TDnetの5桁コード（4桁+普通株式サフィックス"0"）から、
    yfinance等で使う4桁（英数字含む）の実質コードを取り出す。

    末尾の"0"を`rstrip("0")`で取り除くと、実コード自体が0で終わる場合
    （例: 実コード"7200"→5桁"72000"）に複数文字削られてしまうため、
    固定で先頭4文字を使う。
    """
    return code[:4] if len(code) >= 5 else code


def _classify_listing_stage(title: str) -> str | None:
    """上場関連の開示タイトルから段階（申請/承認/上場）を判定する。
    上場に無関係なタイトルはNoneを返す。
    """
    if _LISTING_KEYWORD not in title:
        return None
    if any(k in title for k in _APPLICATION_KEYWORDS):
        return "申請"
    if any(k in title for k in _APPROVAL_KEYWORDS):
        return "承認"
    if "上場のお知らせ" in title or "上場に関するお知らせ" in title or "上場について" in title:
        return "上場"
    return None


def _filter_regional_only(disclosures_df: pd.DataFrame) -> pd.DataFrame:
    if disclosures_df.empty or not _REQUIRED_DISCLOSURE_COLUMNS.issubset(disclosures_df.columns):
        return pd.DataFrame(columns=list(_REQUIRED_DISCLOSURE_COLUMNS))
    df = disclosures_df.copy()
    return df.loc[df["markets_string"].apply(is_regional_only)]


def detect_regional_listing_events(disclosures_df: pd.DataFrame) -> pd.DataFrame:
    """地方単独上場企業の新規上場・重複上場・市場変更関連の開示を検出する。

    東京証券取引所への上場申請・承認・上場に関するものはIsTokyoRelated=True
    として区別する（②の「最重要イベント」向け）。
    """
    regional = _filter_regional_only(disclosures_df)
    if regional.empty:
        return pd.DataFrame(columns=_LISTING_EVENTS_COLUMNS)

    stages = regional["title"].fillna("").apply(_classify_listing_stage)
    hit = regional.loc[stages.notna()].copy()
    if hit.empty:
        return pd.DataFrame(columns=_LISTING_EVENTS_COLUMNS)

    hit["Stage"] = stages.loc[hit.index]
    hit["IsTokyoRelated"] = hit["title"].fillna("").apply(lambda t: any(k in t for k in _TOKYO_KEYWORDS))
    hit["TargetMarkets"] = hit["title"].fillna("").apply(
        lambda t: [name for name in _MARKET_NAME_KEYWORDS if name in t]
    )
    hit["Date"] = pd.to_datetime(hit["pubdate"], errors="coerce")

    result = hit.rename(columns={
        "company_code": "Code",
        "company_name": "CompanyName",
        "markets_string": "MarketsString",
        "title": "Title",
        "document_url": "Url",
    })
    return result[_LISTING_EVENTS_COLUMNS].reset_index(drop=True)


def detect_regional_major_events(disclosures_df: pd.DataFrame) -> pd.DataFrame:
    """地方単独上場企業のM&A・TOB・資本業務提携等、株価に影響しうる大型イベントを検出する。

    タイトルのキーワード一致による検出のため、実際の内容はUrl先で必ず確認すること。
    """
    regional = _filter_regional_only(disclosures_df)
    if regional.empty:
        return pd.DataFrame(columns=_MAJOR_EVENTS_COLUMNS)

    titles = regional["title"].fillna("")
    matched_keyword = titles.apply(lambda t: next((k for k in MAJOR_EVENT_KEYWORDS if k in t), None))
    hit = regional.loc[matched_keyword.notna()].copy()
    if hit.empty:
        return pd.DataFrame(columns=_MAJOR_EVENTS_COLUMNS)

    hit["MatchedKeyword"] = matched_keyword.loc[hit.index]
    hit["Date"] = pd.to_datetime(hit["pubdate"], errors="coerce")
    result = hit.rename(columns={
        "company_code": "Code",
        "company_name": "CompanyName",
        "markets_string": "MarketsString",
        "title": "Title",
        "document_url": "Url",
    })
    return result[_MAJOR_EVENTS_COLUMNS].reset_index(drop=True)


def _latest_company_status(disclosures_df: pd.DataFrame) -> pd.DataFrame:
    """地方単独上場の開示から、銘柄ごとの最新のmarkets_string・会社名・最終確認日を集計する。"""
    regional = _filter_regional_only(disclosures_df)
    if regional.empty:
        return pd.DataFrame(columns=["Code", "CompanyName", "MarketsString", "LastSeenDate"])

    df = regional.copy()
    df["Date"] = pd.to_datetime(df["pubdate"], errors="coerce")
    df = df.sort_values("Date")
    latest = df.groupby("company_code").tail(1)
    return latest.rename(columns={
        "company_code": "Code",
        "company_name": "CompanyName",
        "markets_string": "MarketsString",
        "Date": "LastSeenDate",
    })[["Code", "CompanyName", "MarketsString", "LastSeenDate"]].reset_index(drop=True)


def fetch_regional_market_cap(code: str, markets_string: str) -> tuple[float | None, str]:
    """地方単独上場企業の時価総額(近似: 現在値のみ、株数は別途J-Quants等が必要)を試みる。

    株数(発行済株式数)を取得できる手段が無いため、実際には「現在値が取得できるか」
    までしか分からない。ここでは現在値をそのままMarketCapの代用値として返す
    （呼び出し側で「時価総額(現在値のみ)」であることを明示する前提）。
    取得できない場合はNoneと理由文字列を返す（誤った推測値を出さない）。
    """
    suffix = next((s for char, s in _YFINANCE_SUFFIX_BY_MARKET.items() if char in (markets_string or "")), None)
    if suffix is None:
        return None, "取得不可（この取引所の銘柄はyfinanceでも株価が取得できないことを確認済み）"

    ticker = f"{_legacy_ticker(code)}.{suffix}"
    try:
        import yfinance as yf

        price = yf.Ticker(ticker).fast_info.get("lastPrice")
    except Exception as exc:  # noqa: BLE001 -- 銘柄によって取得できないことが前提のため、失敗しても取得不可として継続する
        logger.info("[%s] yfinance(%s)での株価取得に失敗しました: %s", code, ticker, exc)
        return None, "取得不可（yfinance取得エラー）"

    if price is None or pd.isna(price):
        return None, "取得不可（yfinanceに株価データなし）"
    return float(price), ""


def _load_watermark() -> dt.date | None:
    df = cache.load(_STORE_ENDPOINT, _WATERMARK_KEY)
    if df is None or df.empty:
        return None
    return pd.to_datetime(df.iloc[0]["date"]).date()


def _save_watermark(date: dt.date) -> None:
    cache.save(_STORE_ENDPOINT, _WATERMARK_KEY, pd.DataFrame({"date": [date.isoformat()]}))


def _load_table(key: str, columns: list[str]) -> pd.DataFrame:
    df = cache.load(_STORE_ENDPOINT, key)
    return df if df is not None else pd.DataFrame(columns=columns)


def load_regional_store() -> dict[str, pd.DataFrame]:
    """更新はせず、保存済みの検出結果だけを読む（Streamlitページの通常表示用）。"""
    return {
        "company_status": _load_table(_COMPANY_STATUS_KEY, _COMPANY_STATUS_COLUMNS),
        "listing_events": _load_table(_LISTING_EVENTS_KEY, _LISTING_EVENTS_COLUMNS),
        "major_events": _load_table(_MAJOR_EVENTS_KEY, _MAJOR_EVENTS_COLUMNS),
    }


def update_regional_store(today: dt.date | None = None) -> dict[str, pd.DataFrame]:
    """前回スキャン済み日付の翌日から今日までのTDnet開示だけを追加取得し、
    地方単独上場企業の状況・上場イベント・大型イベントの保存済みデータに
    追記して返す（初回はREGIONAL_LISTING_LOOKBACK_YEARS年分を遡って取得）。
    """
    today = today or dt.date.today()
    watermark = _load_watermark()
    start = (
        watermark + dt.timedelta(days=1)
        if watermark is not None
        else today - dt.timedelta(days=365 * REGIONAL_LISTING_LOOKBACK_YEARS)
    )

    stores = load_regional_store()
    if start > today:
        return stores

    new_disclosures = tdnet_client.get_disclosures_range(start, today)

    new_listing = detect_regional_listing_events(new_disclosures)
    new_major = detect_regional_major_events(new_disclosures)
    new_status = _latest_company_status(new_disclosures)

    listing_events = (
        pd.concat([stores["listing_events"], new_listing], ignore_index=True)
        .drop_duplicates(subset=["id"])
        .sort_values("Date")
        .reset_index(drop=True)
    )
    major_events = (
        pd.concat([stores["major_events"], new_major], ignore_index=True)
        .drop_duplicates(subset=["id"])
        .sort_values("Date")
        .reset_index(drop=True)
    )
    company_status = (
        pd.concat([stores["company_status"], new_status], ignore_index=True)
        .sort_values("LastSeenDate")
        .drop_duplicates(subset=["Code"], keep="last")
        .reset_index(drop=True)
    )

    # 現在も地方単独上場のままの銘柄だけ、時価総額(現在値)を更新する
    # （東証を含む市場に移った銘柄は対象外。既存値のまま保持する）。
    still_regional = company_status["MarketsString"].apply(is_regional_only)
    for idx in company_status.loc[still_regional].index:
        code = company_status.at[idx, "Code"]
        markets_string = company_status.at[idx, "MarketsString"]
        price, note = fetch_regional_market_cap(code, markets_string)
        company_status.at[idx, "MarketCap"] = price
        company_status.at[idx, "MarketCapNote"] = note

    cache.save(_STORE_ENDPOINT, _LISTING_EVENTS_KEY, listing_events)
    cache.save(_STORE_ENDPOINT, _MAJOR_EVENTS_KEY, major_events)
    cache.save(_STORE_ENDPOINT, _COMPANY_STATUS_KEY, company_status)
    _save_watermark(today)

    return {
        "company_status": company_status,
        "listing_events": listing_events,
        "major_events": major_events,
    }
