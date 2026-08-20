"""地方証券取引所（札幌・福岡・名古屋）単独上場企業の検出・追跡。

J-Quantsは東証(TSE)とTOKYO PRO Marketのみが対象で、地方取引所独自市場
（Q-Board、アンビシャス等）の銘柄は上場銘柄マスタ・株価データから完全に
除外される（CLAUDE.md「データ対象範囲の制約」参照）。そのため本モジュールは
J-Quantsを使わず、TDnet開示の`markets_string`列（開示時点でどの取引所に
上場していたかを示す1〜数文字: 東=東証, 福=福証, 名=名証, 札=札証。
実データで確認済み、2026-08-13）を手がかりに地方単独上場企業を特定する。

検出できること:
  - 地方単独上場企業の新規上場・重複上場（申請/承認/実施の段階を区別）
  - 東証への新規上場・重複上場・市場変更（最重要イベントとして区別）
  - M&A・TOB・資本業務提携等の大型企業イベント

検出できないこと（既知の制約、誤った推測値を出さないためあえて空欄にする）:
  - 地方単独上場企業の時価総額はJ-Quantsに株価データが無いため計算不能。
    福証単独上場企業のみyfinanceの`.F`サフィックスで現在値を取得できることを
    実機確認済み（2026-08-13、9388で確認）。名証・札証単独上場企業や、
    新規上場直後の英数字コード銘柄（482A0等）はyfinanceでも取得できない
    ことを同日に確認済みのため、その場合は時価総額を「取得不可」とする。

毎回全期間のTDnetデータを再走査すると重くなるため、前回スキャン済み日付
（ウォーターマーク）をキャッシュに保存し、次回はその翌日からだけ追加取得する
（初回のみREGIONAL_LISTING_LOOKBACK_YEARS年分を遡る）。
"""
from __future__ import annotations

import datetime as dt
import logging

import pandas as pd

from src import cache, rules, tdnet_client, tdnet_xbrl
from src.config import (
    REGIONAL_LISTING_LOOKBACK_YEARS,
    REGIONAL_STATEMENTS_LOOKBACK_DAYS,
)
from src.jst import today_jst
from src.pipeline import RULE_LABELS

# 地方単独上場企業でも判定できる条件（株価が必要なストップ高・PBRは対象外。
# 福証単独上場企業に限り別途対応予定）。財務諸表系はfetch_regional_statements()の
# statements_df、TDnet開示タイトル系は通常のdisclosures_dfをそのまま使う。
REGIONAL_STATEMENT_RULES = [
    "sales_growth_major", "sales_growth_explosive", "equity_ratio_high",
    "two_quarter_growth", "earnings_beat", "profit_doubling",
]
REGIONAL_TITLE_RULES = ["new_facility_or_store", "world_first", "large_order", "stock_split"]
REGIONAL_NEGATIVE_RULE = "downward_revision"
REGIONAL_APPLICABLE_RULES = REGIONAL_STATEMENT_RULES + REGIONAL_TITLE_RULES

logger = logging.getLogger(__name__)

REGIONAL_MARKET_LABELS = {"福": "福証", "名": "名証", "札": "札証"}
TOKYO_MARKET_CHAR = "東"

_STORE_ENDPOINT = "regional_stocks"
_COMPANY_STATUS_KEY = "company_status"
_LISTING_EVENTS_KEY = "listing_events"
_MAJOR_EVENTS_KEY = "major_events"
_STATEMENTS_KEY = "statements"
_WATERMARK_KEY = "watermark"
# 財務諸表(statements)専用のウォーターマーク。上場イベント等の既存
# ウォーターマーク(_WATERMARK_KEY)とは別に持つ。この機能を後から追加した
# 既存デプロイでは、_WATERMARK_KEYが既に最新付近まで進んでいるため、
# 共用すると初回実行時にREGIONAL_STATEMENTS_LOOKBACK_DAYS分の遡り取得が
# 行われず、その時点でまだ取得可能だった直近の決算短信を永久に取り逃す
# （2026-08-19の3巡目のCodexレビューで指摘）。
_STATEMENTS_WATERMARK_KEY = "statements_watermark"

# yfinanceで現在値取得を実機確認済みのサフィックス（2026-08-13、9388で確認）。
# 名証・札証単独上場企業は同日に実機確認の上、同サフィックス方式では取得できな
# かったため対象外（取得不可のまま扱う。README参照）。
_YFINANCE_SUFFIX_BY_MARKET = {"福": "F"}

# 株価に大きな影響を与えうる大型企業イベントのキーワード（ユーザー指定）。
# タイトルのキーワード一致による検出のため、実際の内容はUrl先で必ず確認すること
# （既存のTDNET_TITLE_BASED_RULESと同じ注意点）。
MAJOR_EVENT_KEYWORDS = [
    "M&A", "買収", "子会社化", "TOB", "公開買付け", "公開買付", "MBO", "資本業務提携", "出資",
    "大型受注", "大型契約",
]

_LISTING_KEYWORD = "上場"
_DELISTING_KEYWORD = "上場廃止"
_APPLICATION_KEYWORDS = ["申請"]
_APPROVAL_KEYWORDS = ["承認"]
_TOKYO_KEYWORDS = ["東京証券取引所", "東証"]
_MARKET_NAME_KEYWORDS = ["東京証券取引所", "名古屋証券取引所", "札幌証券取引所", "福岡証券取引所"]

