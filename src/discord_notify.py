"""Discord Webhookへの通知送信。

scripts/watch_and_notify.py専用。Webhook URLは秘密情報のため、環境変数
(DISCORD_WEBHOOK_URL)経由でのみ受け取り、ログ・例外メッセージに含めない。
"""
from __future__ import annotations

import time

import requests

# Discordの1メッセージ(content)あたりの文字数上限。
_DISCORD_CONTENT_LIMIT = 2000

# Discord Webhookのレート制限(429)に当たった場合の最大リトライ回数と、
# Retry-Afterが取得できなかった場合のフォールバック待機秒数。
_MAX_RATE_LIMIT_RETRIES = 5
_DEFAULT_RETRY_AFTER_SECONDS = 1.0


def _split_into_chunks(text: str, limit: int) -> list[str]:
    """textを、改行区切りを優先してlimit文字以内のチャンクに分割する。

    1行がlimitを超える場合はその行単独で強制的に区切る（呼び出し側の
    メッセージ生成では起こらない想定だが、防御的に対応する）。
    """
    lines = text.split("\n")
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = line
        while len(current) > limit:
            chunks.append(current[:limit])
            current = current[limit:]
    if current:
        chunks.append(current)
    return chunks


class DiscordNotifyError(RuntimeError):
    pass


def _retry_after_seconds(resp: requests.Response) -> float:
    """429応答から待機すべき秒数を取り出す。DiscordはJSON本文の
    retry_after（秒、小数あり）が最も正確なため優先し、無ければ標準の
    Retry-Afterヘッダ、それも無ければ既定値を使う。
    """
    try:
        body = resp.json()
        if isinstance(body, dict) and "retry_after" in body:
            return float(body["retry_after"])
    except (ValueError, TypeError):
        pass
    header = resp.headers.get("Retry-After")
    if header is not None:
        try:
            return float(header)
        except ValueError:
            pass
    return _DEFAULT_RETRY_AFTER_SECONDS


def _send_chunk(webhook_url: str, chunk: str) -> None:
    """1チャンクを送信する。429(レート制限)はDiscord側が想定内の応答として
    Retry-Afterを返す一時的な状態のため、それ自体を即座にエラー扱いに
    せず、示された秒数だけ待って再試行する（1件ずつ個別メッセージとして
    連続送信する設計上、件数が多い日にバーストして429になりうる。
    2026-08-27のCodexレビューで指摘・修正）。

    requests.post自体（DNS・接続・タイムアウト等）とraise_for_status
    （4xx/5xx）の両方が、例外メッセージにWebhook URL全体（トークン含む）を
    含みうる。呼び出し側のログ・トレースバックに秘密情報を残さないため、
    どちらもURLを含まないDiscordNotifyErrorに置き換える（2026-08-27の
    3巡目のCodexレビューで、raise_for_statusのHTTPErrorしか対処しておらず
    requests.post自体が送出する接続エラー等は素通りしていた不具合を
    指摘・修正）。
    """
    for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
        try:
            resp = requests.post(webhook_url, json={"content": chunk}, timeout=30)
            if resp.status_code == 429 and attempt < _MAX_RATE_LIMIT_RETRIES:
                time.sleep(_retry_after_seconds(resp))
                continue
            resp.raise_for_status()
            return
        except requests.exceptions.RequestException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            detail = f"（HTTP {status}）" if status is not None else f"（{type(e).__name__}）"
            raise DiscordNotifyError(f"Discord Webhookへの送信に失敗しました{detail}") from None
    raise DiscordNotifyError("Discord Webhookへの送信に失敗しました（レート制限が解消しませんでした）")


def send_discord_message(webhook_url: str, content: str) -> None:
    """Discord Webhookにメッセージを送信する。2000文字を超える場合は
    複数メッセージに分割して順に送信する。
    """
    for chunk in _split_into_chunks(content, _DISCORD_CONTENT_LIMIT):
        _send_chunk(webhook_url, chunk)
