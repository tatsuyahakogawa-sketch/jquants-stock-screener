"""須田忠雄事務所ルールの判定ロジック。

J-Quantsの数値データで判定できるルールに加え、equities/masterの履歴を使った
市場変更検出、TDnet開示タイトルのキーワード検出（新工場・新店舗、東証移籍）
を含む。

2026年1月のJ-Quants V2移行に伴い、レスポンスの列名が短縮形に変わっている
（例: `Close`→`C`, `AdjustmentFactor`→`AdjFactor`）。以下の定数はV2の公式
APIリファレンス(https://jpx-jquants.com/en/spec/eq-bars-daily,
https://jpx-jquants.com/en/spec/fin-summary)を確認した上で設定しているが、
サービス側の仕様変更で再度ズレる可能性があるため、`scripts/inspect_schema.py`
の出力と食い違っていないか都度確認すること。
"""
from __future__ import annotations

import re

import pandas as pd

from src.config import (
    SALES_GROWTH_MAJOR_THRESHOLD,
    SALES_GROWTH_EXPLOSIVE_THRESHOLD,
    EARNINGS_BEAT_THRESHOLD,
    EQUITY_RATIO_THRESHOLD,
    PROFIT_DOUBLING_YEARS,
    PROFIT_DOUBLING_MULTIPLE,
    DOWNWARD_REVISION_THRESHOLD,
)

# --- 列名定数（要: 実際のAPIレスポンスと突き合わせて確認） -----------------

QUOTES_CODE = "Code"
QUOTES_DATE = "Date"
QUOTES_CLOSE = "C"
QUOTES_UPPER_LIMIT_FLAG = "UL"  # "1"=ストップ高で終値確定, "0"=それ以外
QUOTES_ADJUSTMENT_FACTOR = "AdjFactor"  # 分割・併合等があった日は1.0以外になる

STMT_CODE = "Code"
STMT_DISCLOSED_DATE = "DiscDate"
STMT_PERIOD_TYPE = "CurPerType"  # "1Q","2Q","3Q","4Q","FY" 等
STMT_PERIOD_END = "CurPerEn"
STMT_FY_END = "CurFYEn"
STMT_NET_SALES = "Sales"
STMT_OPERATING_PROFIT = "OP"
STMT_ORDINARY_PROFIT = "OdP"
STMT_PROFIT = "NP"
STMT_EQUITY_RATIO = "EqAR"  # 自己資本比率。0.6 = 60% のような比率で返る
STMT_FORECAST_NET_SALES = "FSales"
STMT_FORECAST_OPERATING_PROFIT = "FOP"
STMT_FORECAST_PROFIT = "FNP"

MASTER_CODE = "Code"
MASTER_DATE = "Date"
MASTER_MKT_NAME = "MktNm"  # 市場区分名。「プライム」「スタンダード」「グロース」等

# TDnet開示タイトルのキーワード（誤検出のリスクがあるため、詳細列に開示タイトルを
# そのまま出し、ユーザー自身がリンク先で確認できるようにしている）
FACILITY_KEYWORDS = ["新工場", "工場新設", "新設工場", "生産拠点の新設", "第二工場", "第2工場"]
STORE_KEYWORDS = ["新店舗", "新規出店", "店舗新設", "新規開設"]
# 「新工場」「第二工場」等は閉鎖・計画中止のニュースにも一致してしまうため、
# これらの語を含むタイトルは逆方向（ネガティブ）の話として除外する。
FACILITY_STORE_EXCLUSION_KEYWORDS = ["閉鎖", "中止", "撤退", "廃止", "休止", "縮小"]
REGIONAL_EXCHANGES = ["札幌証券取引所", "福岡証券取引所", "名古屋証券取引所"]
TOKYO_EXCHANGE_KEYWORDS = ["東京証券取引所", "東証"]