_LISTING_EVENTS_COLUMNS = [
    "id", "Code", "CompanyName", "Date", "MarketsString", "TargetMarkets",
    "Stage", "IsTokyoRelated", "Title", "Url",
]
_MAJOR_EVENTS_COLUMNS = [
    "id", "Code", "CompanyName", "Date", "MarketsString", "Title", "Url", "MatchedKeyword",
]
_COMPANY_STATUS_COLUMNS = [
    "Code", "CompanyName", "MarketsString", "LastSeenDate", "LastDelistingDate",
    "IsDelisted", "CurrentPrice", "CurrentPriceNote",
]
# src.rules内の各detect_*関数が前提とするJ-Quants由来の列名(STMT_*)と同じ形。
# 財務条件(売上高増加・自己資本比率等)をrules.pyのロジックそのまま流用するため。
# IsPrimaryはJ-Quants側には無い、tdnet_xbrl.py固有の列（実際の開示行か、開示に
# 埋め込まれた前年同期実績・翌期予想の合成行かの区別。tdnet_xbrl.pyのモジュール
# docstring参照）。
_STATEMENTS_COLUMNS = [
    "id", "Code", "DiscDate", "CurPerType", "CurPerEn", "CurFYEn",
    "Sales", "OP", "OdP", "NP", "EqAR", "FSales", "FOP", "FNP", "BPS", "IsPrimary",
]
_DECISION_TANSHIN_TITLE_KEYWORD = "決算短信"

_REQUIRED_DISCLOSURE_COLUMNS = {
    "id", "company_code", "company_name", "title", "pubdate", "markets_string", "document_url",
}



def is_regional_only(markets_string) -> bool:
    """開示時点でのmarkets_stringが「東証を含まない」=地方単独上場かどうか。

    markets_string列が欠損している開示ではpandasがNaN(float)を入れてくる
    ことがあり、`bool(NaN)`はTrueのため`not markets_string`では弾けず、
    後続の`in`演算がTypeErrorになる。isinstance(str)を明示的に確認する。
    """
    if not isinstance(markets_string, str) or not markets_string:
        return False
    return TOKYO_MARKET_CHAR not in markets_string


def regional_markets_in(markets_string) -> list[str]:
    """markets_stringに含まれる地方取引所名（表示用）を出現順に返す。"""
    if not isinstance(markets_string, str) or not markets_string:
        return []
    return [label for char, label in REGIONAL_MARKET_LABELS.items() if char in markets_string]


_ALL_MARKET_LABELS = {TOKYO_MARKET_CHAR: "東証", **REGIONAL_MARKET_LABELS}


def all_markets_in(markets_string) -> list[str]:
    """markets_stringに含まれる取引所名（東証も含む、表示用）を出現順に返す。

    地方単独上場から東証への移籍が完了した開示は、その時点でmarkets_string
    に"東"も含まれる（例:"東福"）。regional_markets_in()は地方取引所名しか
    返さないため、この関数を使わないと表示上「まだ東証には上場していない」
    ように誤って見えてしまう。
    """
    if not isinstance(markets_string, str) or not markets_string:
        return []
    return [label for char, label in _ALL_MARKET_LABELS.items() if char in markets_string]


def _legacy_ticker(code: str) -> str:
    """J-Quants/TDnetの5桁コード（4桁+普通株式サフィックス"0"）から、
    yfinance等で使う4桁（英数字含む）の実質コードを取り出す。

    末尾の"0"を`rstrip("0")`で取り除くと、実コード自体が0で終わる場合
    （例: 実コード"7200"→5桁"72000"）に複数文字削られてしまうため、
    固定で先頭4文字を使う。
    """
    return code[:4] if len(code) >= 5 else code


def _classify_listing_stage(title: str) -> str | None:
    """上場関連の開示タイトルから段階（申請/承認/上場）を判定する。
    上場に無関係なタイトルはNoneを返す。

    「上場廃止申請」のように"上場"と"申請"を含むが実際は上場廃止（＝新規上場
    の逆方向）の開示は、誤って新規上場の「申請」段階と判定しないよう先に除外する。
    """
    if _DELISTING_KEYWORD in title:
        return None
    if _LISTING_KEYWORD not in title:
        return None
    # 承認を先に判定する。「上場申請の承認に関するお知らせ」のように、
    # 承認の対象として"申請"という語が埋め込まれているタイトルがあり、
    # 申請を先に判定すると承認済みの開示を格下げしてしまうため。
    if any(k in title for k in _APPROVAL_KEYWORDS):
        return "承認"
    if any(k in title for k in _APPLICATION_KEYWORDS):
        return "申請"
    if "上場のお知らせ" in title or "上場に関するお知らせ" in title or "上場について" in title:
        return "上場"
    return None


def _filter_regional_only(disclosures_df: pd.DataFrame) -> pd.DataFrame:
    if disclosures_df.empty or not _REQUIRED_DISCLOSURE_COLUMNS.issubset(disclosures_df.columns):
        return pd.DataFrame(columns=list(_REQUIRED_DISCLOSURE_COLUMNS))
    df = disclosures_df.copy()
    return df.loc[df["markets_string"].apply(is_regional_only)]


