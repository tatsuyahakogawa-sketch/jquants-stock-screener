"""スクリーニング全体のオーケストレーション。

指定期間について株価・決算開示データを取得し、各ルールを適用して
「どの銘柄が、いつ、どの条件に合致したか」の一覧、および銘柄単位で
集約したサマリーを返す。
"""
from __future__ import annotations

import datetime as dt
import warnings

import pandas as pd

from src import endpoints, rules, tdnet_client
from src.config import LISTING_LOOKBACK_YEARS, LISTING_DATE_BOUNDARY_TOLERANCE_DAYS
from src.jquants_client import JQuantsClient

RULE_LABELS = {
    "stop_high": "ストップ高",
    "sales_growth_major": "売上高が大幅に増加（前年同期比+20%以上）",
    "sales_growth_explosive": "売上高が爆発的に増加（前年同期比+50%以上）",
    "earnings_beat": "本決算が会社予想を上回った",
    "stock_split": "株式分割・併合等（調整係数の変化を検知）",
    "equity_ratio_high": "自己資本比率60%以上",
    "profit_doubling": "経常利益が4年で2倍以上",
    "market_upgrade_to_prime": "スタンダード/グロースからプライムへ市場変更",
    "new_facility_or_store": "新工場・新店舗の開示（TDnetタイトル検出・要確認）",
    "exchange_transfer_to_tokyo": "札幌/福岡/名古屋証取から東証への上場（TDnetタイトル検出・要確認）",
    "downward_revision": "業績予想の下方修正（マイナス要因）",
}

# サマリーの「合致数」に含めるルール（downward_revisionはマイナス要因なので除外用に別扱い）
POSITIVE_RULES = [r for r in RULE_LABELS if r != "downward_revision"]
NEGATIVE_RULES = ["downward_revision"]


def run_screening(
    client: JQuantsClient,
    start: dt.date,
    end: dt.date,
) -> pd.DataFrame:
    """start〜end の期間で全ルールを適用し、イベント単位の結果をまとめて返す。

    戻り値の列: Code, CompanyName, Rule, RuleLabel, Date, Detail
    """
    listed_info = endpoints.get_listed_info(client)
    name_map: dict[str, str] = {}
    sector_map: dict[str, str] = {}
    if not listed_info.empty and "Code" in listed_info.columns:
        if "CoName" in listed_info.columns:
            name_map = dict(zip(listed_info["Code"], listed_info["CoName"]))
        if "S33Nm" in listed_info.columns:
            sector_map = dict(zip(listed_info["Code"], listed_info["S33Nm"]))

    quotes_df = endpoints.get_daily_quotes_range(client, start, end)
    statements_df = endpoints.get_statements_range(client, start, end)
    master_history_df = endpoints.get_listed_info_range(client, start, end)

    hits = [
        rules.detect_stop_high(quotes_df),
        rules.detect_stock_split(quotes_df),
        rules.detect_sales_growth(statements_df),
        rules.detect_earnings_beat(statements_df),
        rules.detect_equity_ratio(statements_df),
        rules.detect_profit_doubling(statements_df),
        rules.detect_downward_revision(statements_df),
        rules.detect_market_upgrade_to_prime(master_history_df),
    ]

    try:
        disclosures_df = tdnet_client.get_disclosures_range(start, end)
        hits.append(rules.detect_new_facility_or_store(disclosures_df))
        hits.append(rules.detect_exchange_transfer_to_tokyo(disclosures_df))
    except Exception as e:
        # TDnetの非公式ミラーは個人運営で不安定なことがあるため、失敗しても
        # 他のルールの結果は返す（README参照）。
        warnings.warn(f"TDnet開示情報の取得に失敗しました（新工場・新店舗・東証移籍の検出をスキップします）: {e}")

    hits = [h for h in hits if not h.empty]
    if not hits:
        return pd.DataFrame(columns=["Code", "CompanyName", "Rule", "RuleLabel", "Date", "Detail"])

    result = pd.concat(hits, ignore_index=True)
    result = result.rename(columns={"rule": "Rule", "detail": "Detail"})
    result["CompanyName"] = result["Code"].map(name_map).fillna("")
    result["Sector"] = result["Code"].map(sector_map).fillna("")
    result["RuleLabel"] = result["Rule"].map(RULE_LABELS).fillna(result["Rule"])
    result["Date"] = pd.to_datetime(result["Date"])
    result = result.sort_values(["Date", "Code"]).reset_index(drop=True)
    return result[["Code", "CompanyName", "Sector", "Rule", "RuleLabel", "Date", "Detail"]]