def _to_numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def detect_stop_high(quotes_df: pd.DataFrame) -> pd.DataFrame:
    """ストップ高で終値が確定した(Code, Date)の一覧を返す。"""
    if quotes_df.empty or QUOTES_UPPER_LIMIT_FLAG not in quotes_df.columns:
        return pd.DataFrame(columns=[QUOTES_CODE, QUOTES_DATE, "rule", "detail"])
    flag = _to_numeric(quotes_df[QUOTES_UPPER_LIMIT_FLAG]).fillna(0)
    hit = quotes_df.loc[flag == 1, [QUOTES_CODE, QUOTES_DATE, QUOTES_CLOSE]].copy()
    hit["rule"] = "stop_high"
    hit["detail"] = "ストップ高で終値が確定 (Close=" + hit[QUOTES_CLOSE].astype(str) + ")"
    return hit.drop(columns=[QUOTES_CLOSE]).rename(columns={QUOTES_CODE: "Code", QUOTES_DATE: "Date"})


def detect_stock_split(quotes_df: pd.DataFrame) -> pd.DataFrame:
    """株式分割・併合等でAdjustmentFactorが1.0以外になった(Code, Date)の一覧。"""
    if quotes_df.empty or QUOTES_ADJUSTMENT_FACTOR not in quotes_df.columns:
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])
    factor = _to_numeric(quotes_df[QUOTES_ADJUSTMENT_FACTOR])
    hit = quotes_df.loc[factor.notna() & (factor != 1.0), [QUOTES_CODE, QUOTES_DATE]].copy()
    hit["rule"] = "stock_split"
    factors = factor.loc[hit.index]
    hit["detail"] = "調整係数 " + factors.astype(str) + "（分割/併合等の可能性）"
    return hit.rename(columns={QUOTES_CODE: "Code", QUOTES_DATE: "Date"})


def detect_sales_growth(statements_df: pd.DataFrame) -> pd.DataFrame:
    """同一の決算期タイプ(TypeOfCurrentPeriod)の前年同期と比べ、売上高が
    大幅(+20%以上) または 爆発的(+50%以上) に増えた開示の一覧。
    """
    required = {STMT_CODE, STMT_PERIOD_TYPE, STMT_PERIOD_END, STMT_NET_SALES, STMT_DISCLOSED_DATE}
    if statements_df.empty or not required.issubset(statements_df.columns):
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    df = statements_df.copy()
    df[STMT_NET_SALES] = _to_numeric(df[STMT_NET_SALES])
    df = df.dropna(subset=[STMT_NET_SALES])
    df[STMT_PERIOD_END] = pd.to_datetime(df[STMT_PERIOD_END], errors="coerce")
    df = df.sort_values([STMT_CODE, STMT_PERIOD_TYPE, STMT_PERIOD_END])

    df["prev_net_sales"] = df.groupby([STMT_CODE, STMT_PERIOD_TYPE])[STMT_NET_SALES].shift(1)
    df["prev_period_end"] = df.groupby([STMT_CODE, STMT_PERIOD_TYPE])[STMT_PERIOD_END].shift(1)

    valid = df["prev_net_sales"].notna() & (df["prev_net_sales"] > 0)
    # 前年同期との比較になっているかの軽いチェック（350〜380日程度離れているか）
    gap_days = (df[STMT_PERIOD_END] - df["prev_period_end"]).dt.days
    valid &= gap_days.between(330, 400)

    growth = (df[STMT_NET_SALES] - df["prev_net_sales"]) / df["prev_net_sales"]
    df["growth_rate"] = growth

    hit = df.loc[valid & (growth >= SALES_GROWTH_MAJOR_THRESHOLD)].copy()
    hit["rule"] = hit["growth_rate"].apply(
        lambda g: "sales_growth_explosive" if g >= SALES_GROWTH_EXPLOSIVE_THRESHOLD else "sales_growth_major"
    )
    hit["detail"] = "前年同期比 売上高 " + (hit["growth_rate"] * 100).round(1).astype(str) + "% 増"
    result = hit[[STMT_CODE, STMT_DISCLOSED_DATE, "rule", "detail"]].rename(
        columns={STMT_CODE: "Code", STMT_DISCLOSED_DATE: "Date"}
    )
    return result