def compute_was_known_regional(disclosures_df: pd.DataFrame, base_known_codes: set[str] | None = None) -> pd.Series:
    """各開示の時点で、その開示より前の情報から「地方単独上場と判明している」
    銘柄かどうかを表すbool列(disclosures_dfと同じindex)を返す。

    disclosures_df全体から単純に「地方単独上場の行が1件でもあるコード」を
    集めるだけでは、時系列を無視してしまう（例:同じバッチ内で後の方に
    地方単独上場の開示がある銘柄について、それより前にある無関係な東証の
    開示まで誤って「地方単独上場からの移籍」と判定してしまう）。日付の
    昇順で1件ずつ走査し、その時点までに分かっている集合だけを使う。
    """
    if disclosures_df.empty:
        return pd.Series(dtype=bool)
    base_known_codes = base_known_codes or set()
    dates = pd.to_datetime(disclosures_df["pubdate"], errors="coerce")
    order = dates.sort_values(kind="stable").index
    is_regional_row = disclosures_df["markets_string"].apply(is_regional_only)
    known = set(base_known_codes)
    result = pd.Series(False, index=disclosures_df.index)
    for idx in order:
        code = str(disclosures_df.at[idx, "company_code"])
        result.at[idx] = code in known
        if is_regional_row.at[idx]:
            known.add(code)
    return result


def detect_regional_listing_events(
    disclosures_df: pd.DataFrame, was_known_regional: pd.Series | None = None
) -> pd.DataFrame:
    """地方単独上場企業の新規上場・重複上場・市場変更関連の開示を検出する。

    東京証券取引所への上場申請・承認・上場に関するものはIsTokyoRelated=True
    として区別する（②の「最重要イベント」向け）。

    東証への上場が実際に完了した開示は、その時点でmarkets_stringにもう
    「東」が含まれてしまう（=その開示自体は地方単独上場ではない）ため、
    markets_stringだけで絞り込むと肝心の「上場完了」の開示を取り逃す。
    was_known_regional（compute_was_known_regional()の戻り値。disclosures_df
    と同じindexを持つbool列）で、その開示より前の時点で地方単独上場と
    分かっていた銘柄かどうかを渡すことで、そのようなmarkets_string変化後の
    開示も対象にする。
    """
    if disclosures_df.empty or not _REQUIRED_DISCLOSURE_COLUMNS.issubset(disclosures_df.columns):
        return pd.DataFrame(columns=_LISTING_EVENTS_COLUMNS)

    df = disclosures_df.copy()
    is_currently_regional = df["markets_string"].apply(is_regional_only)
    titles_for_scope = df["title"].fillna("")
    is_tokyo_titled = titles_for_scope.apply(lambda t: any(k in t for k in _TOKYO_KEYWORDS))
    was_known_regional = (
        was_known_regional.reindex(df.index, fill_value=False)
        if was_known_regional is not None
        else pd.Series(False, index=df.index)
    )
    in_scope = is_currently_regional | (is_tokyo_titled & was_known_regional)

    regional = df.loc[in_scope]
    if regional.empty:
        return pd.DataFrame(columns=_LISTING_EVENTS_COLUMNS)

    stages = regional["title"].fillna("").apply(_classify_listing_stage)
    hit = regional.loc[stages.notna()].copy()
    if hit.empty:
        return pd.DataFrame(columns=_LISTING_EVENTS_COLUMNS)

    hit["Stage"] = stages.loc[hit.index]
    hit["IsTokyoRelated"] = hit["title"].fillna("").apply(lambda t: any(k in t for k in _TOKYO_KEYWORDS))
    hit["TargetMarkets"] = hit["title"].fillna("").apply(
        lambda t: [name for name in _MARKET_NAME_KEYWORDS if name in t]
    )
    hit["Date"] = pd.to_datetime(hit["pubdate"], errors="coerce")

    result = hit.rename(columns={
        "company_code": "Code",
        "company_name": "CompanyName",
        "markets_string": "MarketsString",
        "title": "Title",
        "document_url": "Url",
    })
    return result[_LISTING_EVENTS_COLUMNS].reset_index(drop=True)


def detect_regional_major_events(disclosures_df: pd.DataFrame) -> pd.DataFrame:
    """地方単独上場企業のM&A・TOB・資本業務提携等、株価に影響しうる大型イベントを検出する。

    タイトルのキーワード一致による検出のため、実際の内容はUrl先で必ず確認すること。
    """
    regional = _filter_regional_only(disclosures_df)
    if regional.empty:
        return pd.DataFrame(columns=_MAJOR_EVENTS_COLUMNS)

    titles = regional["title"].fillna("")
    matched_keyword = titles.apply(lambda t: next((k for k in MAJOR_EVENT_KEYWORDS if k in t), None))
    hit = regional.loc[matched_keyword.notna()].copy()
    if hit.empty:
        return pd.DataFrame(columns=_MAJOR_EVENTS_COLUMNS)

    hit["MatchedKeyword"] = matched_keyword.loc[hit.index]
    hit["Date"] = pd.to_datetime(hit["pubdate"], errors="coerce")
    result = hit.rename(columns={
        "company_code": "Code",
        "company_name": "CompanyName",
        "markets_string": "MarketsString",
        "title": "Title",
        "document_url": "Url",
    })
    return result[_MAJOR_EVENTS_COLUMNS].reset_index(drop=True)


