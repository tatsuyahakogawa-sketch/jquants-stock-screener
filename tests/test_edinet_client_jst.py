"""src/edinet_client.py の当日分キャッシュ非保存(JST対応)の回帰テスト。

today_jst()導入によりJST 0:00〜8:59の間も正しく「当日」がEDINET検索対象に
含まれるようになったが、当日分はまだ提出が続いている途中の可能性がある
ため、キャッシュに保存してしまうとその日の後で提出された書類を翌日まで
拾えなくなる。_get_documents_for_date()が当日分だけキャッシュの読み書き
両方をスキップすることを確認する（EDINETへの実通信は行わない）。

実行方法:
    python -m unittest discover tests
"""
from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import edinet_client

_MOD = "src.edinet_client"


def _mock_response(results):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"results": results}
    return resp


class TestGetDocumentsForDateTodayCaching(unittest.TestCase):
    def test_today_is_not_read_from_or_written_to_cache(self):
        fixed_today = dt.date(2026, 8, 19)
        with patch(f"{_MOD}.today_jst", return_value=fixed_today), \
                patch(f"{_MOD}.requests.get", return_value=_mock_response([{"docID": "X"}])), \
                patch(f"{_MOD}.cache.load") as mock_load, \
                patch(f"{_MOD}.cache.save") as mock_save:
            result = edinet_client._get_documents_for_date(fixed_today, "dummy-key")

        mock_load.assert_not_called()
        mock_save.assert_not_called()
        self.assertEqual(result, [{"docID": "X"}])

    def test_past_date_still_uses_cache(self):
        fixed_today = dt.date(2026, 8, 19)
        past_date = dt.date(2026, 8, 10)
        with patch(f"{_MOD}.today_jst", return_value=fixed_today), \
                patch(f"{_MOD}.requests.get", return_value=_mock_response([{"docID": "Y"}])), \
                patch(f"{_MOD}.cache.load", return_value=None) as mock_load, \
                patch(f"{_MOD}.cache.save") as mock_save:
            edinet_client._get_documents_for_date(past_date, "dummy-key")

        mock_load.assert_called_once()
        mock_save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