def detect_earnings_beat(statements_df: pd.DataFrame) -> pd.DataFrame:
    """本決算(TypeOfCurrentPeriod=='FY')の実績が、直前の開示で会社自身が
    出していた通期予想を上回っていた場合を検出する。

    前提: 「直前の開示」は同一銘柄・同一決算期(CurrentFiscalYearEndDate)で
    DisclosedDateが最も近い過去の開示とする。取得期間が短いと直前開示が
    データに含まれず判定できないことがある。
    """
    required = {
        STMT_CODE, STMT_PERIOD_TYPE, STMT_FY_END, STMT_DISCLOSED_DATE,
        STMT_PROFIT, STMT_FORECAST_PROFIT,
    }
    if statements_df.empty or not required.issubset(statements_df.columns):
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    df = statements_df.copy()
    df[STMT_DISCLOSED_DATE] = pd.to_datetime(df[STMT_DISCLOSED_DATE], errors="coerce")
    df[STMT_PROFIT] = _to_numeric(df[STMT_PROFIT])
    df[STMT_FORECAST_PROFIT] = _to_numeric(df[STMT_FORECAST_PROFIT])
    df = df.sort_values([STMT_CODE, STMT_FY_END, STMT_DISCLOSED_DATE])

    # 同一銘柄・同一決算期内で、直前開示のForecastProfitを引いてくる
    df["prev_forecast_profit"] = df.groupby([STMT_CODE, STMT_FY_END])[STMT_FORECAST_PROFIT].shift(1)

    is_fy_actual = df[STMT_PERIOD_TYPE] == "FY"
    has_prev_forecast = df["prev_forecast_profit"].notna()
    beat = df[STMT_PROFIT] > (df["prev_forecast_profit"] * (1 + EARNINGS_BEAT_THRESHOLD))

    hit = df.loc[is_fy_actual & has_prev_forecast & beat].copy()
    hit["rule"] = "earnings_beat"
    hit["detail"] = (
        "通期純利益 実績" + hit[STMT_PROFIT].astype(str)
        + " > 直前会社予想" + hit["prev_forecast_profit"].astype(str)
    )
    return hit[[STMT_CODE, STMT_DISCLOSED_DATE, "rule", "detail"]].rename(
        columns={STMT_CODE: "Code", STMT_DISCLOSED_DATE: "Date"}
    )


def detect_equity_ratio(statements_df: pd.DataFrame) -> pd.DataFrame:
    """自己資本比率(EqAR)が閾値以上の開示を検出する（銘柄の現在の財務健全性チェック）。"""
    required = {STMT_CODE, STMT_DISCLOSED_DATE, STMT_EQUITY_RATIO}
    if statements_df.empty or not required.issubset(statements_df.columns):
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    df = statements_df.copy()
    df[STMT_EQUITY_RATIO] = _to_numeric(df[STMT_EQUITY_RATIO])
    hit = df.loc[df[STMT_EQUITY_RATIO] >= EQUITY_RATIO_THRESHOLD, [STMT_CODE, STMT_DISCLOSED_DATE, STMT_EQUITY_RATIO]].copy()
    hit["rule"] = "equity_ratio_high"
    hit["detail"] = "自己資本比率 " + (hit[STMT_EQUITY_RATIO] * 100).round(1).astype(str) + "%"
    return hit.drop(columns=[STMT_EQUITY_RATIO]).rename(columns={STMT_CODE: "Code", STMT_DISCLOSED_DATE: "Date"})