def fetch_regional_statements(
    disclosures_df: pd.DataFrame,
    today: dt.date | None = None,
    was_known_regional: pd.Series | None = None,
) -> pd.DataFrame:
    """地方単独上場企業の決算短信からXBRLサマリー情報を取得し、rules.pyの
    各detect_*関数がそのまま使えるSTMT_*列のDataFrameを返す（当期行＋前年同期行。
    src/tdnet_xbrl.py参照）。

    TDnetの開示添付ファイルは公開から約1〜1.5ヶ月で取得できなくなるため
    （src/tdnet_xbrl.py参照）、REGIONAL_STATEMENTS_LOOKBACK_DAYSより古い開示は
    どうせ404になるだけなので、無駄なHTTPリクエストを避けるため最初から
    対象外にする（新規上場・大型イベント検出用のREGIONAL_LISTING_LOOKBACK_YEARS
    とは別軸の制約）。

    was_known_regional（compute_was_known_regional()の戻り値）を渡すと、
    markets_stringが欠損している決算短信も、直前まで地方単独上場と分かって
    いた銘柄であれば対象に含める。欠損時にそのまま除外すると、
    update_regional_store()のウォーターマークが進んだ後は同じ開示が二度と
    対象にならず、その四半期の財務データが永久に欠落する
    （_latest_company_status()/_delisting_dates_by_code()で既に同種の欠損に
    対応済みのパターンと同じ。2026-08-19のCodexレビューで指摘）。
    """
    if disclosures_df.empty or not _REQUIRED_DISCLOSURE_COLUMNS.issubset(disclosures_df.columns):
        return pd.DataFrame(columns=_STATEMENTS_COLUMNS)

    today = today or today_jst()
    cutoff = today - dt.timedelta(days=REGIONAL_STATEMENTS_LOOKBACK_DAYS)

    df = disclosures_df.copy()
    df["_pubdate_parsed"] = pd.to_datetime(df["pubdate"], errors="coerce")
    is_regional = df["markets_string"].apply(is_regional_only)
    if was_known_regional is not None:
        is_regional = is_regional | was_known_regional.reindex(df.index, fill_value=False)
    is_tanshin = df["title"].fillna("").str.contains(_DECISION_TANSHIN_TITLE_KEYWORD)
    has_xbrl = df["url_xbrl"].notna() if "url_xbrl" in df.columns else False
    is_recent = df["_pubdate_parsed"].dt.date >= cutoff
    target = df.loc[is_regional & is_tanshin & has_xbrl & is_recent]
    if target.empty:
        return pd.DataFrame(columns=_STATEMENTS_COLUMNS)

    rows: list[dict] = []
    for _, r in target.iterrows():
        disclosed_date = r["_pubdate_parsed"].date() if pd.notna(r["_pubdate_parsed"]) else None
        try:
            fetched = tdnet_xbrl.fetch_tanshin_statement_rows(r["url_xbrl"], str(r["company_code"]), disclosed_date)
        except Exception as exc:  # noqa: BLE001 -- 個別の決算短信1件のXBRL取得・パース失敗で全体を止めない
            logger.info("[%s] 決算短信XBRLの取得に失敗しました: %s", r["company_code"], exc)
            continue
        for i, row in enumerate(fetched):
            row["id"] = f"{r['id']}_{i}"
            rows.append(row)

    if not rows:
        return pd.DataFrame(columns=_STATEMENTS_COLUMNS)
    return pd.DataFrame(rows)[_STATEMENTS_COLUMNS]


def _latest_primary_statements(statements_df: pd.DataFrame) -> pd.DataFrame:
    """銘柄ごとに最新の実際の開示行(IsPrimary=True。列が無ければ全行)だけを残す。

    自己資本比率のような「現在の財務健全性」を見る判定に使う。開示に埋め込
    まれた前年同期の実績や、過去の開示がたまたま閾値を満たしていても、
    現在の状態としては扱わないようにするため（本決算のprior_rowも
    EqAR/BPSを持つため、cur_rowが閾値未満でもprior_rowが閾値以上だと誤って
    「現在も健全」と判定してしまうケースがあった。2026-08-19のCodexレビューで
    指摘、実データで確認）。
    """
    if statements_df.empty:
        return statements_df
    df = statements_df
    if "IsPrimary" in df.columns:
        df = df.loc[df["IsPrimary"].fillna(True).astype(bool)]
    if df.empty or "DiscDate" not in df.columns or "Code" not in df.columns:
        return df
    df = df.copy()
    df["_disc_date_parsed"] = pd.to_datetime(df["DiscDate"], errors="coerce")
    df = df.sort_values("_disc_date_parsed")
    return df.groupby("Code", as_index=False, group_keys=False).tail(1).drop(columns=["_disc_date_parsed"])


def _currently_regional_codes(company_status_df: pd.DataFrame) -> set[str] | None:
    """company_status_dfから、現在も地方単独上場のまま（東証移籍済み・上場廃止
    済みではない）銘柄コードの集合を返す。company_status_dfが空の場合は
    絞り込まない(None、企業ステータス情報が無い状態で誤って全件除外しないため)。
    """
    if company_status_df.empty:
        return None
    if "IsDelisted" in company_status_df.columns:
        is_delisted = company_status_df["IsDelisted"].fillna(False).astype(bool)
    else:
        is_delisted = pd.Series(False, index=company_status_df.index)
    still_regional = company_status_df["MarketsString"].apply(is_regional_only) & ~is_delisted
    return set(company_status_df.loc[still_regional, "Code"].astype(str))


