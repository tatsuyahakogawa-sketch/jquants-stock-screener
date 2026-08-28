"""src/discord_notify.py の単体テスト。

実行方法:
    python -m unittest discover tests
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import requests

from src.discord_notify import DiscordNotifyError, _MAX_RATE_LIMIT_RETRIES, send_discord_message

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
            f"401 Client Error: Unauthorized for url: {secret_url}", response=mock_resp
        )
        with patch(f"{_MOD}.requests.post", return_value=mock_resp):
            with self.assertRaises(DiscordNotifyError) as ctx:
                send_discord_message(secret_url, "hello")

        self.assertNotIn("super-secret-token", str(ctx.exception))
        self.assertIn("401", str(ctx.exception))

    def test_connection_error_does_not_leak_webhook_url(self):
        # requests.post自体が送出する接続エラー等（DNS失敗・タイムアウト等）も
        # 例外メッセージにURL全体を含むことがあるため、raise_for_statusの
        # HTTPErrorだけでなくこちらもサニタイズする（2026-08-27の3巡目の
        # Codexレビューで指摘・修正）。
        secret_url = "https://discord.com/api/webhooks/123/super-secret-token"
        with patch(
            f"{_MOD}.requests.post",
            side_effect=requests.exceptions.ConnectionError(f"Failed to connect to {secret_url}"),
        ):
            with self.assertRaises(DiscordNotifyError) as ctx:
                send_discord_message(secret_url, "hello")

        self.assertNotIn("super-secret-token", str(ctx.exception))

    def test_rate_limit_retries_then_succeeds(self):
        rate_limited_resp = MagicMock()
        rate_limited_resp.status_code = 429
        rate_limited_resp.json.return_value = {"retry_after": 0.0}
        ok_resp = MagicMock()
        ok_resp.status_code = 200

        with (
            patch(f"{_MOD}.requests.post", side_effect=[rate_limited_resp, ok_resp]) as mock_post,
            patch(f"{_MOD}.time.sleep") as mock_sleep,
        ):
            send_discord_message("https://example.com/webhook", "hello")

        self.assertEqual(mock_post.call_count, 2)
        mock_sleep.assert_called_once_with(0.0)
        ok_resp.raise_for_status.assert_called_once()

    def test_rate_limit_exhausts_retries_and_raises(self):
        rate_limited_resp = MagicMock()
        rate_limited_resp.status_code = 429
        rate_limited_resp.json.return_value = {"retry_after": 0.0}
        rate_limited_resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
            "429 Too Many Requests", response=rate_limited_resp
        )

        with (
            patch(f"{_MOD}.requests.post", return_value=rate_limited_resp) as mock_post,
            patch(f"{_MOD}.time.sleep"),
        ):
            with self.assertRaises(DiscordNotifyError):
                send_discord_message("https://example.com/webhook", "hello")

        self.assertEqual(mock_post.call_count, _MAX_RATE_LIMIT_RETRIES + 1)


if __name__ == "__main__":
    unittest.main()