def detect_profit_doubling(statements_df: pd.DataFrame) -> pd.DataFrame:
    """本決算(FY)の経常利益(OdP)が、約PROFIT_DOUBLING_YEARS年前の本決算と比べて
    PROFIT_DOUBLING_MULTIPLE倍以上になった開示を検出する。
    """
    required = {STMT_CODE, STMT_PERIOD_TYPE, STMT_FY_END, STMT_DISCLOSED_DATE, STMT_ORDINARY_PROFIT}
    if statements_df.empty or not required.issubset(statements_df.columns):
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    df = statements_df.copy()
    df = df.loc[df[STMT_PERIOD_TYPE] == "FY"].copy()
    df[STMT_ORDINARY_PROFIT] = _to_numeric(df[STMT_ORDINARY_PROFIT])
    df = df.dropna(subset=[STMT_ORDINARY_PROFIT])
    df[STMT_FY_END] = pd.to_datetime(df[STMT_FY_END], errors="coerce")
    df = df.sort_values([STMT_CODE, STMT_FY_END])

    target_days = 365 * PROFIT_DOUBLING_YEARS
    tolerance_days = 45  # 決算期変更等のブレを許容する

    hits = []
    for code, group in df.groupby(STMT_CODE):
        group = group.reset_index(drop=True)
        for i in range(len(group)):
            cur = group.iloc[i]
            gap = (cur[STMT_FY_END] - group[STMT_FY_END]).dt.days
            candidates = group.loc[(gap - target_days).abs() <= tolerance_days]
            if candidates.empty:
                continue
            target_prev_date = cur[STMT_FY_END] - pd.Timedelta(days=target_days)
            best_idx = (candidates[STMT_FY_END] - target_prev_date).abs().idxmin()
            prev = candidates.loc[best_idx]
            if prev[STMT_ORDINARY_PROFIT] <= 0:
                continue
            if cur[STMT_ORDINARY_PROFIT] >= prev[STMT_ORDINARY_PROFIT] * PROFIT_DOUBLING_MULTIPLE:
                hits.append({
                    "Code": code,
                    "Date": cur[STMT_DISCLOSED_DATE],
                    "rule": "profit_doubling",
                    "detail": (
                        f"経常利益 {PROFIT_DOUBLING_YEARS}年前{prev[STMT_ORDINARY_PROFIT]:.0f}→"
                        f"今回{cur[STMT_ORDINARY_PROFIT]:.0f}"
                    ),
                })
    return pd.DataFrame(hits, columns=["Code", "Date", "rule", "detail"])


def detect_downward_revision(statements_df: pd.DataFrame) -> pd.DataFrame:
    """同一決算期(CurFYEn)について、会社予想の純利益(FNP)が前回開示時点より
    下がった（下方修正された）開示を検出する。これは除外フィルター用のマイナス
    シグナルとして扱う想定。
    """
    required = {STMT_CODE, STMT_FY_END, STMT_DISCLOSED_DATE, STMT_FORECAST_PROFIT}
    if statements_df.empty or not required.issubset(statements_df.columns):
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    df = statements_df.copy()
    df[STMT_DISCLOSED_DATE] = pd.to_datetime(df[STMT_DISCLOSED_DATE], errors="coerce")
    df[STMT_FORECAST_PROFIT] = _to_numeric(df[STMT_FORECAST_PROFIT])
    df = df.dropna(subset=[STMT_FORECAST_PROFIT])
    df = df.sort_values([STMT_CODE, STMT_FY_END, STMT_DISCLOSED_DATE])

    df["prev_forecast_profit"] = df.groupby([STMT_CODE, STMT_FY_END])[STMT_FORECAST_PROFIT].shift(1)
    has_prev = df["prev_forecast_profit"].notna()
    lowered = df[STMT_FORECAST_PROFIT] < (df["prev_forecast_profit"] * (1 - DOWNWARD_REVISION_THRESHOLD))

    hit = df.loc[has_prev & lowered].copy()
    hit["rule"] = "downward_revision"
    hit["detail"] = (
        "通期純利益予想 下方修正 " + hit["prev_forecast_profit"].astype(str)
        + " → " + hit[STMT_FORECAST_PROFIT].astype(str)
    )
    return hit[[STMT_CODE, STMT_DISCLOSED_DATE, "rule", "detail"]].rename(
        columns={STMT_CODE: "Code", STMT_DISCLOSED_DATE: "Date"}
    )


def detect_market_upgrade_to_prime(master_history_df: pd.DataFrame) -> pd.DataFrame:
    """上場銘柄マスタの日次履歴から、スタンダード/グロース市場からプライム市場への
    市場変更を検出する。equities/masterはdateパラメータで過去時点の市場区分を
    遡って返すため、期間内の日次スナップショットを比較して変更日を特定する。
    """
    required = {MASTER_CODE, MASTER_DATE, MASTER_MKT_NAME}
    if master_history_df.empty or not required.issubset(master_history_df.columns):
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    df = master_history_df.copy()
    df[MASTER_DATE] = pd.to_datetime(df[MASTER_DATE], errors="coerce")
    df = df.dropna(subset=[MASTER_DATE]).sort_values([MASTER_CODE, MASTER_DATE])
    df = df.drop_duplicates(subset=[MASTER_CODE, MASTER_DATE])

    df["prev_mkt"] = df.groupby(MASTER_CODE)[MASTER_MKT_NAME].shift(1)
    upgraded = df["prev_mkt"].isin(["スタンダード", "グロース"]) & (df[MASTER_MKT_NAME] == "プライム")

    hit = df.loc[upgraded, [MASTER_CODE, MASTER_DATE, "prev_mkt", MASTER_MKT_NAME]].copy()
    hit["rule"] = "market_upgrade_to_prime"
    hit["detail"] = hit["prev_mkt"] + " → " + hit[MASTER_MKT_NAME] + " へ市場変更"
    return hit[[MASTER_CODE, MASTER_DATE, "rule", "detail"]].rename(
        columns={MASTER_CODE: "Code", MASTER_DATE: "Date"}
    )


