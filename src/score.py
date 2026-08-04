"""「10倍株候補スコア」の計算ロジック。

各条件は個別の純粋関数（DataFrameに依存しない、数値だけを受け取る関数）として
実装し、`tests/test_score.py` で単体テストする。DataFrameからこれらの入力値を
取り出す集計処理は `compute_scores` が行う。

対象データはJ-Quants（決算開示・株価）のみ。Web検索・生成AIは使わない。
未来の業績を独自推計する処理は含まない（判定できない場合は必ず「判定不能」を返す）。
"""
from __future__ import annotations

import math

import pandas as pd

_PROGRESS_THRESHOLD = {"1Q": 25.0, "2Q": 50.0, "3Q": 75.0}


def _is_valid_number(value) -> bool:
    return value is not None and not (isinstance(value, float) and math.isnan(value))


def score_market_cap(market_cap_oku: float | None) -> tuple[float, str]:
    """時価総額(億円)から加点と区分ラベルを返す。30億円未満は除外せず「超小型株」を返す。"""
    if not _is_valid_number(market_cap_oku):
        return 0.0, "判定不能"
    if market_cap_oku < 30:
        return 0.0, "超小型株"
    if market_cap_oku <= 300:
        return 1.0, "30億円〜300億円"
    if market_cap_oku <= 500:
        return 0.5, "300億円超〜500億円"
    return 0.0, "500億円超"


def score_three_year_revenue_growth(sales_last_4fy: list[float | None]) -> tuple[float, str]:
    """直近4期分のFY売上高（古い順）から、3期連続増収かどうかを判定する。

    欠損があれば無理に判定せず「判定不能」を返す。
    """
    if len(sales_last_4fy) != 4 or any(not _is_valid_number(s) for s in sales_last_4fy):
        return 0.0, "判定不能"
    increasing = all(sales_last_4fy[i] < sales_last_4fy[i + 1] for i in range(3))
    return (1.0 if increasing else 0.0), ("3期連続増収" if increasing else "判定可（3期連続増収ではない）")


def score_revenue_cagr(sales_3y_ago: float | None, sales_latest: float | None) -> tuple[float, float | None]:
    """3年間の売上CAGRから加点を計算する。15%以上=1点、25%以上=追加1点（最大2点）。"""
    if not _is_valid_number(sales_3y_ago) or not _is_valid_number(sales_latest) or sales_3y_ago <= 0:
        return 0.0, None
    cagr = (sales_latest / sales_3y_ago) ** (1 / 3) - 1
    if cagr >= 0.25:
        return 2.0, cagr
    if cagr >= 0.15:
        return 1.0, cagr
    return 0.0, cagr


def score_profit_growth_exceeds_sales_growth(
    sales_growth: float | None,
    profit_growth: float | None,
    prev_profit: float | None,
    curr_profit: float | None,
) -> tuple[float, str]:
    """売上+15%以上・経常利益+30%以上・利益成長率>売上成長率を満たす場合に1点。

    前年の利益が0以下の場合は通常の成長率計算をせず、黒字転換（売上+15%以上かつ
    直近が黒字）で判定する。
    """
    if _is_valid_number(prev_profit) and prev_profit <= 0:
        if _is_valid_number(curr_profit) and curr_profit > 0 and _is_valid_number(sales_growth) and sales_growth >= 0.15:
            return 1.0, "黒字転換かつ売上+15%以上"
        return 0.0, "黒字転換の条件を満たさない"
    if not _is_valid_number(sales_growth) or not _is_valid_number(profit_growth):
        return 0.0, "判定不能"
    if sales_growth >= 0.15 and profit_growth >= 0.30 and profit_growth > sales_growth:
        return 1.0, "増収率を上回る増益"
    return 0.0, "条件を満たさない"


def score_margin_improvement(margin_diff_points: float | None) -> float:
    """経常利益率が前年（同期）より2ポイント以上改善していれば1点。"""
    if not _is_valid_number(margin_diff_points):
        return 0.0
    return 1.0 if margin_diff_points >= 2.0 else 0.0


