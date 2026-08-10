"""日付単位のローカルキャッシュ（+ Supabaseへの永続化）。

J-Quants Free プランは呼び出し回数の制限が厳しいため、一度取得した
日付分のデータはparquetでローカル保存し、再実行時の再取得を避ける。

Streamlit Community Cloudはデプロイ毎にコンテナが作り直され、ローカルの
data/cache/以下は消えてしまう。SUPABASE_URL・SUPABASE_KEYが設定されている
場合は、同じデータをSupabase（PostgREST経由）にも保存し、ローカルキャッシュが
無い時はそちらから読み直してローカルにも書き戻す。どちらも未設定の場合は
従来通りローカルキャッシュのみで動作する（既存動作に変更なし）。

Supabase側に用意が必要なテーブル（README.md参照）:
    create table jquants_cache (
        endpoint text not null,
        cache_key text not null,
        data text not null,
        updated_at timestamptz not null default now(),
        primary key (endpoint, cache_key)
    );
"""
from __future__ import annotations

import base64
import io
import logging
import os

import pandas as pd
import requests

from src.config import CACHE_DIR

logger = logging.getLogger(__name__)

_EMPTY_MARKER_COLUMN = "__empty__"
_SUPABASE_TABLE = "jquants_cache"
_SUPABASE_TIMEOUT_SECONDS = 10


def _cache_path(endpoint: str, date: str) -> str:
    dir_path = os.path.join(CACHE_DIR, endpoint)
    os.makedirs(dir_path, exist_ok=True)
    return os.path.join(dir_path, f"{date}.parquet")


def _to_storable(df: pd.DataFrame) -> pd.DataFrame:
    # 土日祝日等でその日のレコードが0件だと列も無くなり、そのままではparquet化
    # できないので、空だったことを示すマーカー列を付けて保存する。
    return df if not df.empty else pd.DataFrame({_EMPTY_MARKER_COLUMN: [True]})


def _from_storable(df: pd.DataFrame) -> pd.DataFrame:
    if list(df.columns) == [_EMPTY_MARKER_COLUMN]:
        return pd.DataFrame()
    return df


def _local_load(path: str) -> pd.DataFrame | None:
    if not os.path.exists(path):
        return None
    return _from_storable(pd.read_parquet(path))


def _local_save(path: str, storable: pd.DataFrame) -> None:
    storable.to_parquet(path, index=False)


def _supabase_config() -> tuple[str, str] | None:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        return None
    return url.rstrip("/"), key


def _supabase_headers(key: str) -> dict[str, str]:
    return {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _encode(storable: pd.DataFrame) -> str:
    buf = io.BytesIO()
    storable.to_parquet(buf, index=False)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _decode(data: str) -> pd.DataFrame:
    return pd.read_parquet(io.BytesIO(base64.b64decode(data)))


def _supabase_load(endpoint: str, date: str) -> pd.DataFrame | None:
    config = _supabase_config()
    if config is None:
        return None
    url, key = config
    try:
        resp = requests.get(
            f"{url}/rest/v1/{_SUPABASE_TABLE}",
            headers=_supabase_headers(key),
            params={"endpoint": f"eq.{endpoint}", "cache_key": f"eq.{date}", "select": "data"},
            timeout=_SUPABASE_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return None
        return _from_storable(_decode(rows[0]["data"]))
    except Exception as exc:  # noqa: BLE001 -- Supabase障害時はローカルキャッシュのみで継続する意図的なフォールバック
        logger.warning("[%s/%s] Supabaseキャッシュの取得に失敗したためスキップします: %s", endpoint, date, exc)
        return None


def _supabase_save(endpoint: str, date: str, storable: pd.DataFrame) -> None:
    config = _supabase_config()
    if config is None:
        return
    url, key = config
    try:
        headers = _supabase_headers(key)
        headers["Prefer"] = "resolution=merge-duplicates"
        resp = requests.post(
            f"{url}/rest/v1/{_SUPABASE_TABLE}",
            headers=headers,
            json={"endpoint": endpoint, "cache_key": date, "data": _encode(storable)},
            timeout=_SUPABASE_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 -- Supabase障害時はローカルキャッシュのみで継続する意図的なフォールバック
        logger.warning("[%s/%s] Supabaseへのキャッシュ保存に失敗しました: %s", endpoint, date, exc)


def load(endpoint: str, date: str) -> pd.DataFrame | None:
    path = _cache_path(endpoint, date)
    local = _local_load(path)
    if local is not None:
        return local
    remote = _supabase_load(endpoint, date)
    if remote is not None:
        _local_save(path, _to_storable(remote))
    return remote


def save(endpoint: str, date: str, df: pd.DataFrame) -> None:
    storable = _to_storable(df)
    _local_save(_cache_path(endpoint, date), storable)
    _supabase_save(endpoint, date, storable)
