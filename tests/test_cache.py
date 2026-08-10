"""src/cache.py の単体テスト（ローカルキャッシュ + Supabase永続化）。

Supabase呼び出しはunittest.mock.patchでrequestsを差し替えてオフラインで
実行する。実際のネットワークには繋がない。

実行方法:
    python -m unittest discover tests
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import cache

_MOD = "src.cache"


class TestLocalCacheOnly(unittest.TestCase):
    """SUPABASE_URL/KEY未設定時は従来通りローカルのみで動作すること。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patcher = patch(f"{_MOD}.CACHE_DIR", self._tmpdir.name)
        self._patcher.start()
        self._env_patcher = patch.dict("os.environ", {}, clear=False)
        self._env_patcher.start()
        for key in ("SUPABASE_URL", "SUPABASE_KEY"):
            __import__("os").environ.pop(key, None)

    def tearDown(self):
        self._env_patcher.stop()
        self._patcher.stop()
        self._tmpdir.cleanup()

    def test_round_trip_non_empty_dataframe(self):
        df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
        cache.save("statements", "20260101", df)
        loaded = cache.load("statements", "20260101")
        pd.testing.assert_frame_equal(loaded, df)

    def test_round_trip_empty_dataframe(self):
        cache.save("statements", "20260102", pd.DataFrame())
        loaded = cache.load("statements", "20260102")
        self.assertTrue(loaded.empty)

    def test_missing_key_returns_none(self):
        self.assertIsNone(cache.load("statements", "99999999"))

    @patch(f"{_MOD}.requests.get")
    def test_no_supabase_request_when_unconfigured(self, mock_get):
        cache.load("statements", "99999999")
        mock_get.assert_not_called()

    @patch(f"{_MOD}.requests.post")
    def test_no_supabase_save_when_unconfigured(self, mock_post):
        cache.save("statements", "20260103", pd.DataFrame({"a": [1]}))
        mock_post.assert_not_called()


class TestSupabaseFallback(unittest.TestCase):
    """SUPABASE_URL/KEY設定時、ローカルミス時にSupabaseから読み直すこと。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patcher = patch(f"{_MOD}.CACHE_DIR", self._tmpdir.name)
        self._patcher.start()
        self._env_patcher = patch.dict(
            "os.environ", {"SUPABASE_URL": "https://example.supabase.co", "SUPABASE_KEY": "key123"}
        )
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()
        self._patcher.stop()
        self._tmpdir.cleanup()

    @patch(f"{_MOD}.requests.get")
    def test_local_miss_falls_back_to_supabase_and_writes_local(self, mock_get):
        df = pd.DataFrame({"a": [1, 2]})
        mock_resp = MagicMock()
        mock_resp.json.return_value = [{"data": cache._encode(cache._to_storable(df))}]
        mock_get.return_value = mock_resp

        loaded = cache.load("statements", "20260104")
        pd.testing.assert_frame_equal(loaded, df)
        mock_get.assert_called_once()

        # ローカルにも書き戻され、2回目はSupabaseを呼ばない
        mock_get.reset_mock()
        loaded_again = cache.load("statements", "20260104")
        pd.testing.assert_frame_equal(loaded_again, df)
        mock_get.assert_not_called()

    @patch(f"{_MOD}.requests.get")
    def test_supabase_empty_result_returns_none(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_get.return_value = mock_resp
        self.assertIsNone(cache.load("statements", "20260105"))

    @patch(f"{_MOD}.requests.get", side_effect=ConnectionError("network down"))
    def test_supabase_network_failure_degrades_to_none(self, mock_get):
        self.assertIsNone(cache.load("statements", "20260106"))

    @patch(f"{_MOD}.requests.post")
    def test_save_also_posts_to_supabase(self, mock_post):
        mock_post.return_value = MagicMock(status_code=201)
        cache.save("statements", "20260107", pd.DataFrame({"a": [1]}))
        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["json"]["endpoint"], "statements")
        self.assertEqual(kwargs["json"]["cache_key"], "20260107")

    @patch(f"{_MOD}.requests.post", side_effect=ConnectionError("network down"))
    def test_save_network_failure_does_not_raise(self, mock_post):
        # Supabaseへの保存が失敗してもローカル保存・呼び出し全体は成功すること
        cache.save("statements", "20260108", pd.DataFrame({"a": [1]}))
        loaded = cache.load("statements", "20260108")
        self.assertEqual(loaded["a"].tolist(), [1])


if __name__ == "__main__":
    unittest.main()