def screen_regional(
    disclosures_df: pd.DataFrame,
    statements_df: pd.DataFrame,
    company_status_df: pd.DataFrame,
    selected_rules: list[str],
) -> pd.DataFrame:
    """地方単独上場企業について、REGIONAL_APPLICABLE_RULES(+除外用の
    downward_revision)のうちselected_rulesに含まれる条件だけをsrc.rules.pyの
    各detect_*関数でそのまま判定する。

    戻り値はsrc.pipeline.run_screening()と同じ列形状(Code, CompanyName,
    Sector, Rule, RuleLabel, Date, Detail)のため、pipeline.build_summary()に
    そのまま渡せる（Sectorは業種データが無いため常に空文字）。

    株価が必要な条件(ストップ高・PBR)は地方単独上場企業では判定できない
    （福証単独上場企業のみ株価取得可能）ため対象外。

    statements_dfは、東証移籍済み・上場廃止済みの銘柄の過去分も蓄積され続ける
    ため、company_status_df上で現在も地方単独上場と分かる銘柄だけに絞り込んで
    から判定する（そうしないと、既に東証に移籍した銘柄の古い財務データが
    「地方単独上場企業のみ」の結果に紛れ込む。2026-08-19のCodexレビューで指摘）。
    """
    eligible_codes = _currently_regional_codes(company_status_df)
    if eligible_codes is not None and not statements_df.empty:
        statements_df = statements_df.loc[statements_df["Code"].astype(str).isin(eligible_codes)]

    hits = []
    if any(r in selected_rules for r in ("sales_growth_major", "sales_growth_explosive")):
        hits.append(rules.detect_sales_growth(statements_df))
    if "earnings_beat" in selected_rules:
        hits.append(rules.detect_earnings_beat(statements_df))
    if "equity_ratio_high" in selected_rules:
        hits.append(rules.detect_equity_ratio(_latest_primary_statements(statements_df)))
    if "profit_doubling" in selected_rules:
        hits.append(rules.detect_profit_doubling(statements_df))
    if "two_quarter_growth" in selected_rules:
        hits.append(rules.detect_two_quarter_growth(statements_df))
    if REGIONAL_NEGATIVE_RULE in selected_rules:
        hits.append(rules.detect_downward_revision(statements_df))
    if "new_facility_or_store" in selected_rules:
        hits.append(rules.detect_new_facility_or_store(disclosures_df))
    if "world_first" in selected_rules:
        hits.append(rules.detect_world_first(disclosures_df))
    if "large_order" in selected_rules:
        hits.append(rules.detect_large_order(disclosures_df))
    if "stock_split" in selected_rules:
        hits.append(rules.detect_stock_split(disclosures_df))

    columns = ["Code", "CompanyName", "Sector", "Rule", "RuleLabel", "Date", "Detail"]
    hits = [h for h in hits if not h.empty]
    if not hits:
        return pd.DataFrame(columns=columns)

    result = pd.concat(hits, ignore_index=True)
    result = result.rename(columns={"rule": "Rule", "detail": "Detail"})
    result["Code"] = result["Code"].astype(str)

    name_map: dict[str, str] = {}
    if not company_status_df.empty:
        name_map = dict(zip(company_status_df["Code"], company_status_df["CompanyName"]))
    result["CompanyName"] = result["Code"].map(name_map).fillna("")
    result["Sector"] = ""
    result["RuleLabel"] = result["Rule"].map(RULE_LABELS).fillna(result["Rule"])
    result["Date"] = pd.to_datetime(result["Date"])
    result = result.sort_values(["Date", "Code"]).reset_index(drop=True)
    return result[columns]


_STATUS_COLUMNS = ["Code", "CompanyName", "MarketsString", "LastSeenDate"]


def _in_scope_mask(disclosures_df: pd.DataFrame, was_known_regional: pd.Series | None) -> pd.Series:
    """地方単独上場の開示、またはwas_known_regional（直前まで地方単独上場
    だった銘柄）の開示かどうかを返す（markets_stringの有無は問わない）。
    """
    if disclosures_df.empty:
        return pd.Series(dtype=bool)
    is_currently_regional = disclosures_df["markets_string"].apply(is_regional_only)
    was_known_regional = (
        was_known_regional.reindex(disclosures_df.index, fill_value=False)
        if was_known_regional is not None
        else pd.Series(False, index=disclosures_df.index)
    )
    return is_currently_regional | was_known_regional


def _delisting_dates_by_code(disclosures_df: pd.DataFrame, was_known_regional: pd.Series | None = None) -> dict:
    """地方単独上場（またはwas_known_regionalで分かる直前まで地方単独上場
    だった）銘柄について、上場廃止関連開示の最終日をコードごとに返す。

    markets_stringの有無は問わない。上場廃止の開示自体でmarkets_stringが
    欠損していても上場廃止の事実を取り逃さないようにするため
    （_latest_company_status()のhas_valid_markets要件とは意図的に独立させている）。
    """
    if disclosures_df.empty or not _REQUIRED_DISCLOSURE_COLUMNS.issubset(disclosures_df.columns):
        return {}
    in_scope = disclosures_df.loc[_in_scope_mask(disclosures_df, was_known_regional)]
    if in_scope.empty:
        return {}
    dates = pd.to_datetime(in_scope["pubdate"], errors="coerce")
    is_delisting_title = in_scope["title"].fillna("").str.contains(_DELISTING_KEYWORD)
    delisting_dates = dates.where(is_delisting_title).dropna()
    if delisting_dates.empty:
        return {}
    return delisting_dates.groupby(in_scope["company_code"].astype(str)).max().to_dict()


