"""src/tdnet_xbrl.py の単体テスト。

決算短信サマリー情報XBRL(ixbrl.htm)の最小限の合成フィクスチャを使い、
scale/sign属性の扱い・四半期/本決算の判定・前年同期行の抽出・予想値の
期ズレ回避を検証する。実際のTDnetへのネットワークアクセスは行わない。

実行方法:
    python -m unittest discover tests
"""
from __future__ import annotations

import datetime as dt
import io
import sys
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import tdnet_xbrl

_NS = (
    'xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" '
    'xmlns:xbrli="http://www.xbrl.org/2003/instance" '
    'xmlns:tse-ed-t="http://www.xbrl.tdnet.info/jp/tse/ed/t"'
)


def _nonfraction(name: str, context: str, value: str, *, scale: str | None = None, sign: str | None = None) -> str:
    attrs = f'name="{name}" contextRef="{context}" unitRef="U"'
    if scale is not None:
        attrs += f' scale="{scale}"'
    if sign is not None:
        attrs += f' sign="{sign}"'
    return f"<ix:nonFraction {attrs}>{value}</ix:nonFraction>"


def _nonnumeric(name: str, context: str, value: str) -> str:
    return f'<ix:nonNumeric name="{name}" contextRef="{context}">{value}</ix:nonNumeric>'


def _context(cid: str, *, start: str | None = None, end: str | None = None, instant: str | None = None) -> str:
    if instant is not None:
        period = f"<xbrli:instant>{instant}</xbrli:instant>"
    else:
        period = f"<xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>"
    return f'<xbrli:context id="{cid}"><xbrli:period>{period}</xbrli:period></xbrli:context>'


def _make_zip(body_fragments: list[str]) -> bytes:
    html = f"<html {_NS}><body>{''.join(body_fragments)}</body></html>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("XBRLData/Summary/tse-xxedjpsm-00000-x-ixbrl.htm", html)
    return buf.getvalue()


def _q1_fixture() -> bytes:
    fragments = [
        _nonnumeric("tse-ed-t:FiscalYearEnd", "CurrentYearInstant", "2027-03-31"),
        _nonfraction("tse-ed-t:QuarterlyPeriod", "CurrentAccumulatedQ1Instant", "1"),
        _nonfraction(
            "tse-ed-t:NetSales", "CurrentAccumulatedQ1Duration_ConsolidatedMember_ResultMember", "378", scale="6"
        ),
        _nonfraction(
            "tse-ed-t:OperatingIncome", "CurrentAccumulatedQ1Duration_ConsolidatedMember_ResultMember", "16",
            scale="6",
        ),
        _nonfraction(
            "tse-ed-t:OrdinaryIncome", "CurrentAccumulatedQ1Duration_ConsolidatedMember_ResultMember", "14",
            scale="6",
        ),
        _nonfraction(
            "tse-ed-t:ProfitAttributableToOwnersOfParent",
            "CurrentAccumulatedQ1Duration_ConsolidatedMember_ResultMember", "216", scale="6",
        ),
        _nonfraction(
            "tse-ed-t:CapitalAdequacyRatio", "CurrentAccumulatedQ1Instant_ConsolidatedMember_ResultMember", "34.0",
            scale="-2",
        ),
        # 前年同期(赤字だったケースを再現。sign="-"で赤字)
        _nonfraction(
            "tse-ed-t:NetSales", "PriorAccumulatedQ1Duration_ConsolidatedMember_ResultMember", "447", scale="6"
        ),
        _nonfraction(
            "tse-ed-t:OperatingIncome", "PriorAccumulatedQ1Duration_ConsolidatedMember_ResultMember", "92",
            scale="6", sign="-",
        ),
        _nonfraction(
            "tse-ed-t:OrdinaryIncome", "PriorAccumulatedQ1Duration_ConsolidatedMember_ResultMember", "95",
            scale="6", sign="-",
        ),
        _nonfraction(
            "tse-ed-t:ProfitAttributableToOwnersOfParent",
            "PriorAccumulatedQ1Duration_ConsolidatedMember_ResultMember", "72", scale="6", sign="-",
        ),
        # 進行中の当期予想(CurFYEnと同じ期)
        _nonfraction(
            "tse-ed-t:NetSales", "CurrentYearDuration_ConsolidatedMember_ForecastMember", "1484", scale="6"
        ),
        _nonfraction(
            "tse-ed-t:OperatingIncome", "CurrentYearDuration_ConsolidatedMember_ForecastMember", "10", scale="6"
        ),
        _nonfraction(
            "tse-ed-t:ProfitAttributableToOwnersOfParent",
            "CurrentYearDuration_ConsolidatedMember_ForecastMember", "149", scale="6",
        ),
        # 貸借対照表側の「前期末」は前年同四半期末ではなく直前本決算末を指すため、
        # 前年同期行では意図的に読まない対象（テスト側にも同名タグを置いて
        # 誤って拾わないことを検証する）
        _nonfraction(
            "tse-ed-t:CapitalAdequacyRatio", "PriorYearInstant_ConsolidatedMember_ResultMember", "14.4", scale="-2"
        ),
        _context("CurrentYearInstant", instant="2027-03-31"),
        _context("CurrentAccumulatedQ1Instant", instant="2026-06-30"),
        _context(
            "CurrentAccumulatedQ1Duration_ConsolidatedMember_ResultMember",
            start="2026-04-01", end="2026-06-30",
        ),
        _context(
            "PriorAccumulatedQ1Duration_ConsolidatedMember_ResultMember",
            start="2025-04-01", end="2025-06-30",
        ),
        _context("CurrentAccumulatedQ1Instant_ConsolidatedMember_ResultMember", instant="2026-06-30"),
        _context("PriorYearInstant_ConsolidatedMember_ResultMember", instant="2026-03-31"),
    ]
    return _make_zip(fragments)


