"""src/discord_notify.py の単体テスト。

実行方法:
    python -m unittest discover tests
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import requests

from src.discord_notify import DiscordNotifyError, send_discord_message

_MOD = "src.discord_notify"


class TestSendDiscordMessage(unittest.TestCase):
    def test_short_message_sent_in_a_single_request(self):
        mock_resp = MagicMock()
        with patch(f"{_MOD}.requests.post", return_value=mock_resp) as mock_post:
            send_discord_message("https://example.com/webhook", "hello")

        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://example.com/webhook")
        self.assertEqual(kwargs["json"], {"content": "hello"})
        mock_resp.raise_for_status.assert_called_once()

    def test_long_message_is_split_into_multiple_requests(self):
        # Discordの1メッセージあたりの上限(2000文字)を超える内容は、
        # 複数メッセージに分割して送信する。
        long_content = "\n".join(f"line{i}" * 50 for i in range(100))
        self.assertGreater(len(long_content), 2000)

        mock_resp = MagicMock()
        with patch(f"{_MOD}.requests.post", return_value=mock_resp) as mock_post:
            send_discord_message("https://example.com/webhook", long_content)

        self.assertGreater(mock_post.call_count, 1)
        for _, kwargs in mock_post.call_args_list:
            self.assertLessEqual(len(kwargs["json"]["content"]), 2000)

    def test_http_error_propagates(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = RuntimeError("boom")
        with patch(f"{_MOD}.requests.post", return_value=mock_resp):
            with self.assertRaises(RuntimeError):
                send_discord_message("https://example.com/webhook", "hello")

    def test_http_error_does_not_leak_webhook_url(self):
        # requests.HTTPErrorの既定メッセージにはWebhook URL（秘密のトークンを
        # 含む）がそのまま入るため、呼び出し側に伝播する例外からは除去する
        # （2026-08-27のCodexレビューで指摘・修正）。
        secret_url = "https://discord.com/api/webhooks/123/super-secret-token"
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
            f"401 Client Error: Unauthorized for url: {secret_url}"
        )
        with patch(f"{_MOD}.requests.post", return_value=mock_resp):
            with self.assertRaises(DiscordNotifyError) as ctx:
                send_discord_message(secret_url, "hello")

        self.assertNotIn("super-secret-token", str(ctx.exception))
        self.assertIn("401", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
