"""src/tdnet_client.py の単体テスト（force_refresh引数のみ）。

既存のTDnetミラー呼び出し自体は元からテストが無かったため、今回追加した
force_refreshの挙動だけを対象にする。実際のネットワークには繋がない。

実行方法:
    python -m unittest discover tests
"""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import tdnet_client


class TestForceRefresh(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._cache_dir_patcher = patch("src.cache.CACHE_DIR", self._tmpdir.name)
        self._cache_dir_patcher.start()
        self._supabase_patcher = patch("src.cache._supabase_config", return_value=None)
        self._supabase_patcher.start()

    def tearDown(self):
        self._supabase_patcher.stop()
        self._cache_dir_patcher.stop()
        self._tmpdir.cleanup()

    def _mock_response(self, items):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"items": [{"Tdnet": item} for item in items]}
        return resp

    def test_default_uses_cache_on_second_call(self):
        item = {"id": "1", "company_code": "12340", "pubdate": "2026-08-13 10:00:00"}
        with patch("src.tdnet_client.requests.get", return_value=self._mock_response([item])) as mock_get:
            tdnet_client.get_disclosures_range(dt.date(2026, 8, 13), dt.date(2026, 8, 13))
            tdnet_client.get_disclosures_range(dt.date(2026, 8, 13), dt.date(2026, 8, 13))
        self.assertEqual(mock_get.call_count, 1)

    def test_force_refresh_bypasses_cache_every_time(self):
        item = {"id": "1", "company_code": "12340", "pubdate": "2026-08-13 10:00:00"}
        with patch("src.tdnet_client.requests.get", return_value=self._mock_response([item])) as mock_get:
            tdnet_client.get_disclosures_range(dt.date(2026, 8, 13), dt.date(2026, 8, 13), force_refresh=True)
            tdnet_client.get_disclosures_range(dt.date(2026, 8, 13), dt.date(2026, 8, 13), force_refresh=True)
        self.assertEqual(mock_get.call_count, 2)

    def test_force_refresh_does_not_write_to_cache(self):
        # force_refreshで取得した結果はキャッシュに保存しない。「まだ全件
        # 公開されていない可能性がある」と分かって敢えて取りに来た結果を
        # キャッシュしてしまうと、翌日以降の通常(非force_refresh)取得が
        # その不完全な結果を返し続けてしまうため。
        item = {"id": "1", "company_code": "12340", "pubdate": "2026-08-13 10:00:00"}
        with patch("src.tdnet_client.requests.get", return_value=self._mock_response([item])) as mock_get:
            tdnet_client.get_disclosures_range(dt.date(2026, 8, 13), dt.date(2026, 8, 13), force_refresh=True)
            tdnet_client.get_disclosures_range(dt.date(2026, 8, 13), dt.date(2026, 8, 13))
        self.assertEqual(mock_get.call_count, 2)

    def test_default_result_matches_force_refresh_result(self):
        item = {"id": "1", "company_code": "12340", "pubdate": "2026-08-13 10:00:00"}
        with patch("src.tdnet_client.requests.get", return_value=self._mock_response([item])):
            result = tdnet_client.get_disclosures_range(dt.date(2026, 8, 13), dt.date(2026, 8, 13), force_refresh=True)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["company_code"], "12340")


if __name__ == "__main__":
    unittest.main()