def _fy_fixture(*, include_guidance: bool = True) -> bytes:
    fragments = [
        _nonnumeric("tse-ed-t:FiscalYearEnd", "CurrentYearInstant", "2026-06-30"),
        _nonfraction(
            "tse-ed-t:NetSales", "CurrentYearDuration_ConsolidatedMember_ResultMember", "7035", scale="6"
        ),
        _nonfraction(
            "tse-ed-t:OperatingIncome", "CurrentYearDuration_ConsolidatedMember_ResultMember", "595", scale="6"
        ),
        _nonfraction(
            "tse-ed-t:OrdinaryIncome", "CurrentYearDuration_ConsolidatedMember_ResultMember", "746", scale="6"
        ),
        _nonfraction(
            "tse-ed-t:ProfitAttributableToOwnersOfParent",
            "CurrentYearDuration_ConsolidatedMember_ResultMember", "491", scale="6",
        ),
        _nonfraction(
            "tse-ed-t:CapitalAdequacyRatio", "CurrentYearInstant_ConsolidatedMember_ResultMember", "68.1",
            scale="-2",
        ),
        _nonfraction(
            "tse-ed-t:NetAssetsPerShare", "CurrentYearInstant_ConsolidatedMember_ResultMember", "9936.46",
        ),
        # 前年同期(本決算なのでPriorYearInstantは文字通り1年前の期末を指す)
        _nonfraction(
            "tse-ed-t:NetSales", "PriorYearDuration_ConsolidatedMember_ResultMember", "7841", scale="6"
        ),
        _nonfraction(
            "tse-ed-t:CapitalAdequacyRatio", "PriorYearInstant_ConsolidatedMember_ResultMember", "62.1",
            scale="-2",
        ),
        _context("CurrentYearInstant", instant="2026-06-30"),
        _context(
            "CurrentYearDuration_ConsolidatedMember_ResultMember", start="2025-07-01", end="2026-06-30",
        ),
        _context("CurrentYearInstant_ConsolidatedMember_ResultMember", instant="2026-06-30"),
        _context(
            "PriorYearDuration_ConsolidatedMember_ResultMember", start="2024-07-01", end="2025-06-30",
        ),
        _context("PriorYearInstant_ConsolidatedMember_ResultMember", instant="2025-06-30"),
    ]
    if include_guidance:
        # 来期予想(当期行のCurFYEnとは別の期を指すため当期行には含まれないが、
        # 翌期予想専用の合成行(guidance_row)としては抽出されるはず)
        fragments += [
            _nonfraction(
                "tse-ed-t:NetSales", "NextYearDuration_ConsolidatedMember_ForecastMember", "6800", scale="6"
            ),
            _nonfraction(
                "tse-ed-t:OperatingIncome", "NextYearDuration_ConsolidatedMember_ForecastMember", "550", scale="6"
            ),
            _nonfraction(
                "tse-ed-t:ProfitAttributableToOwnersOfParent",
                "NextYearDuration_ConsolidatedMember_ForecastMember", "450", scale="6",
            ),
        ]
    return _make_zip(fragments)


def _fy_fixture_without_guidance() -> bytes:
    return _fy_fixture(include_guidance=False)


