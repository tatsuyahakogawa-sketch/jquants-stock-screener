"""銘柄コードから企業研究データ(dataclass)を組み立てるモジュール。

excel_export.pyの企業詳細Excelが使っているのと同じ情報源（J-Quantsの財務・
株価、EDINET有報テキスト）を、Excelへの書き込みとは分離した構造化データ
として返す。値の取得元は既存モジュール（endpoints/pipeline/edinet_client/
excel_export）をそのまま再利用し、既存の実装・出力は変更しない。
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import pandas as pd

from src import edinet_client, endpoints, excel_export, pipeline
from src.jquants_client import JQuantsClient

logger = logging.getLogger(__name__)


class CompanyResearchError(Exception):
    """企業研究データの取得に失敗し、続行できない場合。"""


@dataclass
class CompanyResearch:
    """単位: market_cap/latest_closeは円（生値）、shares_outは株（生値）、
    per/pbrは倍（比率）、dividend_yieldは小数（0.03=3%）。
    億円・万株等への換算は呼び出し側の責務とする。
    """

    code: str
    company_name: str | None
    market_name: str | None
    fiscal_year_end_month: int | None
    market_cap: float | None
    per: float | None
    pbr: float | None
    dividend_yield: float | None
    latest_close: float | None
    shares_out: float | None
    listing_date: dt.date | None
    business_overview: str | None
    shareholders_text: str | None
    potential_shares_text: str | None
    stock_split_events_text: str | None


def build_company_research(client: JQuantsClient, code: str) -> CompanyResearch:
    """銘柄コードから企業研究データを組み立てる。

    銘柄マスタ・決算・株価が取得できない場合はCompanyResearchErrorを送出する。
    EDINET有報等の付随情報が取得できない場合は、その項目だけNoneにして続行する。
    """
    code = str(code)
    master_row = _fetch_master_row(client, code)
    fins = _fetch_financials(client, code)
    _ensure_code_exists(code, master_row, fins)
    excel_export._ensure_common_stock(master_row, code)

    prices = _fetch_price_history(client, code)
    metrics = _compute_metrics(code, fins, prices)
    fiscal_year_end = _fetch_fiscal_year_end(code, fins)
    yuho = edinet_client.fetch_yuho_texts(code, fiscal_year_end)

    return CompanyResearch(
        code=code,
        company_name=master_row.get("CoName"),
        market_name=master_row.get("MktNm"),
        fiscal_year_end_month=fiscal_year_end.month if fiscal_year_end else None,
        market_cap=metrics.get("market_cap"),
        per=metrics.get("per"),
        pbr=metrics.get("pbr"),
        dividend_yield=metrics.get("dividend_yield"),
        latest_close=metrics.get("latest_close"),
        shares_out=metrics.get("shares_out"),
        listing_date=_fetch_listing_date(code, prices),
        business_overview=yuho.get("business_overview"),
        shareholders_text=_format_shareholders_text(code, yuho.get("shareholders")),
        potential_shares_text=yuho.get("potential_shares"),
        stock_split_events_text=_fetch_split_events_text(code, prices),
    )


def _ensure_code_exists(code: str, master_row: dict, fins: pd.DataFrame) -> None:
    """銘柄マスタ・決算情報のいずれにも存在しない場合はエラーにする。

    地方取引所への移籍等でmaster_rowが空でも決算開示(fins)は残ることがある
    （例: 2026-08時点の9388）ため、両方が空の場合だけ「存在しない銘柄コード」
    と判断する。
    """
    if not master_row and fins.empty:
        raise CompanyResearchError(
            f"銘柄コード{code}が見つかりません"
            "（J-Quantsの銘柄マスタ・決算情報のいずれにも存在しません）"
        )


def _fetch_master_row(client: JQuantsClient, code: str) -> dict:
    try:
        return excel_export._get_master_row(client, code)
    except Exception as exc:
        logger.error("[%s] 上場銘柄マスタの取得に失敗しました: %s", code, exc)
        raise CompanyResearchError(f"銘柄コード{code}のマスタ情報取得に失敗しました") from exc


def _fetch_financials(client: JQuantsClient, code: str) -> pd.DataFrame:
    try:
        return endpoints.get_financials_by_code(client, code)
    except Exception as exc:
        logger.error("[%s] 決算情報の取得に失敗しました: %s", code, exc)
        raise CompanyResearchError(f"銘柄コード{code}の決算情報取得に失敗しました") from exc


def _fetch_price_history(client: JQuantsClient, code: str) -> pd.DataFrame:
    try:
        return endpoints.get_price_history_by_code(client, code)
    except Exception as exc:
        logger.error("[%s] 株価履歴の取得に失敗しました: %s", code, exc)
        raise CompanyResearchError(f"銘柄コード{code}の株価履歴取得に失敗しました") from exc


def _compute_metrics(code: str, fins: pd.DataFrame, prices: pd.DataFrame) -> dict[str, object]:
    try:
        metrics = pipeline.compute_market_metrics(fins, prices)
    except Exception as exc:  # noqa: BLE001 -- 付随情報は失敗しても空値で継続する意図的なフォールバック
        logger.warning("[%s] 市場指標の計算に失敗したため空値で継続します: %s", code, exc)
        return {}
    return {key: (None if pd.isna(value) else value) for key, value in metrics.items()}


def _fetch_listing_date(code: str, prices: pd.DataFrame) -> dt.date | None:
    try:
        listing_date, _recently_listed = pipeline.estimate_listing_date(prices)
        return listing_date
    except Exception as exc:  # noqa: BLE001 -- 付随情報は失敗しても空値で継続する意図的なフォールバック
        logger.warning("[%s] 上場日の推定に失敗したため空値で継続します: %s", code, exc)
        return None


def _fetch_fiscal_year_end(code: str, fins: pd.DataFrame) -> dt.date | None:
    try:
        return excel_export._latest_actual_fy_end(fins)
    except Exception as exc:  # noqa: BLE001 -- 付随情報は失敗しても空値で継続する意図的なフォールバック
        logger.warning("[%s] 決算期末日の取得に失敗したため空値で継続します: %s", code, exc)
        return None


def _format_shareholders_text(code: str, shareholders: pd.DataFrame | None) -> str | None:
    if shareholders is None or shareholders.empty:
        return None
    try:
        return excel_export._format_shareholders_text(shareholders)
    except Exception as exc:  # noqa: BLE001 -- 付随情報は失敗しても空値で継続する意図的なフォールバック
        logger.warning("[%s] 大株主テキストの整形に失敗したため空値で継続します: %s", code, exc)
        return None


def _fetch_split_events_text(code: str, prices: pd.DataFrame) -> str | None:
    try:
        return excel_export._stock_split_events_text(prices)
    except Exception as exc:  # noqa: BLE001 -- 付随情報は失敗しても空値で継続する意図的なフォールバック
        logger.warning("[%s] 株式分割イベントの抽出に失敗したため空値で継続します: %s", code, exc)
        return None