def build_summary(hits: pd.DataFrame) -> pd.DataFrame:
    """イベント単位の結果(run_screeningの戻り値)を銘柄単位に集約する。

    各ルールについて「合致したか」「最新の合致日」の列を持つワイドテーブルに
    し、MatchedCount（POSITIVE_RULESのうち合致した数）と
    HasDownwardRevision（下方修正歴があるか）を付与する。
    """
    columns = ["Code", "CompanyName", "Sector", "MatchedCount", "HasDownwardRevision"]
    columns += [f"{rule}_matched" for rule in RULE_LABELS]
    columns += [f"{rule}_date" for rule in RULE_LABELS]
    if hits.empty:
        return pd.DataFrame(columns=columns)

    base = hits[["Code", "CompanyName", "Sector"]].drop_duplicates(subset=["Code"]).set_index("Code")

    for rule in RULE_LABELS:
        rule_hits = hits.loc[hits["Rule"] == rule]
        latest_date = rule_hits.groupby("Code")["Date"].max()
        base[f"{rule}_matched"] = base.index.isin(latest_date.index)
        base[f"{rule}_date"] = latest_date.reindex(base.index)

    base["MatchedCount"] = base[[f"{r}_matched" for r in POSITIVE_RULES]].sum(axis=1)
    base["HasDownwardRevision"] = base[[f"{r}_matched" for r in NEGATIVE_RULES]].any(axis=1)

    return base.reset_index()[columns]


def _to_numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


# EPSはCurPerType（1Q/2Q/3Q/FY等）の期間累計値で開示されるため、会社予想EPS(FEPS)が
# 無いときに実績EPSで代用する場合は年率換算してから使う。
_EPS_ANNUALIZE_FACTOR = {"1Q": 4.0, "2Q": 2.0, "3Q": 4 / 3, "4Q": 1.0, "FY": 1.0}


def _last_valid_value(df: pd.DataFrame, column: str) -> float | None:
    """dfの中でcolumnが数値として読める最後の（最新の）値を返す。

    決算短信は開示形式によって一部の項目（BPS等）が入っていない回があるため、
    直近1件だけでなく履歴全体から「最後に開示された値」を探す。
    """
    if column not in df.columns:
        return None
    values = pd.to_numeric(df[column], errors="coerce").dropna()
    return values.iloc[-1] if not values.empty else None


def _last_valid_eps_annualized(df: pd.DataFrame) -> float | None:
    """実績EPS(EPS)の最後の開示値を、その開示時点のCurPerTypeに応じて年率換算する。"""
    if "EPS" not in df.columns:
        return None
    eps_numeric = pd.to_numeric(df["EPS"], errors="coerce")
    valid_idx = eps_numeric.dropna().index
    if len(valid_idx) == 0:
        return None
    idx = valid_idx[-1]
    period_type = df.loc[idx, "CurPerType"] if "CurPerType" in df.columns else None
    factor = _EPS_ANNUALIZE_FACTOR.get(period_type, 1.0)
    return eps_numeric.loc[idx] * factor


