"""須田忠雄事務所ルール スクリーニングの Streamlit UI。

絞り込み条件の中に「地方単独上場企業のみを検索する」チェックボックスがあり、
ONにするとJ-Quantsベースのスクリーニング（イベント・業績・財務条件）の代わりに
TDnetベースの地方株スキャン結果を表示する（地方単独上場企業はJ-Quants対象外の
ためPBR等の財務条件と併用できない。CLAUDE.md参照）。

実行方法:
    streamlit run app.py

事前に .env を用意し、J-QuantsのAPIキー（ダッシュボードの「設定 > APIキー」で発行）
を設定しておくこと。詳細は README.md を参照。
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components

from src import excel_export, regional_stocks
from src.auth import bridge_env_secrets, check_password
from src.jquants_client import JQuantsAuthError, JQuantsClient
from src.pipeline import (
    RULE_LABELS,
    EVENT_RULES,
    ATTRIBUTE_RULES,
    TDNET_TITLE_BASED_RULES,
    YOY_LOOKBACK_RULES,
    run_screening,
    build_summary,
    enrich_with_market_data,
)

# EVENT_RULESを画面上の見た目だけ2グループに分ける（判定ロジックは変更しない）。
_MATERIAL_EVENT_RULES = [
    "stop_high", "stock_split", "new_facility_or_store", "large_order",
    "world_first", "market_upgrade_to_prime", "exchange_transfer_to_tokyo",
]
_PERFORMANCE_EVENT_RULES = [
    "sales_growth_major", "sales_growth_explosive", "earnings_beat",
    "two_quarter_growth", "profit_doubling",
]

bridge_env_secrets()

st.set_page_config(page_title="日本株スクリーニング", layout="wide")


if not check_password():
    st.stop()

# 印刷時は入力欄・ボタン・サイドバー等を隠し、結果テーブルだけを表示する。
# st.dataframeは仮想スクロールのグリッドで画面外の行が印刷に出ないため、
# 印刷用には全行を静的HTMLテーブルとして別途出力し（.print-only）、
# 通常時は非表示にしておく。
st.markdown(
    """
    <style>
    @media print {
        header, [data-testid="stSidebar"], [data-testid="stStatusWidget"],
        div[data-testid="stButton"], div[data-testid="stDownloadButton"],
        div[data-testid="stDateInput"], div[data-testid="stSlider"],
        div[data-testid="stMultiSelect"], div[data-testid="stCheckbox"],
        div[data-testid="stExpander"], div[data-testid="stDataFrame"],
        .no-print {
            display: none !important;
        }
        .print-only { display: block !important; }
    }
    .print-only { display: none; }
    .print-only table { border-collapse: collapse; width: 100%; font-size: 12px; }
    .print-only th, .print-only td {
        border: 1px solid #999; padding: 4px 6px; text-align: left;
    }
    /* multiselectの選択タグ（赤いタグ）が画面幅不足で条件名を省略表示するのを防ぐ。
       小さい画面でもタグ内テキストを折り返して全文表示する。 */
    [data-baseweb="tag"] {
        max-width: none !important;
        height: auto !important;
    }
    [data-baseweb="tag"] span {
        white-space: normal !important;
        overflow: visible !important;
        text-overflow: unset !important;
        max-width: none !important;
    }
    /* 前年同期比較が必要で検索が遅くなる項目のpillsだけ、選択時に紫色にする
       （st.container(key="slow_performance_pills")でこのCSSの適用範囲を
       スコープしている。data-variant="pills"・aria-pressedはst.pillsが
       付与する属性で、実機のDOMで確認済みの安定したフック）。 */
    .st-key-slow_performance_pills [data-variant="pills"][aria-pressed="true"] {
        background-color: rgba(147, 51, 234, 0.2) !important;
        color: rgb(147, 51, 234) !important;
        border-color: rgb(147, 51, 234) !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("日本株スクリーニング（須田忠雄事務所ルール）")

st.info(
    "現在このアプリで自動判定できるのは、以下の条件です。\n\n"
    + "\n".join(f"- {label}" for label in RULE_LABELS.values())
    + "\n\n"
    "次の条件はTDnet開示タイトルのキーワード検出のため、誤検出の可能性があります: "
    + "、".join(RULE_LABELS[r] for r in TDNET_TITLE_BASED_RULES)
    + "。銘柄コードを確認し、TDnet（適時開示情報閲覧サービス）等で開示内容を確認してください。\n\n"
    "「オーナー経営」「取引先」は、大株主・取引先情報が構造化データとして提供されていないため、"
    "このバージョンでは未対応です（README参照）。\n\n"
    "「上場日」はJ-Quantsに存在しないため、株価データが取得できる最古の日付から推定した近似値"
    "（過去5年より前から取引されている場合は「5年以上前」）を表示しています。"
)

st.caption("契約プラン（Light）は遅延なしで過去5年分のデータを取得できます。")


def render_regional_section() -> None:
    """地方証券取引所（札幌・福岡・名古屋）単独上場企業の発見支援。

    まだ注目されていない地方上場企業を早く見つけることを目的とし、以下を表示する。
      - 地方単独上場企業の一覧（上場日・現在の市場・現在値）
      - 東証への新規上場・重複上場・市場変更の発表（最重要イベント）
      - M&A・TOB等、株価に大きな影響を与えうる大型企業イベント

    データ源はTDnet開示（markets_string列で取引所を判定）。J-Quantsは地方取引所
    単独上場企業を一切対象としないため使用しない（CLAUDE.md参照）。PBR等の財務
    条件は計算に必要な決算数値自体がJ-Quantsから取得できず併用不可のため、この
    モードでは財務条件のカードごと表示しない。
    """
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
    statements = store["statements"]

    if company_status.empty and listing_events.empty and major_events.empty:
        st.info("まだデータがありません。上の「最新情報を取得（更新）」を押してください。")
        return

    with st.container(border=True):
        st.markdown("### 💰 財務条件で絞り込む（任意）")
        st.caption(
            "地方単独上場企業はJ-Quants対象外のためPBR・PER等は計算できませんが、TDnet決算短信の"
            "サマリー情報（本決算/四半期の実績・前年同期比較）からは売上高増加・自己資本比率等の"
            "判定ができます。決算短信のXBRL添付は公開から約1〜1.5ヶ月で取得できなくなるため、"
            "直近の開示分から順次蓄積されます（README参照）。"
        )
        selected_statement_rules = st.pills(
            "対象にする財務条件",
            options=regional_stocks.REGIONAL_STATEMENT_RULES,
            format_func=lambda k: RULE_LABELS[k],
            selection_mode="multi",
            default=[],
            key="regional_statement_rules_pills",
            label_visibility="collapsed",
        )
        exclude_downward_regional = st.checkbox(
            "業績予想の下方修正歴がある銘柄を除外する", value=True, key="regional_exclude_downward"
        )

    if selected_statement_rules:
        rules_to_check = list(selected_statement_rules)
        if exclude_downward_regional:
            rules_to_check.append(regional_stocks.REGIONAL_NEGATIVE_RULE)
        hits = regional_stocks.screen_regional(pd.DataFrame(), statements, company_status, rules_to_check)
        summary = build_summary(hits)
        if not summary.empty:
            if exclude_downward_regional:
                summary = summary[~summary["HasDownwardRevision"]]
            # MatchedCountはbuild_summary()が全ルールについて集計した値のため、
            # ユーザーが選んでいない条件（例: sales_growth_majorのみ選択時に
            # 実際は閾値50%以上でsales_growth_explosive側にラベル付けされた行）
            # まで含んでしまう。選択した条件だけで数え直す（app.py内の通常
            # スクリーニングのMatchedCountSelectedと同じ方式。2026-08-19の
            # Codexレビューで指摘: これをしないと「合致した条件」欄が空欄の
            # まま表示されることがあった）。
            summary["MatchedCountSelected"] = summary[[f"{r}_matched" for r in selected_statement_rules]].sum(axis=1)
            summary = summary[summary["MatchedCountSelected"] >= 1]
            summary["MatchedConditions"] = summary.apply(
                lambda row: "、".join(RULE_LABELS[r] for r in selected_statement_rules if row[f"{r}_matched"]),
                axis=1,
            )

        display = (
            summary[["Code", "CompanyName", "MatchedCountSelected", "MatchedConditions"]]
            .rename(columns={"MatchedCountSelected": "合致数", "MatchedConditions": "合致した条件"})
            .sort_values("合致数", ascending=False)
            if not summary.empty else summary
        )
        if display.empty:
            st.warning("該当なし（財務条件に合致した地方単独上場企業はありませんでした）。")
        else:
            st.success(f"{len(display)} 銘柄が財務条件に合致しました。")
            st.dataframe(display, width="stretch", hide_index=True)
        st.divider()
        st.caption("以下は絞り込みなしの一覧です。")

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
        return

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


st.subheader("絞り込み条件（実行前に選択）")

regional_only = st.checkbox(
    "地方単独上場企業（札幌・福岡・名古屋）のみを検索する",
    key="regional_only",
    help=(
        "TDnet開示から検出します（J-Quantsは地方取引所単独上場企業を対象としないため）。"
        "PBR等の財務条件・イベント条件は地方単独上場企業には計算できないため併用できません。"
    ),
)

if regional_only:
    render_regional_section()
else:
    max_end = dt.date.today() - dt.timedelta(days=1)
    default_end = max_end
    default_start = default_end

    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input("開始日", value=default_start, max_value=max_end)
    with col2:
        end_date = st.date_input("終了日", value=default_end, max_value=max_end)

    with st.container(border=True):
        st.markdown("### 🚀 イベント・材料")
        event_and_or = st.radio(
            "イベント条件の組み合わせ方",
            options=["OR検索", "AND検索"],
            index=0,
            horizontal=True,
            key="event_logic",
            help="OR＝選んだ条件のうち1つでも合致すれば表示。AND＝選んだ条件すべてに合致した銘柄だけ表示。",
        )
        material_events = st.pills(
            "対象にするイベント・材料条件",
            options=_MATERIAL_EVENT_RULES,
            format_func=lambda k: RULE_LABELS[k],
            selection_mode="multi",
            default=[],
            key="material_events_pills",
            label_visibility="collapsed",
        )
        st.caption(
            "※次の条件はTDnet開示タイトルのキーワード検出のため誤検出の可能性があります: "
            + "、".join(RULE_LABELS[r] for r in TDNET_TITLE_BASED_RULES if r in _MATERIAL_EVENT_RULES)
        )
        st.caption(f"{len(material_events)}件選択中")

    _FAST_PERFORMANCE_RULES = [r for r in _PERFORMANCE_EVENT_RULES if r not in YOY_LOOKBACK_RULES]
    _SLOW_PERFORMANCE_RULES = [r for r in _PERFORMANCE_EVENT_RULES if r in YOY_LOOKBACK_RULES]

    with st.container(border=True):
        st.markdown("### 📈 業績")
        fast_performance_events = st.pills(
            "対象にする業績条件",
            options=_FAST_PERFORMANCE_RULES,
            format_func=lambda k: RULE_LABELS[k],
            selection_mode="multi",
            default=[],
            key="fast_performance_events_pills",
            label_visibility="collapsed",
        )
        with st.container(key="slow_performance_pills"):
            slow_performance_events = st.pills(
                "対象にする業績条件（前年同期比較のため時間がかかるもの）",
                options=_SLOW_PERFORMANCE_RULES,
                format_func=lambda k: RULE_LABELS[k],
                selection_mode="multi",
                default=[],
                key="slow_performance_events_pills",
                label_visibility="collapsed",
            )
        st.caption("🟣紫色のボタンは決算データを数年分遡って取得するため、検索に時間がかかります。")
        st.caption("黒字転換（今後追加予定）")
        performance_events = list(fast_performance_events) + list(slow_performance_events)
        st.caption(f"{len(performance_events)}件選択中")

    # st.pillsは複数ウィジェットを同じ変数名にできないため、2カードの選択結果をここで結合する
    # （検索ロジック自体はEVENT_RULES全体に対して変わらず動く）。
    selected_events = list(material_events) + list(performance_events)

    with st.container(border=True):
        st.markdown("### 💰 財務")
        attr_cols = st.columns(len(ATTRIBUTE_RULES))
        selected_attributes = []
        for attr_col, rule in zip(attr_cols, ATTRIBUTE_RULES):
            with attr_col:
                if st.checkbox(RULE_LABELS[rule], key=f"attr_{rule}"):
                    selected_attributes.append(rule)
        exclude_downward = st.checkbox("業績予想の下方修正歴がある銘柄を除外する", value=True, key="exclude_downward")
        st.caption(f"{len(selected_attributes) + int(exclude_downward)}件選択中")

    if st.button("スクリーニング実行", type="primary"):
        if not selected_events and not selected_attributes:
            st.error("イベント条件または属性条件を1つ以上選択してください。")
        elif start_date > end_date:
            st.error("開始日は終了日より前にしてください。")
        else:
            with st.spinner("J-Quants からデータを取得し、条件判定しています…"):
                try:
                    client = JQuantsClient()
                    hits = run_screening(
                        client, start_date, end_date,
                        selected_rules=selected_events + selected_attributes,
                    )
                    summary = build_summary(hits)
                    if not summary.empty:
                        with st.spinner(f"合致した{len(summary)}銘柄の時価総額・PER・PBR・配当利回りを取得しています…"):
                            summary = enrich_with_market_data(client, summary)
                    st.session_state["summary"] = summary
                except JQuantsAuthError as e:
                    st.error(f"J-Quants への認証に失敗しました: {e}")
                except requests.exceptions.HTTPError as e:
                    status = e.response.status_code if e.response is not None else None
                    body = e.response.text if e.response is not None else str(e)
                    if status == 400:
                        st.error(f"J-Quants への取得条件が不正です（契約プランの取得可能期間外の可能性）。\n\n{body}")
                    elif status == 429:
                        st.error("J-Quants のレート制限に達しました。1分ほど待ってから再実行してください。")
                    else:
                        st.error(f"J-Quants API呼び出しでエラーが発生しました (status={status})。\n\n{body}")

    def _format_date(value) -> str:
        if value is None or pd.isna(value):
            return ""
        return pd.Timestamp(value).strftime("%Y-%m-%d")

    if "summary" in st.session_state:
        summary = st.session_state["summary"]

        expected_cols = {f"{r}_matched" for r in (selected_events + selected_attributes)}
        if not summary.empty and not expected_cols.issubset(summary.columns):
            st.warning("コード更新により前回の結果が古くなっています。もう一度「スクリーニング実行」を押してください。")
        elif summary.empty:
            st.warning("該当なし（条件に合致した銘柄はありませんでした）。")
        else:
            view = summary.copy()
            if selected_events:
                view["MatchedCountSelected"] = view[[f"{r}_matched" for r in selected_events]].sum(axis=1)
                if event_and_or == "AND検索":
                    view = view[view["MatchedCountSelected"] == len(selected_events)]
                else:
                    view = view[view["MatchedCountSelected"] >= 1]
            else:
                view["MatchedCountSelected"] = 0
            view["MatchedConditions"] = view.apply(
                lambda row: "、".join(RULE_LABELS[r] for r in selected_events if row[f"{r}_matched"]),
                axis=1,
            )

            for attr_rule in selected_attributes:
                view = view[view[f"{attr_rule}_matched"]]
            if exclude_downward:
                view = view[~view["HasDownwardRevision"]]

            if "MarketCap" in view.columns:
                view["MarketCapOku"] = (view["MarketCap"] / 1e8).round(0)

            if "stop_high_date" in view.columns:
                view["StopHighDate"] = view["stop_high_date"].apply(_format_date)

            if "EstimatedListingDate" in view.columns:
                view["ListingDateDisplay"] = view.apply(
                    lambda row: (
                        _format_date(row["EstimatedListingDate"])
                        if row.get("RecentlyListed") and pd.notna(row.get("EstimatedListingDate"))
                        else "5年以上前"
                    ),
                    axis=1,
                )

            display_cols = [
                "Code", "CompanyName", "Sector", "MatchedCountSelected", "MatchedConditions",
                "StopHighDate", "MarketCapOku", "PER", "PBR", "DividendYield",
                "ListingDateDisplay", "HasDownwardRevision",
            ]
            rename_map = {
                "Sector": "業種",
                "MatchedCountSelected": "合致数",
                "MatchedConditions": "合致した条件",
                "StopHighDate": "ストップ高日付",
                "MarketCapOku": "時価総額(億円)",
                "PER": "PER(予想)",
                "PBR": "PBR",
                "DividendYield": "配当利回り",
                "ListingDateDisplay": "推定上場日（近似）",
                "HasDownwardRevision": "下方修正歴あり",
            }

            display_cols = [c for c in display_cols if c in view.columns]
            display = view[display_cols].rename(columns=rename_map).sort_values("合致数", ascending=False)

            if "PER(予想)" in display.columns:
                display["PER(予想)"] = display["PER(予想)"].round(1)
            if "PBR" in display.columns:
                display["PBR"] = display["PBR"].round(1)
            if "配当利回り" in display.columns:
                display["配当利回り"] = (display["配当利回り"] * 100).round(1)

            if len(display) == 0:
                st.warning("該当なし（絞り込み条件を満たす銘柄はありませんでした）。")
            else:
                st.success(f"{len(display)} 銘柄が条件に合致しました（列見出しクリックでソート可能）。")
            st.dataframe(
                display,
                width="stretch",
                hide_index=True,
                column_config={"時価総額(億円)": st.column_config.NumberColumn(format="%,d")},
            )
            st.download_button(
                "CSVダウンロード（銘柄集計）",
                data=display.to_csv(index=False).encode("utf-8-sig"),
                file_name="screening_summary.csv",
                mime="text/csv",
            )
            components.html(
                """
                <button onclick="window.parent.print()"
                    style="padding:6px 14px;font-size:14px;cursor:pointer;">
                    🖨️ 印刷
                </button>
                """,
                height=45,
            )
            st.markdown(
                f'<div class="print-only">{display.to_html(index=False, escape=True)}</div>',
                unsafe_allow_html=True,
            )


def _call_jquants(fn):
    """J-Quants呼び出しを実行し、認証・レート制限等のエラーを共通のメッセージで表示する。"""
    try:
        return fn()
    except excel_export.NotCommonStockError as e:
        st.warning(str(e))
    except JQuantsAuthError as e:
        st.error(f"J-Quants への認証に失敗しました: {e}")
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        body = e.response.text if e.response is not None else str(e)
        if status == 400:
            st.error(f"J-Quants への取得条件が不正です（契約プランの取得可能期間外の可能性）。\n\n{body}")
        elif status == 429:
            st.error("J-Quants のレート制限に達しました。1分ほど待ってから再実行してください。")
        else:
            st.error(f"J-Quants API呼び出しでエラーが発生しました (status={status})。\n\n{body}")
    return None


st.divider()
st.subheader("個別銘柄のExcel出力（企業詳細・実行表）")
st.caption(
    "銘柄コードを入力すると、時価総額・PBR・PER・配当利回り・株価・四半期業績（売上・経常利益）を"
    "Excelテンプレートに自動入力します。事業概要・主要株主・将来予想等の自由記述はJ-Quantsだけでは"
    "自動化できないため空欄のままです。生成後、手動で追記のうえ印刷してください。"
)
export_code = st.text_input("銘柄コード", key="export_code", placeholder="例: 6584")

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

exp_col1, exp_col2 = st.columns(2)
with exp_col1:
    if st.button("企業詳細を生成"):
        code = export_code.strip()
        if not code:
            st.warning("銘柄コードを入力してください。")
        else:
            with st.spinner("企業詳細Excelを生成しています…"):
                client = JQuantsClient()
                data = _call_jquants(lambda: excel_export.build_company_detail_excel(client, code))
            if data is not None:
                st.session_state["detail_excel"] = data
                st.session_state["detail_excel_code"] = code
    if "detail_excel" in st.session_state:
        st.download_button(
            "企業詳細をダウンロード",
            data=st.session_state["detail_excel"],
            file_name=f"企業詳細_{st.session_state['detail_excel_code']}.xlsx",
            mime=_XLSX_MIME,
        )
with exp_col2:
    if st.button("実行表を生成"):
        code = export_code.strip()
        if not code:
            st.warning("銘柄コードを入力してください。")
        else:
            with st.spinner("実行表Excelを生成しています…"):
                client = JQuantsClient()
                data = _call_jquants(lambda: excel_export.build_execution_table_excel(client, code))
            if data is not None:
                st.session_state["table_excel"] = data
                st.session_state["table_excel_code"] = code
    if "table_excel" in st.session_state:
        st.download_button(
            "実行表をダウンロード",
            data=st.session_state["table_excel"],
            file_name=f"実行表_{st.session_state['table_excel_code']}.xlsx",
            mime=_XLSX_MIME,
        )

st.caption(
    "本アプリの結果は投資判断の参考情報であり、投資助言ではありません。"
    "自己判断・自己責任でご利用ください。"
)
