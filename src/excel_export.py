"""銘柄コードから「企業詳細」「実行表」のExcelを自動生成するモジュール。

J-Quantsから取得できる数値項目（時価総額・PBR・PER・配当利回り・株価・
四半期業績等）だけを自動入力する。事業概要・主要株主・将来予想・定性コメント
等の自由記述はJ-Quantsだけでは自動化できないため、生成後にユーザーが手動で
追記する運用とする（README.md参照）。
"""
from __future__ import annotations

import datetime as dt
import io
import math
from pathlib import Path

import openpyxl
import pandas as pd

from src import edinet_client, endpoints, pipeline
from src.jquants_client import JQuantsClient

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
COMPANY_DETAIL_TEMPLATE = TEMPLATE_DIR / "company_detail_template.xlsx"

# equities/masterのProdCat（商品区分）。011=内国株式（普通株式）で、それ以外は
# ETF(014)・REIT(013)・ETN(023)・外国株式(021)・日本銀行等(012)等、事業内容や
# 決算を持たない/対象外の銘柄のため、企業詳細・実行表の生成対象から除外する。
_COMMON_STOCK_PROD_CAT = "011"


class NotCommonStockError(Exception):
    """銘柄コードがETF・REIT・ETN等、株式会社の普通株式ではない場合。"""


def _to_numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _get_master_row(client: JQuantsClient, code: str) -> dict:
    listed = endpoints.get_listed_info(client)
    if listed.empty or "Code" not in listed.columns:
        return {}
    # equities/masterのCodeは5桁表記（4桁コードは末尾に"0"が付く）だが、
    # fins/summary・equities/bars/dailyは4桁のままでも通るため、入力側は
    # 4桁のことがある。末尾"0"を補った表記も候補にして突き合わせる。
    code_str = str(code)
    candidates = {code_str}
    if code_str.isdigit() and len(code_str) == 4:
        candidates.add(code_str + "0")
    row = listed.loc[listed["Code"].astype(str).isin(candidates)]
    return row.iloc[0].to_dict() if not row.empty else {}


def _ensure_common_stock(master_row: dict, code: str) -> None:
    prod_cat = master_row.get("ProdCat")
    if master_row and prod_cat != _COMMON_STOCK_PROD_CAT:
        name = master_row.get("CoName", "")
        raise NotCommonStockError(
            f"銘柄コード{code}（{name}）はETF・REIT・ETN等の金融商品であり、株式会社の"
            "普通株式ではありません。企業詳細・実行表は株式会社向けの項目（事業概要・決算等）"
            "のみに対応しています。"
        )


def _nearest_close(prices: pd.DataFrame, target_date) -> float | None:
    """target_date以前で最も近い（無ければ最初の）取引日の終値を返す。"""
    if prices.empty or target_date is None or pd.isna(target_date):
        return None
    target = pd.Timestamp(target_date)
    on_or_before = prices.loc[prices["Date"] <= target]
    row = on_or_before.iloc[-1] if not on_or_before.empty else prices.iloc[0]
    close = _to_numeric(pd.Series([row.get("C")])).iloc[0]
    return None if pd.isna(close) else float(close)


def _stock_split_events_text(prices: pd.DataFrame) -> str | None:
    """株価履歴のAdjFactor（調整係数）から株式分割・併合があった日付を検出する
    （rules.detect_stock_splitと同じ考え方。契約プランの取得可能期間より前の
    分割・併合は株価データが無く検出できない）。
    """
    if prices.empty or "AdjFactor" not in prices.columns or "Date" not in prices.columns:
        return None
    df = prices.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df["AdjFactor"] = _to_numeric(df["AdjFactor"])
    df = df.dropna(subset=["Date", "AdjFactor"]).sort_values("Date")
    hits = df.loc[df["AdjFactor"] != 1.0]
    if hits.empty:
        return None
    lines = [
        f"{row['Date'].date()}（調整係数 {row['AdjFactor']}）"
        for _, row in hits.iterrows()
    ]
    return "株式分割・併合: " + "、".join(lines)