class TestParseTanshinSummaryRowsQuarterly(unittest.TestCase):
    def setUp(self):
        self.rows = tdnet_xbrl.parse_tanshin_summary_rows(_q1_fixture(), "33460", dt.date(2026, 8, 13))

    def test_returns_current_and_prior_rows(self):
        self.assertEqual(len(self.rows), 2)

    def test_current_row_period_type_and_dates(self):
        cur = self.rows[0]
        self.assertEqual(cur["CurPerType"], "1Q")
        self.assertEqual(cur["CurFYEn"].date(), dt.date(2027, 3, 31))
        self.assertEqual(cur["CurPerEn"].date(), dt.date(2026, 6, 30))

    def test_current_row_sales_converted_to_yen_via_scale(self):
        self.assertEqual(self.rows[0]["Sales"], 378_000_000.0)

    def test_current_row_equity_ratio_converted_via_scale(self):
        self.assertAlmostEqual(self.rows[0]["EqAR"], 0.34)

    def test_current_row_forecast_present(self):
        cur = self.rows[0]
        self.assertEqual(cur["FSales"], 1_484_000_000.0)
        self.assertEqual(cur["FOP"], 10_000_000.0)
        self.assertEqual(cur["FNP"], 149_000_000.0)

    def test_current_row_bps_absent_is_none_not_fabricated(self):
        self.assertIsNone(self.rows[0]["BPS"])

    def test_current_and_prior_rows_are_primary_and_synthetic_respectively(self):
        self.assertTrue(self.rows[0]["IsPrimary"])
        self.assertFalse(self.rows[1]["IsPrimary"])

    def test_prior_row_sign_attribute_applied_as_loss(self):
        prior = self.rows[1]
        self.assertEqual(prior["OP"], -92_000_000.0)
        self.assertEqual(prior["OdP"], -95_000_000.0)
        self.assertEqual(prior["NP"], -72_000_000.0)

    def test_prior_row_period_type_and_fy_end_one_year_back(self):
        prior = self.rows[1]
        self.assertEqual(prior["CurPerType"], "1Q")
        self.assertEqual(prior["CurFYEn"].date(), dt.date(2026, 3, 31))
        self.assertEqual(prior["CurPerEn"].date(), dt.date(2025, 6, 30))

    def test_prior_row_has_no_forecast(self):
        prior = self.rows[1]
        self.assertIsNone(prior["FSales"])
        self.assertIsNone(prior["FOP"])
        self.assertIsNone(prior["FNP"])

    def test_prior_row_quarterly_excludes_balance_sheet_figures(self):
        # PriorYearInstantは「前年同四半期末」ではなく「直前本決算末」を指すため、
        # 四半期報告の前年同期行ではEqAR/BPSを取得しない。
        prior = self.rows[1]
        self.assertIsNone(prior["EqAR"])
        self.assertIsNone(prior["BPS"])


class TestParseTanshinSummaryRowsFiscalYear(unittest.TestCase):
    def setUp(self):
        self.rows = tdnet_xbrl.parse_tanshin_summary_rows(_fy_fixture(), "19990", dt.date(2026, 8, 18))

    def test_returns_current_prior_and_guidance_rows(self):
        # 当期行・前年同期行に加え、来期予想専用の合成行(guidance_row)の3行。
        self.assertEqual(len(self.rows), 3)

    def test_current_row_period_type_is_fy(self):
        self.assertEqual(self.rows[0]["CurPerType"], "FY")

    def test_current_row_sales_and_bps(self):
        cur = self.rows[0]
        self.assertEqual(cur["Sales"], 7_035_000_000.0)
        self.assertAlmostEqual(cur["BPS"], 9936.46)

    def test_current_row_forecast_excluded_for_fy_report(self):
        # 本決算行の予想値は来期(NextYearDuration)を指し、CurFYEnとは別の期に
        # なってしまうため、意図的にFSales等はNoneのままにする。
        cur = self.rows[0]
        self.assertIsNone(cur["FSales"])
        self.assertIsNone(cur["FOP"])
        self.assertIsNone(cur["FNP"])

    def test_current_and_prior_rows_are_primary_and_synthetic_respectively(self):
        self.assertTrue(self.rows[0]["IsPrimary"])
        self.assertFalse(self.rows[1]["IsPrimary"])

    def test_prior_row_fy_includes_balance_sheet_figures(self):
        # 本決算の前年同期行は文字通り1年前の期末を指すため、EqAR等を含めてよい。
        prior = self.rows[1]
        self.assertEqual(prior["Sales"], 7_841_000_000.0)
        self.assertAlmostEqual(prior["EqAR"], 0.621)
        self.assertEqual(prior["CurFYEn"].date(), dt.date(2025, 6, 30))

    def test_guidance_row_captures_next_year_forecast(self):
        # 来期予想は当期行(CurFYEn=2026-06-30)には含まれず、CurFYEnを
        # 翌期(2027-06-30)に設定した予想専用行として抽出される
        # （rules.detect_downward_revision等の下方修正検知の基準値になる）。
        guidance = self.rows[2]
        self.assertFalse(guidance["IsPrimary"])
        self.assertEqual(guidance["CurFYEn"].date(), dt.date(2027, 6, 30))
        self.assertEqual(guidance["FSales"], 6_800_000_000.0)
        self.assertEqual(guidance["FOP"], 550_000_000.0)
        self.assertEqual(guidance["FNP"], 450_000_000.0)
        self.assertIsNone(guidance["Sales"])
        self.assertIsNone(guidance["NP"])

    def test_guidance_row_absent_when_no_next_year_forecast_present(self):
        # 来期予想のタグが全く無い決算短信では、guidance_rowを追加しない。
        rows = tdnet_xbrl.parse_tanshin_summary_rows(_fy_fixture_without_guidance(), "19990", dt.date(2026, 8, 18))
        self.assertEqual(len(rows), 2)


