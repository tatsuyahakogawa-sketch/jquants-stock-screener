"""日付単位のローカルキャッシュ。

J-Quants Free プランは呼び出し回数の制限が厳しいため、一度取得した
日付分のデータはparquetでローカル保存し、再実行時の再取得を避ける。
"""
from __future__ import annotations

import os
import pandas as pd

from src.config import CACHE_DIR


def _cache_path(endpoint: str, date: str) -> str:
    dir_path = os.path.join(CACHE_DIR, endpoint)
    os.makedirs(dir_path, exist_ok=True)
    return os.path.join(dir_path, f"{date}.parquet")


_EMPTY_MARKER_COLUMN = "__empty__"


def load(endpoint: str, date: str) -> pd.DataFrame | None:
    path = _cache_path(endpoint, date)
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path)
    if list(df.columns) == [_EMPTY_MARKER_COLUMN]:
        return pd.DataFrame()
    return df


def save(endpoint: str, date: str, df: pd.DataFrame) -> None:
    # 土日祝日等でその日のレコードが0件だと列も無くなり、そのままでは
    # parquet化できないので、空だったことを示すマーカー列を付けて保存する。
    to_write = df if not df.empty else pd.DataFrame({_EMPTY_MARKER_COLUMN: [True]})
    to_write.to_parquet(_cache_path(endpoint, date), index=False)