def enrich_with_market_data(client: JQuantsClient, summary: pd.DataFrame) -> pd.DataFrame:
    """銘柄ごとに最新の株価・決算情報を取得し、時価総額・PER・PBR・配当利回りを付与する。

    PERは会社の通期予想EPS(FEPS)を使う「予想PER」（無ければ実績EPSを開示期間
    (1Q/2Q/3Q/FY等)に応じて年率換算して代用）。PBRはBPS(1株あたり純資産)を使う。
    時価総額は 直近終値 × (発行済株式数ShOutFY − 自己株式TrShFY) の近似値。
    BPS等は決算短信の開示形式によって入っていない回があるため、各項目は
    直近1件の開示だけでなく履歴全体から「最後に開示された値」を使っている。

    上場日はJ-Quantsに存在しないため、契約プランで取れる株価履歴（Lightは過去5年）の
    最古日を「推定初値観測日」として使う近似判定を行う。取得可能期間の開始日付近から
    既にデータがある場合は「それより前から上場していた可能性がある」として
    EstimatedListingDate=None, RecentlyListed=False とし、正確な上場日は「不明」とする。

    いずれも銘柄数に比例してJ-Quantsへの追加リクエストが発生するため、
    合致銘柄数が多いと時間がかかる（当日分はキャッシュされる）。
    """
    new_cols = [
        "LatestClose", "LatestPriceDate", "MarketCap", "PER", "PBR", "DividendYield",
        "EstimatedListingDate", "RecentlyListed",
    ]
    if summary.empty:
        return summary.assign(**{c: pd.Series(dtype="float64") for c in new_cols})

    window_start = dt.date.today() - dt.timedelta(days=1) - dt.timedelta(days=365 * LISTING_LOOKBACK_YEARS)

    rows = []
    for code in summary["Code"]:
        fins = endpoints.get_financials_by_code(client, code)
        price_history = endpoints.get_price_history_by_code(client, code)

        latest_close = None
        latest_price_date = None
        estimated_listing_date = None
        recently_listed = None
        if not price_history.empty and "Date" in price_history.columns and "C" in price_history.columns:
            price_history = price_history.copy()
            price_history["Date"] = pd.to_datetime(price_history["Date"], errors="coerce")
            price_history = price_history.dropna(subset=["Date"]).sort_values("Date")
            if not price_history.empty:
                latest_close = _to_numeric(price_history["C"]).iloc[-1]
                latest_price_date = price_history["Date"].iloc[-1]

                earliest_date = price_history["Date"].iloc[0].date()
                if (earliest_date - window_start).days > LISTING_DATE_BOUNDARY_TOLERANCE_DAYS:
                    # データ取得可能期間の開始日より明確に後ろから始まっている
                    # -> その頃に新規上場した可能性が高いと推定する
                    estimated_listing_date = earliest_date
                    recently_listed = True
                else:
                    # 取得可能期間の開始日付近から既にデータがある
                    # -> それより前から上場していた可能性があり、正確な上場日は不明
                    recently_listed = False

        feps = bps = shares_out = treasury_shares = div_ann = annualized_eps = None
        if not fins.empty and "DiscDate" in fins.columns:
            fins = fins.copy()
            fins["DiscDate"] = pd.to_datetime(fins["DiscDate"], errors="coerce")
            fins = fins.dropna(subset=["DiscDate"]).sort_values("DiscDate")
            if not fins.empty:
                # 項目ごとに「最後に開示された値」を使う（BPS等は開示されない回もあるため、
                # 直近1件の開示だけでは埋まらないケースが多い）
                feps = _last_valid_value(fins, "FEPS")
                bps = _last_valid_value(fins, "BPS")
                shares_out = _last_valid_value(fins, "ShOutFY")
                treasury_shares = _last_valid_value(fins, "TrShFY")
                div_ann = _last_valid_value(fins, "FDivAnn")
                annualized_eps = _last_valid_eps_annualized(fins)

        market_cap = per = pbr = dividend_yield = None
        if latest_close is not None and pd.notna(latest_close):
            # PERは会社の通期予想EPS(FEPS)を優先し、無ければ実績EPSを年率換算して代用する
            per_eps = feps if feps and pd.notna(feps) and feps > 0 else annualized_eps
            if per_eps and pd.notna(per_eps) and per_eps > 0:
                per = latest_close / per_eps
            if bps is not None and pd.notna(bps) and bps > 0:
                pbr = latest_close / bps
            if shares_out is not None and pd.notna(shares_out):
                float_shares = shares_out - (treasury_shares if pd.notna(treasury_shares) else 0)
                market_cap = latest_close * float_shares
            if div_ann is not None and pd.notna(div_ann) and div_ann >= 0:
                # div_ann == 0 は「無配予定」の明示的な開示であり、未開示(NaN)とは区別する
                dividend_yield = div_ann / latest_close

        rows.append({
            "Code": code,
            "LatestClose": latest_close,
            "LatestPriceDate": latest_price_date,
            "MarketCap": market_cap,
            "PER": per,
            "PBR": pbr,
            "DividendYield": dividend_yield,
            "EstimatedListingDate": estimated_listing_date,
            "RecentlyListed": recently_listed,
        })

    enrichment = pd.DataFrame(rows)
    return summary.merge(enrichment, on="Code", how="left")