def detect_new_facility_or_store(disclosures_df: pd.DataFrame) -> pd.DataFrame:
    """TDnet開示タイトルから「新工場」「新店舗」に関連するキーワードを検出する。

    タイトルだけを見たキーワード一致であり、誤検出（例:本文が別内容）の可能性が
    ある。detail列に開示タイトルそのものを入れているので、実際に使う際は
    リンク先の開示文書で内容を確認すること。
    """
    required = {"company_code", "title", "pubdate"}
    if disclosures_df.empty or not required.issubset(disclosures_df.columns):
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    keywords = FACILITY_KEYWORDS + STORE_KEYWORDS
    df = disclosures_df.copy()
    titles = df["title"].fillna("")
    mentions_keyword = titles.apply(lambda t: any(k in t for k in keywords))
    mentions_exclusion = titles.apply(lambda t: any(k in t for k in FACILITY_STORE_EXCLUSION_KEYWORDS))
    mask = mentions_keyword & ~mentions_exclusion
    hit = df.loc[mask, ["company_code", "pubdate", "title"]].copy()
    if hit.empty:
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    hit["rule"] = "new_facility_or_store"
    hit["detail"] = "開示タイトル: " + hit["title"]
    hit["Date"] = pd.to_datetime(hit["pubdate"], errors="coerce")
    return hit[["company_code", "Date", "rule", "detail"]].rename(columns={"company_code": "Code"})


_CONJUNCTION_SPLIT_PATTERN = re.compile("及び|および|並びに|かつ")


def _is_regional_to_tokyo_transfer(title: str) -> bool:
    """タイトルを句に分割し、「地方取引所+上場廃止」と「東証+上場（廃止ではない）」
    が両方存在するかを見る。単に東証と地方取引所の名前が両方出てくるだけでは
    方向（東京→地方 の逆方向を含む）を区別できないため。
    """
    segments = _CONJUNCTION_SPLIT_PATTERN.split(title)
    leaving_regional = False
    joining_tokyo = False
    for seg in segments:
        has_delisting = "上場廃止" in seg
        if has_delisting and any(ex in seg for ex in REGIONAL_EXCHANGES):
            leaving_regional = True
        if not has_delisting and any(k in seg for k in TOKYO_EXCHANGE_KEYWORDS) and "上場" in seg:
            joining_tokyo = True
    return leaving_regional and joining_tokyo


def detect_exchange_transfer_to_tokyo(disclosures_df: pd.DataFrame) -> pd.DataFrame:
    """TDnet開示タイトルから、札幌・福岡・名古屋証券取引所から東証への上場・
    移籍に関連する開示を検出する（タイトルのキーワード一致のため、内容は
    リンク先で必ず確認すること）。

    タイトルを句で分割し、「地方取引所からの上場廃止」と「東証への上場」が
    両方あるかを見て方向を判定する（単に両方の取引所名が出てくるだけでは、
    逆方向の移籍や重複上場まで拾ってしまうため）。
    """
    required = {"company_code", "title", "pubdate"}
    if disclosures_df.empty or not required.issubset(disclosures_df.columns):
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    df = disclosures_df.copy()
    titles = df["title"].fillna("")
    mask = titles.apply(_is_regional_to_tokyo_transfer)
    hit = df.loc[mask, ["company_code", "pubdate", "title"]].copy()
    if hit.empty:
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    hit["rule"] = "exchange_transfer_to_tokyo"
    hit["detail"] = "開示タイトル: " + hit["title"]
    hit["Date"] = pd.to_datetime(hit["pubdate"], errors="coerce")
    return hit[["company_code", "Date", "rule", "detail"]].rename(columns={"company_code": "Code"})