def _latest_company_status(
    disclosures_df: pd.DataFrame, was_known_regional: pd.Series | None = None
) -> pd.DataFrame:
    """銘柄ごとの最新のmarkets_string・会社名・最終確認日を集計する。

    地方単独上場の開示に加え、was_known_regional（compute_was_known_regional()
    の戻り値。直前まで地方単独上場だった銘柄）が東証を含む市場に移った開示も
    対象にする。そうしないと、東証移籍後もmarkets_stringが移籍前のまま
    更新されず、既に地方単独上場ではなくなった銘柄が地方株一覧に残り続ける。

    markets_stringが欠損している開示（会社コードは分かるがこの項目だけ
    空の回）は、有効な現在値を上書きしてしまわないよう対象から除外する
    （latestの選定に使わない。その回自体は無視され、直前の有効な値が
    そのまま保持される）。上場廃止関連開示の最終日は_delisting_dates_by_code()
    が別途、markets_stringの有無に関わらず集計する（上場廃止時点の開示に
    markets_stringが欠損していてもLastDelistingDateを取り逃さないため）。
    """
    if disclosures_df.empty or not _REQUIRED_DISCLOSURE_COLUMNS.issubset(disclosures_df.columns):
        return pd.DataFrame(columns=_STATUS_COLUMNS)

    has_valid_markets = disclosures_df["markets_string"].apply(lambda m: isinstance(m, str) and bool(m))
    regional = disclosures_df.loc[has_valid_markets & _in_scope_mask(disclosures_df, was_known_regional)]
    if regional.empty:
        return pd.DataFrame(columns=_STATUS_COLUMNS)

    df = regional.copy()
    df["Date"] = pd.to_datetime(df["pubdate"], errors="coerce")
    df = df.sort_values("Date")
    latest = df.groupby("company_code").tail(1).copy()
    return latest.rename(columns={
        "company_code": "Code",
        "company_name": "CompanyName",
        "markets_string": "MarketsString",
        "Date": "LastSeenDate",
    })[_STATUS_COLUMNS].reset_index(drop=True)


def fetch_regional_share_price(code: str, markets_string: str) -> tuple[float | None, str]:
    """地方単独上場企業の現在の株価を試みる（時価総額ではない）。

    発行済株式数を取得できる手段が無いため、時価総額(株価×株式数)は計算
    できない。株価だけを時価総額として見せると誤った金額として読まれる
    リスクがあるため、この関数は明確に「株価」だけを返す
    （呼び出し側でも時価総額としては表示しないこと）。
    取得できない場合はNoneと理由文字列を返す（誤った推測値を出さない）。
    """
    suffix = next((s for char, s in _YFINANCE_SUFFIX_BY_MARKET.items() if char in (markets_string or "")), None)
    if suffix is None:
        return None, "取得不可（この取引所の銘柄はyfinanceでも株価が取得できないことを確認済み）"

    ticker = f"{_legacy_ticker(code)}.{suffix}"
    try:
        import yfinance as yf

        price = yf.Ticker(ticker).fast_info.get("lastPrice")
    except Exception as exc:  # noqa: BLE001 -- 銘柄によって取得できないことが前提のため、失敗しても取得不可として継続する
        logger.info("[%s] yfinance(%s)での株価取得に失敗しました: %s", code, ticker, exc)
        return None, "取得不可（yfinance取得エラー）"

    if price is None or pd.isna(price):
        return None, "取得不可（yfinanceに株価データなし）"
    return float(price), ""


def _load_watermark() -> dt.date | None:
    df = cache.load(_STORE_ENDPOINT, _WATERMARK_KEY)
    if df is None or df.empty:
        return None
    return pd.to_datetime(df.iloc[0]["date"]).date()


def _save_watermark(date: dt.date) -> None:
    cache.save(_STORE_ENDPOINT, _WATERMARK_KEY, pd.DataFrame({"date": [date.isoformat()]}))


def _load_statements_watermark() -> dt.date | None:
    df = cache.load(_STORE_ENDPOINT, _STATEMENTS_WATERMARK_KEY)
    if df is None or df.empty:
        return None
    return pd.to_datetime(df.iloc[0]["date"]).date()


def _save_statements_watermark(date: dt.date) -> None:
    cache.save(_STORE_ENDPOINT, _STATEMENTS_WATERMARK_KEY, pd.DataFrame({"date": [date.isoformat()]}))


def _load_table(key: str, columns: list[str]) -> pd.DataFrame:
    """保存済みテーブルを読む。0件で保存された場合、cache.load()は列情報の
    無い空のDataFrameを返す（cache.pyが空フレームを列無しのマーカー形式で
    保存する仕様のため）。列名を前提にした後続処理がKeyErrorにならないよう、
    その場合も含めてcolumnsを再構成する。
    """
    df = cache.load(_STORE_ENDPOINT, key)
    if df is None or df.empty:
        return pd.DataFrame(columns=columns)
    return df


def load_regional_store() -> dict[str, pd.DataFrame]:
    """更新はせず、保存済みの検出結果だけを読む（Streamlitページの通常表示用）。"""
    return {
        "company_status": _load_table(_COMPANY_STATUS_KEY, _COMPANY_STATUS_COLUMNS),
        "listing_events": _load_table(_LISTING_EVENTS_KEY, _LISTING_EVENTS_COLUMNS),
        "major_events": _load_table(_MAJOR_EVENTS_KEY, _MAJOR_EVENTS_COLUMNS),
        "statements": _load_table(_STATEMENTS_KEY, _STATEMENTS_COLUMNS),
    }


