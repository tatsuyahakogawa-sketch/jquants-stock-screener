"""地方株 - 地方証券取引所（札幌・福岡・名古屋）単独上場企業の発見支援ページ。

まだ注目されていない地方上場企業を早く見つけることを目的とし、以下を表示する。
  - 地方単独上場企業の一覧（上場日・現在の市場・現在値）
  - 東証への新規上場・重複上場・市場変更の発表（最重要イベント）
  - M&A・TOB等、株価に大きな影響を与えうる大型企業イベント

データ源はTDnet開示（markets_string列で取引所を判定）。J-Quantsは地方取引所
単独上場企業を一切対象としないため使用しない（CLAUDE.md参照）。
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from src import regional_stocks
from src.auth import bridge_env_secrets, check_password

bridge_env_secrets()

st.set_page_config(page_title="地方株", layout="wide")

if not check_password():
    st.stop()

st.title("地方株 - 地方上場株・重要イベント検索")
st.caption(
    "地方証券取引所（札幌・福岡・名古屋）単独上場企業の新規上場・東証関連イベント・"
    "大型企業イベントをTDnet開示から検出します。時価総額（株価×発行済株式数）は"
    "算出できないため掲載しません。福証単独上場企業のみyfinanceで取得できる"
    "現在の株価を「現在値」として表示し、それ以外は取得不可（空欄）です。"
)

if st.button("最新情報を取得（更新）"):
    with st.spinner("TDnetから未取得分の開示を取得中です（初回は数分かかります）…"):
        try:
            regional_stocks.update_regional_store()
        except Exception as exc:  # noqa: BLE001 -- 個人運営の非公式TDnetミラーは予告なく不安定になることがあるため、失敗しても保存済みデータの表示は継続する
            st.error(f"TDnetからの取得に失敗しました。しばらく待って再度お試しください（{exc}）")
        else:
            st.success("更新しました。")

store = regional_stocks.load_regional_store()
company_status = store["company_status"]
listing_events = store["listing_events"]
major_events = store["major_events"]

if company_status.empty and listing_events.empty and major_events.empty:
    st.info("まだデータがありません。上の「最新情報を取得（更新）」を押してください。")
    st.stop()

# --- 上場日（Stage=="上場"の最新日）を銘柄ごとに集計 ---
listed_only = listing_events.loc[listing_events["Stage"] == "上場"] if not listing_events.empty else listing_events
listing_date_by_code = (
    listed_only.groupby("Code")["Date"].max() if not listed_only.empty else pd.Series(dtype="datetime64[ns]")
)

current_price_by_code = (
    company_status.set_index("Code")[["CurrentPrice", "CurrentPriceNote"]] if not company_status.empty else None
)


def _current_price_display(code: str) -> str:
    if current_price_by_code is None or code not in current_price_by_code.index:
        return "―"
    row = current_price_by_code.loc[code]
    if pd.notna(row["CurrentPrice"]):
        return f"{row['CurrentPrice']:,.0f}円"
    note = row["CurrentPriceNote"]
    return str(note) if pd.notna(note) and note else "取得不可"


def _listing_date(code: str) -> pd.Timestamp | None:
    if code not in listing_date_by_code.index:
        return None
    return listing_date_by_code.loc[code]


markets_string_by_code = (
    company_status.set_index("Code")["MarketsString"] if not company_status.empty else pd.Series(dtype="object")
)


def _market_display(code: str, fallback_markets_string: str) -> str:
    # 個々のイベント発生時点のmarkets_stringではなく、company_statusが持つ
    # 直近の値を優先する。東証移籍が完了した銘柄の古いイベント行が、
    # 移籍前の地方単独上場の市場のまま表示され続けないようにするため。
    markets_string = markets_string_by_code.get(code, fallback_markets_string)
    return "・".join(regional_stocks.all_markets_in(markets_string)) or str(markets_string)


# --- ①②③を1つの「イベント」テーブルにまとめる（④: id基準で重複排除） ---
rows: list[dict] = []
for _, r in listing_events.iterrows():
    label = ("🔴東証関連: " if r["IsTokyoRelated"] else "地方市場: ") + f"上場{r['Stage']}"
    rows.append({
        "id": r["id"],
        "コード": r["Code"],
        "会社名": r["CompanyName"],
        "市場": _market_display(r["Code"], r["MarketsString"]),
        "上場日": _listing_date(r["Code"]),
        "現在値": _current_price_display(r["Code"]),
        "重要イベント": label,
        "発表日": r["Date"],
        "内容": r["Title"],
        "URL": r["Url"],
        "_is_tokyo": r["IsTokyoRelated"],
    })
for _, r in major_events.iterrows():
    rows.append({
        "id": r["id"],
        "コード": r["Code"],
        "会社名": r["CompanyName"],
        "市場": _market_display(r["Code"], r["MarketsString"]),
        "上場日": _listing_date(r["Code"]),
        "現在値": _current_price_display(r["Code"]),
        "重要イベント": f"大型イベント: {r['MatchedKeyword']}",
        "発表日": r["Date"],
        "内容": r["Title"],
        "URL": r["Url"],
        "_is_tokyo": False,
    })

# イベントが1件も検出されていない地方単独上場企業も、①の一覧としては
# 表示対象（イベント検出だけに頼ると、通常開示しかしていない銘柄が
# 一覧からまるごと漏れてしまうため）。
codes_with_event = {r["コード"] for r in rows}
if not company_status.empty:
    is_regional = company_status["MarketsString"].apply(regional_stocks.is_regional_only)
    is_delisted = company_status["IsDelisted"].fillna(False).astype(bool)
    still_regional = company_status.loc[is_regional & ~is_delisted]
    for _, r in still_regional.iterrows():
        if r["Code"] in codes_with_event:
            continue
        rows.append({
            "id": f"status-{r['Code']}",
            "コード": r["Code"],
            "会社名": r["CompanyName"],
            "市場": _market_display(r["Code"], r["MarketsString"]),
            "上場日": _listing_date(r["Code"]),
            "現在値": _current_price_display(r["Code"]),
            "重要イベント": "―",
            "発表日": r["LastSeenDate"],
            "内容": "（新規上場・大型イベントの検出なし。通常開示のみ確認）",
            "URL": None,
            "_is_tokyo": False,
        })

if not rows:
    st.info("検出された地方単独上場企業・イベントはまだありません。")
    st.stop()

events_df = pd.DataFrame(rows).drop_duplicates(subset=["id"])

sort_option = st.radio("並び順", ["イベントの新しい順", "上場日の新しい順", "最重要イベントを先頭に"], horizontal=True)
if sort_option == "イベントの新しい順":
    events_df = events_df.sort_values("発表日", ascending=False, na_position="last")
elif sort_option == "上場日の新しい順":
    events_df = events_df.sort_values("上場日", ascending=False, na_position="last")
else:
    events_df = events_df.sort_values(["_is_tokyo", "発表日"], ascending=[False, False], na_position="last")

display_df = events_df.drop(columns=["id", "_is_tokyo"]).copy()
display_df["上場日"] = display_df["上場日"].apply(lambda d: d.strftime("%Y-%m-%d") if pd.notna(d) else "不明")

st.dataframe(
    display_df,
    column_config={"URL": st.column_config.LinkColumn("開示リンク")},
    width="stretch",
    hide_index=True,
)
