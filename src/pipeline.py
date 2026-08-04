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
from src.config import (
    LISTING_LOOKBACK_YEARS,
    LISTING_DATE_BOUNDARY_TOLERANCE_DAYS,
    PROFIT_DOUBLING_YEARS,
)
from src.jquants_client import JQuantsClient

RULE_LABELS = {
    "stop_high": "ストップ高",
    "sales_growth_major": "売上高が大幅に増加（前年同期比+20%以上）",
    "sales_growth_explosive": "売上高が爆発的に増加（前年同期比+50%以上）",
    "earnings_beat": "本決算が会社予想を上回った",
    "stock_split": "株式分割・併合の発表（TDnetタイトル検出・要確認）",
    "equity_ratio_high": "自己資本比率60%以上",
    "profit_doubling": "経常利益が4年で2倍以上",
    "pbr_low": "PBR1倍以下",
    "two_quarter_growth": "四半期決算2期連続増収増益",
    "market_upgrade_to_prime": "スタンダード/グロースからプライムへの市場変更の発表（TDnetタイトル検出・要確認）",
    "new_facility_or_store": "新工場・新店舗の開示（TDnetタイトル検出・要確認）",
    "exchange_transfer_to_tokyo": "札幌/福岡/名古屋証取から東証への上場（TDnetタイトル検出・要確認）",
    "downward_revision": "業績予想の下方修正（マイナス要因）",
}

# サマリーの「合致数」に含めるルール（downward_revisionはマイナス要因なので除外用に別扱い）
POSITIVE_RULES = [r for r in RULE_LABELS if r != "downward_revision"]
NEGATIVE_RULES = ["downward_revision"]

# PBR・自己資本比率は「いつ起きたか」というイベントではなく、開示時点での銘柄の
# 属性（状態）を見るルールのため、UI上はイベント条件とは別枠の絞り込みとして扱う。
ATTRIBUTE_RULES = ["pbr_low", "equity_ratio_high"]
EVENT_RULES = [r for r in POSITIVE_RULES if r not in ATTRIBUTE_RULES]


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
    # 増収率(YoY)・増収増益2期連続・経常利益4年倍増の各ルールは、対象開示より
    # 1〜4年前の同期(同じCurPerType)の開示と比較する必要がある。statements_df を
    # start〜end だけで取得すると比較対象の過去開示がそもそも取得できておらず、
    # 前年同期比較が常にNaNになってヒットが極端に少なくなってしまう
    # （選択期間が1年以上にならない限り比較不能）。比較用に必要な分だけ遡って
    # 取得し、実際のヒットは後段でstart〜end開示分に絞り込む。
    comparison_lookback_days = 365 * PROFIT_DOUBLING_YEARS + 60
    statements_fetch_start = start - dt.timedelta(days=comparison_lookback_days)
    statements_df = endpoints.get_statements_range(client, statements_fetch_start, end)

    hits = [
        rules.detect_stop_high(quotes_df),
        rules.detect_sales_growth(statements_df),
        rules.detect_earnings_beat(statements_df),
        rules.detect_equity_ratio(statements_df),
        rules.detect_profit_doubling(statements_df),
        rules.detect_low_pbr(statements_df, quotes_df),
        rules.detect_two_quarter_growth(statements_df),
        rules.detect_downward_revision(statements_df),
    ]

    try:
        disclosures_df = tdnet_client.get_disclosures_range(start, end)
        hits.append(rules.detect_new_facility_or_store(disclosures_df))
        hits.append(rules.detect_exchange_transfer_to_tokyo(disclosures_df))
        hits.append(rules.detect_stock_split(disclosures_df))
        hits.append(rules.detect_market_upgrade_to_prime(disclosures_df))
    except Exception as e:
        # TDnetの非公式ミラーは個人運営で不安定なことがあるため、失敗しても
        # 他のルールの結果は返す（README参照）。
        warnings.warn(
            "TDnet開示情報の取得に失敗しました"
            f"（新工場・新店舗・東証移籍・株式分割・プライム市場変更の発表の検出をスキップします）: {e}"
        )

    hits = [h for h in hits if not h.empty]
    if not hits:
        return pd.DataFrame(columns=["Code", "CompanyName", "Rule", "RuleLabel", "Date", "Detail"])

    result = pd.concat(hits, ignore_index=True)
    result = result.rename(columns={"rule": "Rule", "detail": "Detail"})
    result["CompanyName"] = result["Code"].map(name_map).fillna("")
    result["Sector"] = result["Code"].map(sector_map).fillna("")
    result["RuleLabel"] = result["Rule"].map(RULE_LABELS).fillna(result["Rule"])
    result["Date"] = pd.to_datetime(result["Date"])
    # 比較用に遡って取得した過去開示分がヒットに混ざらないよう、実際の開示日が
    # ユーザーの選択期間(start〜end)に入っているものだけに絞り込む。
    result = result.loc[
        (result["Date"] >= pd.Timestamp(start)) & (result["Date"] <= pd.Timestamp(end))
    ]
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


