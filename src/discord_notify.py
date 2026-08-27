"""Discord Webhookへの通知送信。

scripts/watch_and_notify.py専用。Webhook URLは秘密情報のため、環境変数
(DISCORD_WEBHOOK_URL)経由でのみ受け取り、ログ・例外メッセージに含めない。
"""
from __future__ import annotations

import requests

# Discordの1メッセージ(content)あたりの文字数上限。
_DISCORD_CONTENT_LIMIT = 2000


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


def send_discord_message(webhook_url: str, content: str) -> None:
    """Discord Webhookにメッセージを送信する。2000文字を超える場合は
    複数メッセージに分割して順に送信する。
    """
    for chunk in _split_into_chunks(content, _DISCORD_CONTENT_LIMIT):
        resp = requests.post(webhook_url, json={"content": chunk}, timeout=30)
        resp.raise_for_status()
