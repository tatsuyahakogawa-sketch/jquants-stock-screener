"""やのしん氏が個人運営する非公式TDnetミラーAPIのクライアント。

TDnet（適時開示情報閲覧サービス）自体には無料で使える構造化APIが無いため、
開示タイトル・企業コード・日時をJSON化して配信しているこの非公式ミラーを使う。
個人運営のため予告なく停止・仕様変更される可能性がある点に注意（README参照）。
その場合はTDnet公式サイトの直接スクレイピングか、JPXの有料TDnet APIへの
切り替えを検討すること。

注意: このAPIの `limit` パラメータは指定件数で無言で切り捨てる
（`total_count` もその切り捨てた件数をそのまま返すだけで、本当の合計件数
ではないことを2026-07-28に実機確認済み）。長い期間を一度に指定すると
古いデータが黙って欠落するため、`_MAX_DAYS_PER_REQUEST` 日ごとに
分割して取得している。

参考: https://webapi.yanoshin.jp/
"""
from __future__ import annotations

import datetime as dt
import warnings

import pandas as pd
import requests

from src import cache

TDNET_MIRROR_BASE_URL = "https://webapi.yanoshin.jp/webapi/tdnet/list"
_REQUEST_LIMIT = 10000
# 5日間で1000件超えることもあるため、20日/回であれば10000件の上限に
# 達する可能性は低いという前提（それでも達した場合は警告を出す）。
_MAX_DAYS_PER_REQUEST = 20


def _fetch_raw(start: dt.date, end: dt.date) -> list[dict]:
    date_str = f"{start.strftime('%Y%m%d')}-{end.strftime('%Y%m%d')}"
    cached = cache.load("tdnet_disclosures", date_str)
    if cached is not None:
        return cached.to_dict("records")
    resp = requests.get(
        f"{TDNET_MIRROR_BASE_URL}/{date_str}.json",
        params={"limit": _REQUEST_LIMIT},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    items = [item["Tdnet"] for item in data.get("items", [])]
    if len(items) < _REQUEST_LIMIT:
        # 上限に達した場合は呼び出し側が分割して再取得するので、ここでは
        # キャッシュしない（切り捨てられた不完全な結果を保存してしまうため）。
        cache.save("tdnet_disclosures", date_str, pd.DataFrame.from_records(items))
    return items


def _get_disclosures_chunk(start: dt.date, end: dt.date) -> list[dict]:
    """start〜endの開示を取得する。件数が上限に達した場合は決算集中期などで
    件数が多すぎる可能性があるため、期間を半分に分割して再取得する
    （欠落したデータを黙って返さないようにするため）。
    """
    items = _fetch_raw(start, end)
    if len(items) < _REQUEST_LIMIT:
        return items
    if start >= end:
        warnings.warn(
            f"TDnet({start})の1日分だけで取得上限({_REQUEST_LIMIT})に達しました。"
            "データが欠落している可能性があります。"
        )
        return items

    mid = start + (end - start) // 2
    return _get_disclosures_chunk(start, mid) + _get_disclosures_chunk(mid + dt.timedelta(days=1), end)


def get_disclosures_range(start: dt.date, end: dt.date) -> pd.DataFrame:
    """指定期間に開示された適時開示情報の一覧（タイトル・企業コード・日時等）を取得する。

    列: id, pubdate, company_code, company_name, title, document_url, markets_string 等

    件数上限による欠落を避けるため、期間を _MAX_DAYS_PER_REQUEST 日ごとの
    チャンクに分割して取得し、結合して返す。
    """
    all_items: list[dict] = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + dt.timedelta(days=_MAX_DAYS_PER_REQUEST - 1), end)
        all_items.extend(_get_disclosures_chunk(chunk_start, chunk_end))
        chunk_start = chunk_end + dt.timedelta(days=1)

    if not all_items:
        return pd.DataFrame()
    return pd.DataFrame.from_records(all_items).drop_duplicates(subset=["id"])