def _latest_actual_fy_end(fins: pd.DataFrame) -> dt.date | None:
    """本決算(FY)の実績開示（Salesが入っている行）に基づく直近の決算期末日を返す。

    配当予想の修正等、Salesを伴わない開示にはCurFYEnが未来の（まだ実績が
    確定していない）決算期を指すものがあり、それを「最新の決算期」と
    誤認してEDINET検索の窓がずれる（未来日付になり書類が見つからない）
    ことがあるため、実績Salesが入っている本決算行だけから求める。
    """
    if fins.empty or not {"CurFYEn", "CurPerType", "Sales"}.issubset(fins.columns):
        return None
    f = fins.copy()
    f["CurFYEn"] = pd.to_datetime(f["CurFYEn"], errors="coerce")
    f["Sales"] = _to_numeric(f["Sales"])
    f = f.dropna(subset=["CurFYEn", "Sales"])
    f = f.loc[f["CurPerType"] == "FY"]
    if f.empty:
        return None
    return f["CurFYEn"].max().date()


def build_company_detail_excel(client: JQuantsClient, code: str) -> bytes:
    """銘柄コードから「企業詳細」シートを埋めたExcel(bytes)を生成する。"""
    code = str(code)
    master_row = _get_master_row(client, code)
    _ensure_common_stock(master_row, code)
    fins = endpoints.get_financials_by_code(client, code)
    prices = endpoints.get_price_history_by_code(client, code)
    metrics = pipeline.compute_market_metrics(fins, prices)
    listing_date, _recently_listed = pipeline.estimate_listing_date(prices)

    wb = openpyxl.load_workbook(COMPANY_DETAIL_TEMPLATE)
    ws = wb["原本"]

    ws["B1"] = code
    ws["E1"] = master_row.get("CoName", "")

    latest_fy_end = _latest_actual_fy_end(fins)
    if latest_fy_end is not None:
        ws["O1"] = f"{latest_fy_end.month}月"

    market_cap = metrics["market_cap"]
    if market_cap is not None and pd.notna(market_cap):
        ws["E2"] = round(market_cap / 1e8, 1)
    pbr = metrics["pbr"]
    if pbr is not None and pd.notna(pbr):
        ws["I2"] = round(pbr, 2)
    per = metrics["per"]
    if per is not None and pd.notna(per):
        ws["M2"] = round(per, 2)
    dividend_yield = metrics["dividend_yield"]
    if dividend_yield is not None and pd.notna(dividend_yield):
        ws["Q2"] = dividend_yield

    latest_close = metrics["latest_close"]
    if latest_close is not None and pd.notna(latest_close):
        ws["B3"] = round(latest_close)

    if listing_date is not None:
        ws["F3"] = listing_date
        ws["F3"].number_format = "yyyy/m/d"

    shares_out = metrics["shares_out"]
    if shares_out is not None and pd.notna(shares_out):
        ws["H4"] = round(shares_out / 1e4)

    market_name = master_row.get("MktNm")
    if market_name:
        ws["O4"] = market_name

    yuho = edinet_client.fetch_yuho_texts(code, latest_fy_end)

    if yuho["business_overview"]:
        ws["C7"] = yuho["business_overview"]
    if yuho["shareholders"] is not None and not yuho["shareholders"].empty:
        ws["C9"] = _format_shareholders_text(yuho["shareholders"])

    split_text = _stock_split_events_text(prices)
    c12_parts = [t for t in [yuho["potential_shares"], split_text] if t]
    if c12_parts:
        ws["C12"] = "\n\n".join(c12_parts)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _format_shareholders_text(shareholders: pd.DataFrame, top_n: int = 6) -> str:
    """大株主の状況テーブル(EDINET有報)を上位N件の読みやすい複数行テキストに整形する。"""
    lines = []
    for _, row in shareholders.head(top_n).iterrows():
        parts = [str(v) for v in row.tolist() if pd.notna(v) and str(v).strip()]
        lines.append("　".join(parts))
    return "\n".join(lines)


EXECUTION_TABLE_TEMPLATE = TEMPLATE_DIR / "execution_table_template.xlsx"

# テンプレート(templates/execution_table_template.xlsx)の固定レイアウト。
# 年度列は7枠(C,E,G,I,K,M,O)あり、C列から左詰めで実績年度を並べ、直近の
# 会社予想年度はその直後の列に置く（Oの右隣に単位列があるのと同じ構成）。
_YEAR_COLS = ["C", "E", "G", "I", "K", "M", "O"]
_ROW_SALES = {"1Q": 5, "2Q": 6, "3Q": 7, "FY": 8}
_ROW_PROFIT = {"1Q": 9, "2Q": 10, "3Q": 11, "FY": 12}
_ROW_PRICE = {"1Q": 14, "2Q": 15, "3Q": 16, "FY": 17}


