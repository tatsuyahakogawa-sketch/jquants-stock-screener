"""J-Quants API (V2) クライアント。

2026年1月のV2移行により、認証はメールアドレス/パスワードによる
リフレッシュトークン/IDトークン方式から、ダッシュボードで発行する
「APIキー」を `x-api-key` ヘッダーに載せる方式に変わった
(旧V1の `/token/auth_user` 等は410 Goneで廃止済み)。
参考: https://jpx-jquants.com/ja/spec/migration-v1-v2

レート制限・ページネーションはこのクラスでまとめて扱う。

注意: J-Quants のエンドポイントURL・フィールド名・料金プランは
サービス側の変更が入りうる。実装前提が古くなっている可能性があるため、
実際に動かす前に公式APIリファレンス
(https://jpx-jquants.com/ja/spec/data-spec) で最新仕様を必ず確認すること。
"""
from __future__ import annotations

import os
import time
import collections
from typing import Any, Iterator

import requests

from src.config import JQUANTS_API_CALLS_PER_MINUTE

BASE_URL = "https://api.jquants.com/v2"


class JQuantsAuthError(RuntimeError):
    pass


class RateLimiter:
    """直近1分間の呼び出し回数を Free プランの上限内に抑える簡易リミッター。"""

    def __init__(self, calls_per_minute: int):
        self.calls_per_minute = calls_per_minute
        self._call_times: collections.deque[float] = collections.deque()

    def wait(self) -> None:
        now = time.monotonic()
        while self._call_times and now - self._call_times[0] > 60:
            self._call_times.popleft()
        if len(self._call_times) >= self.calls_per_minute:
            sleep_for = 60 - (now - self._call_times[0]) + 0.1
            if sleep_for > 0:
                time.sleep(sleep_for)
        self._call_times.append(time.monotonic())


class JQuantsClient:
    def __init__(
        self,
        api_key: str | None = None,
        calls_per_minute: int = JQUANTS_API_CALLS_PER_MINUTE,
    ):
        self._api_key = api_key or os.environ.get("JQUANTS_API_KEY")
        if not self._api_key:
            raise JQuantsAuthError(
                "JQUANTS_API_KEY が設定されていません。J-Quantsダッシュボードの"
                "「設定 > APIキー」から発行したキーを .env に設定してください。"
            )
        self._limiter = RateLimiter(calls_per_minute)
        self._session = requests.Session()

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """1回分のGET。ページネーションは呼び出し側 or get_all_pages で処理する。

        レート制限はプロセス内のRateLimiterで自主的に守っているが、直前に別の
        プロセスから呼んだ直後などはサーバー側の1分間ウィンドウと食い違い、
        429が返ることがある。その場合は少し待って再試行する。
        """
        params = dict(params or {})
        headers = {"x-api-key": self._api_key}
        max_retries = 3
        for attempt in range(max_retries):
            self._limiter.wait()
            resp = self._session.get(f"{BASE_URL}{path}", params=params, headers=headers, timeout=30)
            if resp.status_code == 429 and attempt < max_retries - 1:
                # 上限は1分単位のため、次のウィンドウに確実に入るまで待つ
                time.sleep(65)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("unreachable")

    def get_all_pages(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        """pagination_key を使って全ページを辿り、"data" 配列の各レコードを1件ずつ返す。"""
        params = dict(params or {})
        while True:
            body = self.get(path, params)
            for record in body.get("data", []):
                yield record
            pagination_key = body.get("pagination_key")
            if not pagination_key:
                break
            params["pagination_key"] = pagination_key