class TestParseTanshinSummaryRowsGracefulFailure(unittest.TestCase):
    def test_bad_zip_returns_empty_list(self):
        self.assertEqual(tdnet_xbrl.parse_tanshin_summary_rows(b"not a zip", "00000", dt.date(2026, 1, 1)), [])

    def test_zip_without_summary_returns_empty_list(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("XBRLData/Attachment/other.xml", "<x/>")
        self.assertEqual(
            tdnet_xbrl.parse_tanshin_summary_rows(buf.getvalue(), "00000", dt.date(2026, 1, 1)), []
        )

    def test_missing_net_sales_returns_empty_list(self):
        fragments = [_nonnumeric("tse-ed-t:FiscalYearEnd", "CurrentYearInstant", "2027-03-31")]
        self.assertEqual(
            tdnet_xbrl.parse_tanshin_summary_rows(_make_zip(fragments), "00000", dt.date(2026, 1, 1)), []
        )


class TestDownloadRetry(unittest.TestCase):
    """一時的なネットワーク障害（タイムアウト・接続エラー・5xx）の再試行と、
    恒久的なエラー（404等のTDnet保持期限切れ）を即座に諦める挙動を検証する
    （2026-08-19のCodexレビューで指摘: 再試行が無いと、たまたま一時的な障害に
    当たった決算短信の財務データが永久に欠落していた）。
    """

    def test_retries_on_connection_error_then_succeeds(self):
        ok_resp = MagicMock()
        ok_resp.content = b"data"
        ok_resp.raise_for_status = MagicMock()
        with patch(
            "src.tdnet_xbrl.requests.get",
            side_effect=[requests.exceptions.ConnectionError("boom"), ok_resp],
        ) as mock_get, patch("src.tdnet_xbrl.time.sleep"):
            result = tdnet_xbrl._download("https://example.com/x.zip")
        self.assertEqual(result, b"data")
        self.assertEqual(mock_get.call_count, 2)

    def test_gives_up_after_max_attempts(self):
        with (
            patch("src.tdnet_xbrl.requests.get", side_effect=requests.exceptions.ConnectionError("boom")) as mock_get,
            patch("src.tdnet_xbrl.time.sleep"),
            self.assertRaises(requests.exceptions.ConnectionError),
        ):
            tdnet_xbrl._download("https://example.com/x.zip")
        self.assertEqual(mock_get.call_count, tdnet_xbrl._MAX_ATTEMPTS)

    def test_404_is_not_retried(self):
        resp = MagicMock()
        resp.status_code = 404
        bad_resp = MagicMock()
        bad_resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
        with (
            patch("src.tdnet_xbrl.requests.get", return_value=bad_resp) as mock_get,
            patch("src.tdnet_xbrl.time.sleep") as mock_sleep,
            self.assertRaises(requests.exceptions.HTTPError),
        ):
            tdnet_xbrl._download("https://example.com/x.zip")
        self.assertEqual(mock_get.call_count, 1)
        mock_sleep.assert_not_called()

    def test_500_is_retried(self):
        resp = MagicMock()
        resp.status_code = 500
        bad_resp = MagicMock()
        bad_resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
        with (
            patch("src.tdnet_xbrl.requests.get", return_value=bad_resp) as mock_get,
            patch("src.tdnet_xbrl.time.sleep"),
            self.assertRaises(requests.exceptions.HTTPError),
        ):
            tdnet_xbrl._download("https://example.com/x.zip")
        self.assertEqual(mock_get.call_count, tdnet_xbrl._MAX_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