def score_turnaround(prev_profit: float | None, curr_profit: float | None, sustained_two_periods: bool) -> tuple[float, bool]:
    """営業利益または経常利益が赤字→黒字に転換した場合に2点、2期連続で黒字を維持できれば+1点。"""
    if not _is_valid_number(prev_profit) or not _is_valid_number(curr_profit):
        return 0.0, False
    turned = prev_profit <= 0 and curr_profit > 0
    if not turned:
        return 0.0, False
    points = 2.0 + (1.0 if sustained_two_periods else 0.0)
    return points, True


def score_upward_revision(
    sales_revision_pct: float | None,
    profit_revision_pct: float | None,
    consecutive_upward_count: int,
) -> float:
    """会社予想の上方修正: 売上+5%以上=1点、経常利益+10%以上=1点、2回以上連続=追加1点。"""
    points = 0.0
    if _is_valid_number(sales_revision_pct) and sales_revision_pct >= 0.05:
        points += 1.0
    if _is_valid_number(profit_revision_pct) and profit_revision_pct >= 0.10:
        points += 1.0
    if consecutive_upward_count >= 2:
        points += 1.0
    return points


def score_progress_ratio(
    period_type: str,
    progress_pct: float | None,
    prev_year_progress_pct: float | None,
) -> tuple[float, str]:
    """通期予想に対する累計経常利益の進捗率が目安（1Q25%/2Q50%/3Q75%）を超えれば1点。

    前年同期の進捗率が分かる場合は、それより10ポイント以上高いことも条件にする
    （データが無ければこの追加条件は課さない）。
    """
    threshold = _PROGRESS_THRESHOLD.get(period_type)
    if threshold is None or not _is_valid_number(progress_pct):
        return 0.0, "判定不能"
    if progress_pct < threshold:
        return 0.0, "進捗率が目安未達"
    if _is_valid_number(prev_year_progress_pct) and (progress_pct - prev_year_progress_pct) < 10.0:
        return 0.0, "前年同期からの進捗改善が不十分"
    return 1.0, "高進捗率"


def score_dilution(shares_growth_pct: float | None, looks_like_split: bool) -> tuple[float, str]:
    """発行済株式数の前年比増加率から減点を計算する。株式分割起因と推定される場合は減点しない。

    株式分割か実質的な希薄化かは、BPS(1株純資産)が株式数増加率にほぼ反比例して
    下がっているか（=純資産総額が変わらず1株当たりに薄まっただけ）で見分ける
    近似判定（looks_like_split）を呼び出し側で行う。
    """
    if not _is_valid_number(shares_growth_pct):
        return 0.0, "未判定"
    if looks_like_split:
        return 0.0, "株式分割によるものと推定（希薄化ではない）"
    if shares_growth_pct > 0.10:
        return -2.0, f"発行済株式数が前年比+{shares_growth_pct * 100:.1f}%（希薄化の可能性）"
    if shares_growth_pct > 0.05:
        return -1.0, f"発行済株式数が前年比+{shares_growth_pct * 100:.1f}%（希薄化の可能性）"
    return 0.0, "変化は小さい"


def score_downward_revision(has_downward_revision: bool, penalize_only: bool) -> float:
    """直近の下方修正歴による減点。penalize_only=Falseの場合は呼び出し側で除外フィルターを
    別途適用する想定のため、ここでは常に0点を返す（除外と減点を二重に行わないため）。
    """
    if not has_downward_revision:
        return 0.0
    return -2.0 if penalize_only else 0.0


def looks_like_stock_split(shares_growth_pct: float | None, bps_growth_pct: float | None, tolerance: float = 0.08) -> bool:
    """株式数増加が株式分割によるものらしいかを、BPS(1株純資産)の変化から近似判定する。

    株式分割なら総資産(純資産)は変わらず株式数だけ増えるため、BPSはほぼ
    1/(1+shares_growth_pct)倍になる。実質的な増資（希薄化）なら新たな資本が
    入るため、BPSはその比率通りには下がらない。
    """
    if not _is_valid_number(shares_growth_pct) or not _is_valid_number(bps_growth_pct) or shares_growth_pct <= 0:
        return False
    expected_bps_growth = 1.0 / (1.0 + shares_growth_pct) - 1.0
    return abs(bps_growth_pct - expected_bps_growth) <= tolerance