def _last_valid_full_year_value(df: pd.DataFrame, column: str) -> float | None:
    """dfの中でcolumnが数値として読める最後の値を、CurPerType=='FY'の行だけから探す。

    会社予想の修正開示(EarnForecastRevision等)の中には、CurPerTypeが四半期
    (1Q等)のまま会社予想値を更新しているものがあり、単純に「最後に開示された値」
    を取ると通期予想と四半期限定の値を区別できず、PER等の計算が大きく壊れる
    （例: 通期予想EPSのつもりで四半期分の小さい値を使ってしまう）。そのため
    会社予想EPS(FEPS)等は必ず通期(FY)区分の開示に限定する。
    """
    if column not in df.columns or "CurPerType" not in df.columns:
        return None
    return _last_valid_value(df.loc[df["CurPerType"] == "FY"], column)


def _dividend_forecast_or_trailing(df: pd.DataFrame) -> float | None:
    """年間配当（1株あたり）を、開示されている中で最も「今の実力」に近い値で返す。

    本決算実績の開示時点ではFDivAnn（当期の配当予想）は確定済みのため空になり、
    NxFDivAnn（来期の配当予想）が入る。会社によってはさらに、業績が定まらない
    等の理由で配当予想自体を出さない（FDivAnn・NxFDivAnnとも常に空）こともある。
    その場合は「無配」と区別するため、直前に実際に支払われた年間配当(DivAnn)を
    参考値として使う（会社予想ではなく実績配当に基づく利回りになる）。
    """
    if "CurPerType" not in df.columns:
        return None
    fy_rows = df.loc[df["CurPerType"] == "FY"]
    for column in ("FDivAnn", "NxFDivAnn", "DivAnn"):
        value = _last_valid_value(fy_rows, column)
        if value is not None and pd.notna(value):
            return value
    return None


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


def compute_market_metrics(fins: pd.DataFrame, price_history: pd.DataFrame) -> dict:
    """財務履歴と株価履歴から、時価総額・PER・PBR・配当利回り・最新終値等を計算する。

    enrich_with_market_data（銘柄集計テーブル用）とexcel_export（Excel出力用）の
    両方から呼ばれる共通ロジック。
    """
    latest_close = None
    latest_price_date = None
    if not price_history.empty and "Date" in price_history.columns and "C" in price_history.columns:
        price_history = price_history.copy()
        price_history["Date"] = pd.to_datetime(price_history["Date"], errors="coerce")
        price_history = price_history.dropna(subset=["Date"]).sort_values("Date")
        if not price_history.empty:
            latest_close = _to_numeric(price_history["C"]).iloc[-1]
            latest_price_date = price_history["Date"].iloc[-1]

    feps = bps = shares_out = treasury_shares = div_ann = annualized_eps = None
    if not fins.empty and "DiscDate" in fins.columns:
        fins = fins.copy()
        fins["DiscDate"] = pd.to_datetime(fins["DiscDate"], errors="coerce")
        fins = fins.dropna(subset=["DiscDate"]).sort_values("DiscDate")
        if not fins.empty:
            feps = _last_valid_full_year_value(fins, "FEPS")
            bps = _last_valid_value(fins, "BPS")
            shares_out = _last_valid_value(fins, "ShOutFY")
            treasury_shares = _last_valid_value(fins, "TrShFY")
            div_ann = _dividend_forecast_or_trailing(fins)
            annualized_eps = _last_valid_eps_annualized(fins)

    market_cap = per = pbr = dividend_yield = None
    if latest_close is not None and pd.notna(latest_close):
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

    return {
        "latest_close": latest_close,
        "latest_price_date": latest_price_date,
        "market_cap": market_cap,
        "per": per,
        "pbr": pbr,
        "dividend_yield": dividend_yield,
        "shares_out": shares_out,
    }


def estimate_listing_date(
    price_history: pd.DataFrame,
    lookback_years: int = LISTING_LOOKBACK_YEARS,
) -> tuple[dt.date | None, bool | None]:
    """株価履歴の最古日から、上場日の近似値を推定する。

    契約プランの取得可能期間（Lightは過去5年）の開始日付近から既にデータが
    ある場合は、それより前から上場していた可能性があるため正確な上場日は
    不明とする（戻り値は (None, False)）。取得可能期間の開始日より明確に
    後ろから始まっている場合は、新規上場の可能性が高いとみなす。
    """
    window_start = dt.date.today() - dt.timedelta(days=1) - dt.timedelta(days=365 * lookback_years)
    if price_history.empty or "Date" not in price_history.columns:
        return None, None

    ph = price_history.copy()
    ph["Date"] = pd.to_datetime(ph["Date"], errors="coerce")
    ph = ph.dropna(subset=["Date"]).sort_values("Date")
    if ph.empty:
        return None, None

    earliest_date = ph["Date"].iloc[0].date()
    if (earliest_date - window_start).days > LISTING_DATE_BOUNDARY_TOLERANCE_DAYS:
        return earliest_date, True
    return None, False


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

    rows = []
    for code in summary["Code"]:
        fins = endpoints.get_financials_by_code(client, code)
        price_history = endpoints.get_price_history_by_code(client, code)

        metrics = compute_market_metrics(fins, price_history)
        estimated_listing_date, recently_listed = estimate_listing_date(price_history)

        rows.append({
            "Code": code,
            "LatestClose": metrics["latest_close"],
            "LatestPriceDate": metrics["latest_price_date"],
            "MarketCap": metrics["market_cap"],
            "PER": metrics["per"],
            "PBR": metrics["pbr"],
            "DividendYield": metrics["dividend_yield"],
            "EstimatedListingDate": estimated_listing_date,
            "RecentlyListed": recently_listed,
        })

    enrichment = pd.DataFrame(rows)
    return summary.merge(enrichment, on="Code", how="left")
