"""J-Quantsから計算した株価・PER・PBR・配当利回り・時価総額を、yfinance
（Yahoo Finance）側の値と突き合わせて確認するための検証スクリプト。

J-Quantsだけでは値の誤読・バグ（本セッションで何度か発生）に気づきにくい
ため、同じ銘柄をもう一方の値と比較し、大きくズレていないかを目視確認する
用途。アプリ本体（app.py）には組み込まない
（yfinanceは非公式スクレイピング系でJ-Quantsより信頼性が低く、日本株の
対応も銘柄によっては欠落するため。またPERはJ-Quants側が会社予想EPSベース、
yfinance側は直近実績EPSベースなど算出方法が異なるため、多少のズレは
バグではなく正常）。

事前準備（yfinanceはこのスクリプト専用で、requirements.txtには含めていない。
app.py側では使わないため、Streamlit Cloudのデプロイ環境に不要な依存を
増やさないための判断）:
    pip install yfinance

使い方:
    python -m scripts.cross_check_yfinance 7203 6584 9517
    （銘柄コード省略時はデフォルトの数銘柄で実行）
"""
from __future__ import annotations

import sys

import yfinance as yf
from dotenv import load_dotenv

from src import endpoints, pipeline
from src.jquants_client import JQuantsClient

load_dotenv()

DEFAULT_CODES = ["7203", "6584", "9517"]


def _yfinance_ticker(code: str) -> str:
    code = str(code)
    if code.isdigit() and len(code) == 5 and code.endswith("0"):
        code = code[:4]
    return f"{code}.T"


def _fetch_jquants_name(client: JQuantsClient, code: str) -> str | None:
    listed = endpoints.get_listed_info(client)
    if listed.empty or "Code" not in listed.columns:
        return None
    candidates = {code}
    if code.isdigit() and len(code) == 4:
        candidates.add(code + "0")
    row = listed.loc[listed["Code"].astype(str).isin(candidates)]
    return None if row.empty else row.iloc[0].get("CoName")


def _fetch_jquants_metrics(client: JQuantsClient, code: str) -> dict:
    fins = endpoints.get_financials_by_code(client, code)
    prices = endpoints.get_price_history_by_code(client, code)
    return pipeline.compute_market_metrics(fins, prices)


def _fetch_yfinance_metrics(code: str) -> dict:
    info = yf.Ticker(_yfinance_ticker(code)).info
    # yfinanceのdividendYieldはJ-Quants側（小数、例:0.03=3%）と異なり
    # パーセント値そのもの（例:3.0=3%）で返るため、比較しやすいよう100で割る。
    dividend_yield = info.get("dividendYield")
    if dividend_yield is not None:
        dividend_yield = dividend_yield / 100
    return {
        "company_name": info.get("longName") or info.get("shortName"),
        "latest_close": info.get("currentPrice") or info.get("previousClose"),
        "per": info.get("trailingPE"),
        "pbr": info.get("priceToBook"),
        "dividend_yield": dividend_yield,
        "market_cap": info.get("marketCap"),
    }


def _print_comparison(code: str, name: str | None, jq: dict, yfin: dict) -> None:
    print(f"\n=== {code} {name or yfin.get('company_name') or ''} ===")
    rows = [
        ("株価", jq.get("latest_close"), yfin.get("latest_close")),
        ("PER", jq.get("per"), yfin.get("per")),
        ("PBR", jq.get("pbr"), yfin.get("pbr")),
        ("配当利回り", jq.get("dividend_yield"), yfin.get("dividend_yield")),
        ("時価総額", jq.get("market_cap"), yfin.get("market_cap")),
    ]
    for label, jq_value, yf_value in rows:
        print(f"  {label:8s}  J-Quants={jq_value!s:>15}  yfinance={yf_value!s:>15}")


def main(codes: list[str]) -> None:
    client = JQuantsClient()
    for code in codes:
        name = _fetch_jquants_name(client, code)
        jq = _fetch_jquants_metrics(client, code)
        try:
            yfin = _fetch_yfinance_metrics(code)
        except Exception as e:
            print(f"\n=== {code} {name or ''} ===")
            print(f"  yfinance取得失敗: {type(e).__name__}: {e}")
            continue
        _print_comparison(code, name, jq, yfin)


if __name__ == "__main__":
    main(sys.argv[1:] or DEFAULT_CODES)