def _dedupe_superseded_statements(statements_df: pd.DataFrame) -> pd.DataFrame:
    """同一銘柄・同一決算期(CurPerType, CurPerEn)について実際の開示行
    (IsPrimary=True)が複数ある場合、最新のDiscDateの行だけを残す（訂正決算短信
    はTDnet開示としては別ID・別行になるため、id基準の重複排除だけでは元の
    数値の行が残ってしまう。2026-08-19のCodexレビューで指摘、実データで
    「(訂正・数値データ訂正)」開示の存在を確認済み）。

    置き換えられた(古い方の)実際の開示行と同じ(Code, DiscDate)を持つ
    IsPrimary=False行（その開示に埋め込まれていた前年同期実績・翌期予想の
    合成行）も一緒に取り除く。1回のparse_tanshin_summary_rows()呼び出しの
    出力（cur_row+prior_row+guidance_row）は必ず同じDiscDateを共有するため、
    (Code, DiscDate)でどの開示由来かを一意に紐付けられる。これをしないと、
    訂正前の誤った翌期予想がguidance_rowとして残り続け、訂正後の正しい予想と
    比較したdetect_downward_revision()が「訂正で数値が直っただけ」を実際の
    下方修正と誤検知しうる（2026-08-19の3巡目のCodexレビューで指摘:
    前回の修正はIsPrimary=True行だけを差し替え、この合成行の後始末が
    漏れていた）。

    翌年の開示のprior_rowと今年の開示のcur_rowが同じCurPerEnを指す正当な
    ケースは、DiscDateが異なる（別々の開示イベント）ため、この処理では
    誤って統合されない。
    """
    if statements_df.empty or "IsPrimary" not in statements_df.columns:
        return statements_df
    # 空のDataFrame(cache未初期化時のプレースホルダ)とconcatすると、"IsPrimary"列
    # がbool dtypeではなくobject dtypeになることがある。object dtypeのまま
    # ~is_primaryを行うと、pandasが要素ごとのbitwise notとして扱い、
    # PythonのTrueはint(1)扱いのため~True=-2という数値になってしまい、
    # 真偽マスクではなく行ラベルの一覧として誤って解釈されKeyErrorになる
    # （2026-08-19の3巡目のCodexレビューへの対応中に実際に発生させて発見）。
    # astype(bool)で明示的に真偽値dtypeへ変換してから使う。
    is_primary = statements_df["IsPrimary"].fillna(True).astype(bool)
    primary = statements_df.loc[is_primary].copy()
    other = statements_df.loc[~is_primary]
    if primary.empty:
        return statements_df

    primary["_disc_date_parsed"] = pd.to_datetime(primary["DiscDate"], errors="coerce")
    primary = primary.sort_values("_disc_date_parsed")
    kept_primary = primary.groupby(
        ["Code", "CurPerType", "CurPerEn"], as_index=False, group_keys=False, dropna=False
    ).tail(1)
    superseded = primary.loc[~primary["id"].isin(kept_primary["id"])]
    kept_primary = kept_primary.drop(columns=["_disc_date_parsed"])

    if not superseded.empty and not other.empty:
        superseded_keys = set(zip(superseded["Code"], superseded["DiscDate"]))
        other = other.loc[~other.apply(lambda r: (r["Code"], r["DiscDate"]) in superseded_keys, axis=1)]

    return pd.concat([kept_primary, other], ignore_index=True)


