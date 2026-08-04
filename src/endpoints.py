"""J-Quants の各エンドポイントを「日付単位のバルク取得 + ローカルキャッシュ」で
呼び出すヘルパー。

Free プランは呼び出し回数(5件/分)の制限が厳しいため、銘柄ごとに個別リクエスト
するのではなく、日付を指定して「その日の全銘柄分」を1回のページネーション処理
でまとめて取得する方式にしている。
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from src import cache
from src.config import LISTING_LOOKBACK_YEARS
from src.jquants_client import JQuantsClient


def _daterange(start: dt.date, end: dt.date) -> list[dt.date]:
    days = (end - start).days
    return [start + dt.timedelta(days=i) for i in range(days + 1)]


def get_listed_info(client: JQuantsClient, date: dt.date | None = None) -> pd.DataFrame:
    """上場銘柄マスタ(/v2/equities/master)を取得する。date未指定の場合は最新時点の一覧。"""
    params = {}
    if date is not None:
        params["date"] = date.strftime("%Y%m%d")
    records = list(client.get_all_pages("/equities/master", params))
    return pd.DataFrame.from_records(records)


def get_daily_quotes_by_date(client: JQuantsClient, date: dt.date) -> pd.DataFrame:
    """指定日の全銘柄分の株価四本値(/v2/equities/bars/daily)を取得する（キャッシュ利用）。"""
    date_str = date.strftime("%Y%m%d")
    cached = cache.load("daily_quotes", date_str)
    if cached is not None:
        return cached
    records = list(client.get_all_pages("/equities/bars/daily", {"date": date_str}))
    df = pd.DataFrame.from_records(records)
    cache.save("daily_quotes", date_str, df)
    return df


def get_statements_by_date(client: JQuantsClient, date: dt.date) -> pd.DataFrame:
    """指定日に開示された決算短信等の財務情報(/v2/fins/summary)を取得する（キャッシュ利用）。"""
    date_str = date.strftime("%Y%m%d")
    cached = cache.load("statements", date_str)
    if cached is not None:
        return cached
    records = list(client.get_all_pages("/fins/summary", {"date": date_str}))
    df = pd.DataFrame.from_records(records)
    cache.save("statements", date_str, df)
    return df


def get_daily_quotes_range(client: JQuantsClient, start: dt.date, end: dt.date) -> pd.DataFrame:
    """期間内の全営業日について株価四本値を取得し1つのDataFrameにまとめる。
    非営業日は0件が返るだけなので、土日祝日も含めて呼んでよい。
    """
    frames = [get_daily_quotes_by_date(client, d) for d in _daterange(start, end)]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def get_statements_range(client: JQuantsClient, start: dt.date, end: dt.date) -> pd.DataFrame:
    """期間内の全日について決算開示情報を取得し1つのDataFrameにまとめる。"""
    frames = [get_statements_by_date(client, d) for d in _daterange(start, end)]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def get_financials_by_code(client: JQuantsClient, code: str) -> pd.DataFrame:
    """指定銘柄の決算開示履歴(/v2/fins/summary?code=)を全件取得する（当日分をキャッシュ）。
    PER/PBR/配当利回り計算用の最新EPS・BPS・配当予想等を得るために使う。
    """
    today_str = dt.date.today().strftime("%Y%m%d")
    cache_key = f"{code}_{today_str}"
    cached = cache.load("fins_by_code", cache_key)
    if cached is not None:
        return cached
    records = list(client.get_all_pages("/fins/summary", {"code": code}))
    df = pd.DataFrame.from_records(records)
    cache.save("fins_by_code", cache_key, df)
    return df


def get_price_history_by_code(client: JQuantsClient, code: str, lookback_years: int = LISTING_LOOKBACK_YEARS) -> pd.DataFrame:
    """指定銘柄の株価四本値を契約プランの取得可能期間（Lightは過去5年）分まとめて取得する。

    時価総額・PER・PBR計算用の最新終値と、上場5年以内かどうかの近似判定用の
    データ開始日（最も古い取得日）の両方をこの1回の取得結果から求める。
    当日分をキャッシュする。
    """
    end = dt.date.today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=365 * lookback_years)
    today_str = dt.date.today().strftime("%Y%m%d")
    cache_key = f"{code}_{lookback_years}y_{today_str}"
    cached = cache.load("price_history_by_code", cache_key)
    if cached is not None:
        return cached
    records = list(client.get_all_pages(
        "/equities/bars/daily",
        {"code": code, "from": start.strftime("%Y%m%d"), "to": end.strftime("%Y%m%d")},
    ))
    df = pd.DataFrame.from_records(records)
    cache.save("price_history_by_code", cache_key, df)
    return df