def build_execution_table_excel(client: JQuantsClient, code: str) -> bytes:
    """銘柄コードから「実行表」（年度×四半期の売上・経常利益・株価マトリクス）
    を埋めたExcel(bytes)を生成する。

    テンプレート(templates/execution_table_template.xlsx、元の三桜工業実行表から
    個社データを除いたもの)のレイアウト・書式・数式(利益率・検算)をそのまま使い、
    値セルだけを埋める。対象年度は契約プラン（Lightは過去5年）でJ-Quantsから
    取得できる実績年度（直近6年分、テンプレートの列数に合わせる）＋直近の
    会社予想年度に自動で限定される（それより古い年度は手動での追記が必要）。
    """
    code = str(code)
    master_row = _get_master_row(client, code)
    _ensure_common_stock(master_row, code)
    fins = endpoints.get_financials_by_code(client, code)
    prices_raw = endpoints.get_price_history_by_code(client, code)
    metrics = pipeline.compute_market_metrics(fins, prices_raw)

    prices = prices_raw.copy()
    if not prices.empty:
        prices["Date"] = pd.to_datetime(prices["Date"], errors="coerce")
        prices = prices.dropna(subset=["Date"]).sort_values("Date")

    wb = openpyxl.load_workbook(EXECUTION_TABLE_TEMPLATE)
    ws = wb["1"]

    ws["B1"] = code
    ws["C1"] = master_row.get("CoName", "")
    latest_fy_end = _latest_actual_fy_end(fins)
    if latest_fy_end is not None:
        ws["M1"] = latest_fy_end.month

    if metrics["market_cap"] is not None and pd.notna(metrics["market_cap"]):
        ws["B2"] = round(metrics["market_cap"] / 1e8, 1)
    if metrics["pbr"] is not None and pd.notna(metrics["pbr"]):
        ws["F2"] = round(metrics["pbr"], 2)
    if metrics["per"] is not None and pd.notna(metrics["per"]):
        ws["J2"] = round(metrics["per"], 2)
    if metrics["dividend_yield"] is not None and pd.notna(metrics["dividend_yield"]):
        ws["O2"] = round(metrics["dividend_yield"] * 100, 2)
    if metrics["latest_close"] is not None and pd.notna(metrics["latest_close"]):
        ws["B3"] = round(metrics["latest_close"])

    required = {"CurFYEn", "CurPerType", "DiscDate", "Sales", "OdP"}
    if fins.empty or not required.issubset(fins.columns):
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    f = fins.copy()
    f["CurFYEn"] = pd.to_datetime(f["CurFYEn"], errors="coerce")
    f["CurPerEn"] = pd.to_datetime(f.get("CurPerEn"), errors="coerce")
    f["DiscDate"] = pd.to_datetime(f["DiscDate"], errors="coerce")
    f["Sales"] = _to_numeric(f["Sales"])
    f["OdP"] = _to_numeric(f["OdP"])
    f = f.dropna(subset=["CurFYEn"])

    # 決算短信以外の開示（配当予想の修正等）はSales/OdPが空でもCurFYEnを持つため、
    # 実績データの年度は「Salesが実際に入っている行」だけから拾う。
    f_actual = f.dropna(subset=["Sales"])
    actual_years = sorted(f_actual["CurFYEn"].dt.year.unique().tolist())
    # テンプレートの実績年度枠は6つ(C,E,G,I,K,M)なので、直近6年分に絞る。
    actual_years = actual_years[-6:]

    # 本決算(FY)実績の開示時点では、会社予想は「次の期(NxtFYEn)」に対する
    # NxFSales/NxFOdPとして入っている（FSales/FOdPはその期自体の予想であり、
    # 実績が確定済みのため空になる）。直近の本決算開示から次期の予想年度を
    # 求め、その年度がまだ無ければ追加する（テンプレートのO列＝予想専用枠）。
    forecast_year = None
    latest_forecast_sales = None
    latest_forecast_profit = None
    fy_actual_rows = f_actual.loc[f_actual["CurPerType"] == "FY"]
    if not fy_actual_rows.empty:
        latest_fy_row = fy_actual_rows.sort_values("DiscDate").iloc[-1]
        nx_fsales = _to_numeric(pd.Series([latest_fy_row.get("NxFSales")])).iloc[0]
        nx_fodp = _to_numeric(pd.Series([latest_fy_row.get("NxFOdP")])).iloc[0]
        nxt_fy_end = pd.to_datetime(latest_fy_row.get("NxtFYEn"), errors="coerce")
        if pd.notna(nx_fsales) and pd.notna(nxt_fy_end) and nxt_fy_end.year not in actual_years:
            forecast_year = nxt_fy_end.year
            latest_forecast_sales = nx_fsales
            latest_forecast_profit = nx_fodp

    if not actual_years and forecast_year is None:
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    # 実績年度は新しい方からM,K,I,G,E,Cの順に右詰めで割り当てる。
    # C列から左詰めで実績年度を並べ、予想年度はその直後の列に置く。
    col_for_year = {}
    for i, year in enumerate(actual_years):
        col_for_year[year] = _YEAR_COLS[i]
    if forecast_year is not None and len(actual_years) < len(_YEAR_COLS):
        col_for_year[forecast_year] = _YEAR_COLS[len(actual_years)]

    for year, col in col_for_year.items():
        ws[f"{col}4"] = f"{year}予想" if year == forecast_year else f"{year}年"

    for _, row in f_actual.sort_values(["CurFYEn", "CurPerType"]).iterrows():
        period_type = row["CurPerType"]
        if period_type not in _ROW_SALES:
            continue
        year = int(row["CurFYEn"].year)
        col = col_for_year.get(year)
        if col is None:
            continue
        sales = row["Sales"]
        profit = row["OdP"]
        if pd.notna(sales):
            sales_cell = ws[f"{col}{_ROW_SALES[period_type]}"]
            sales_cell.value = math.floor(sales / 1e8)
            sales_cell.number_format = "0;[Red](0);-"
        if pd.notna(profit):
            ws[f"{col}{_ROW_PROFIT[period_type]}"] = round(profit / 1e8, 2)
        close = _nearest_close(prices, row["CurPerEn"])
        if close is not None:
            ws[f"{col}{_ROW_PRICE[period_type]}"] = round(close)

    if forecast_year is not None:
        forecast_col = col_for_year[forecast_year]
        if latest_forecast_sales is not None and pd.notna(latest_forecast_sales):
            forecast_sales_cell = ws[f"{forecast_col}{_ROW_SALES['FY']}"]
            forecast_sales_cell.value = math.floor(latest_forecast_sales / 1e8)
            forecast_sales_cell.number_format = "0;[Red](0);-"
        if latest_forecast_profit is not None and pd.notna(latest_forecast_profit):
            ws[f"{forecast_col}{_ROW_PROFIT['FY']}"] = round(latest_forecast_profit / 1e8, 2)

    # 決算期が3月以外の会社等では、直近の実績年度がまだ本決算を迎えておらず
    # （1Q/2Q等の実績しかない）「決算」行が空欄になることがある。その場合は
    # 直近の開示に入っている会社予想(FSales/FOdP、その期自体の予想)で埋める
    # （本決算が実際に出れば、次回生成時に実績値へ自動的に置き換わる）。
    for year, col in col_for_year.items():
        if year == forecast_year:
            continue
        if ws[f"{col}{_ROW_SALES['FY']}"].value is not None:
            continue
        year_rows = f_actual.loc[f_actual["CurFYEn"].dt.year == year]
        if year_rows.empty:
            continue
        latest_row = year_rows.sort_values("DiscDate").iloc[-1]
        fsales = _to_numeric(pd.Series([latest_row.get("FSales")])).iloc[0]
        fodp = _to_numeric(pd.Series([latest_row.get("FOdP")])).iloc[0]
        if pd.notna(fsales):
            cell = ws[f"{col}{_ROW_SALES['FY']}"]
            cell.value = math.floor(fsales / 1e8)
            cell.number_format = "0;[Red](0);-"
        if pd.notna(fodp):
            ws[f"{col}{_ROW_PROFIT['FY']}"] = round(fodp / 1e8, 2)

    split_text = _stock_split_events_text(prices_raw)
    if split_text:
        ws["Q10"] = split_text

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