def update_regional_store(today: dt.date | None = None) -> dict[str, pd.DataFrame]:
    """前回スキャン済み日付の翌日から今日までのTDnet開示だけを追加取得し、
    地方単独上場企業の状況・上場イベント・大型イベントの保存済みデータに
    追記して返す（初回はREGIONAL_LISTING_LOOKBACK_YEARS年分を遡って取得）。
    """
    today = today or today_jst()
    watermark = _load_watermark()
    start = (
        watermark + dt.timedelta(days=1)
        if watermark is not None
        else today - dt.timedelta(days=365 * REGIONAL_LISTING_LOOKBACK_YEARS)
    )

    stores = load_regional_store()
    if start > today:
        return stores

    # 当日分のTDnet開示はまだ全件公開されていない可能性がある。tdnet_client
    # は期間指定をそのままキャッシュキーにするため、当日を含む期間を一度
    # キャッシュしてしまうと同じボタンをその日のうちに何度押しても同じ
    # 不完全な結果を返し続けてしまう。前日までとは別に、当日分だけ
    # force_refresh=Trueで毎回取り直す。
    yesterday = today - dt.timedelta(days=1)
    stable_disclosures = tdnet_client.get_disclosures_range(start, yesterday)
    todays_disclosures = tdnet_client.get_disclosures_range(today, today, force_refresh=True)
    new_disclosures = pd.concat([stable_disclosures, todays_disclosures], ignore_index=True)

    # was_known_regionalは「これまでに保存済みの地方単独上場銘柄」に加え、
    # 今回まとめて取得した分（特に初回の複数年ブートストラップ）の中で
    # それより前の日付に地方単独上場と分かった銘柄も反映する（日付の昇順で
    # 判定するため、バッチ内で後から分かった分をそれより前の行に誤って
    # 適用することはない）。そうしないと、同じバッチ内で地方単独上場→
    # 東証移籍完了まで進んだ銘柄の完了開示を取り逃す
    # （ストアはバッチ処理前の状態のままのため）。
    was_known_regional = compute_was_known_regional(new_disclosures, set(stores["company_status"]["Code"]))

    new_listing = detect_regional_listing_events(new_disclosures, was_known_regional)
    new_major = detect_regional_major_events(new_disclosures)

    # statements専用のウォーターマークが無ければ、上場イベント用の
    # ウォーターマーク(watermark)が既に進んでいてもREGIONAL_STATEMENTS_
    # LOOKBACK_DAYS分だけ遡って取得する（機能を後から追加した既存デプロイ
    # 向けの初回バックフィル。_STATEMENTS_WATERMARK_KEYのコメント参照）。
    statements_watermark = _load_statements_watermark()
    statements_start = (
        statements_watermark + dt.timedelta(days=1)
        if statements_watermark is not None
        else today - dt.timedelta(days=REGIONAL_STATEMENTS_LOOKBACK_DAYS)
    )
    if statements_start < start:
        statements_backfill = tdnet_client.get_disclosures_range(statements_start, start - dt.timedelta(days=1))
        statements_disclosures = pd.concat([statements_backfill, new_disclosures], ignore_index=True)
        was_known_regional_for_statements = compute_was_known_regional(
            statements_disclosures, set(stores["company_status"]["Code"])
        )
    else:
        statements_disclosures = new_disclosures
        was_known_regional_for_statements = was_known_regional

    new_statements = fetch_regional_statements(statements_disclosures, today, was_known_regional_for_statements)
    new_status = _latest_company_status(new_disclosures, was_known_regional)
    # _latest_company_status()はmarkets_stringが有効な回だけを対象にするため、
    # 上場廃止の開示自体でmarkets_stringが欠損しているケースを取り逃す。
    # 上場廃止関連開示の最終日はmarkets_stringの有無に関わらず別途集計する。
    new_delisting_dates = _delisting_dates_by_code(new_disclosures, was_known_regional)

    listing_events = (
        pd.concat([stores["listing_events"], new_listing], ignore_index=True)
        .drop_duplicates(subset=["id"])
        .sort_values("Date")
        .reset_index(drop=True)
    )
    major_events = (
        pd.concat([stores["major_events"], new_major], ignore_index=True)
        .drop_duplicates(subset=["id"])
        .sort_values("Date")
        .reset_index(drop=True)
    )
    statements = (
        pd.concat([stores["statements"], new_statements], ignore_index=True)
        .drop_duplicates(subset=["id"])
        .sort_values("DiscDate")
        .reset_index(drop=True)
    )
    statements = _dedupe_superseded_statements(statements).sort_values("DiscDate").reset_index(drop=True)
    all_status = pd.concat([stores["company_status"], new_status], ignore_index=True)
    company_status = (
        all_status
        .sort_values("LastSeenDate")
        .drop_duplicates(subset=["Code"], keep="last")
        .reset_index(drop=True)
    )

    if company_status.empty:
        company_status["LastDelistingDate"] = pd.Series(dtype="datetime64[ns]")
        company_status["IsDelisted"] = pd.Series(dtype=bool)
    else:
        # 上場廃止関連開示の最終日を銘柄ごとに集約する（古い保存済み分も含めて
        # 最大値を取る。「直近の開示だけ」を見ると、上場廃止の後の何らかの
        # 通常開示でLastDelistingDateがNaTに戻ってしまうため）。new_delisting_dates
        # （markets_string欠損のため上のconcatに乗っていない分）も合わせる。
        # Series.map()に空/datetime64のSeriesをそのまま渡すと内部の型変換で
        # 例外になることがあるためdictを介し、比較の前にto_datetimeで
        # datetime64に統一する。
        last_delisting_by_code = all_status.groupby("Code")["LastDelistingDate"].max().to_dict()
        for code, date in new_delisting_dates.items():
            existing = last_delisting_by_code.get(code)
            if existing is None or pd.isna(existing) or date > existing:
                last_delisting_by_code[code] = date
        company_status["LastDelistingDate"] = pd.to_datetime(company_status["Code"].map(last_delisting_by_code))

        # IsDelistedは「上場廃止関連開示の最終日」と「新規/重複上場が完了
        # した最終日」を比較して決める（後から再上場すれば解除される、
        # 常にTrueに固定されるわけではない）。
        listed_only = listing_events.loc[listing_events["Stage"] == "上場"] if not listing_events.empty else listing_events
        last_relisting_by_code = listed_only.groupby("Code")["Date"].max().to_dict() if not listed_only.empty else {}
        relisted_at = pd.to_datetime(company_status["Code"].map(last_relisting_by_code))
        company_status["IsDelisted"] = company_status["LastDelistingDate"].notna() & (
            relisted_at.isna() | (company_status["LastDelistingDate"] > relisted_at)
        )

    # 現在も地方単独上場のままの銘柄だけ、株価(現在値)を更新する
    # （東証を含む市場に移った銘柄、および上場廃止済みの銘柄は対象外。
    # 既存値のまま保持する）。
    is_delisted = company_status["IsDelisted"].fillna(False).astype(bool)
    still_regional = company_status["MarketsString"].apply(is_regional_only) & ~is_delisted
    for idx in company_status.loc[still_regional].index:
        code = company_status.at[idx, "Code"]
        markets_string = company_status.at[idx, "MarketsString"]
        price, note = fetch_regional_share_price(code, markets_string)
        company_status.at[idx, "CurrentPrice"] = price
        company_status.at[idx, "CurrentPriceNote"] = note

    saved = [
        cache.save(_STORE_ENDPOINT, _LISTING_EVENTS_KEY, listing_events),
        cache.save(_STORE_ENDPOINT, _MAJOR_EVENTS_KEY, major_events),
        cache.save(_STORE_ENDPOINT, _COMPANY_STATUS_KEY, company_status),
        cache.save(_STORE_ENDPOINT, _STATEMENTS_KEY, statements),
    ]
    if all(saved):
        # todayその日のTDnet開示はまだ全件公開されていない可能性がある
        # （更新ボタンは取引時間中にも押される）。ウォーターマークをtoday
        # まで進めてしまうと、その日の後刻に追加された開示を二度と取得
        # できなくなるため、前日までしか「スキャン済み」にしない。
        _save_watermark(today - dt.timedelta(days=1))
        _save_statements_watermark(today - dt.timedelta(days=1))

    return {
        "company_status": company_status,
        "listing_events": listing_events,
        "major_events": major_events,
        "statements": statements,
    }
