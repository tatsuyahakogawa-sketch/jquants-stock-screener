"""須田忠雄事務所ルール スクリーニングの Streamlit UI。

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
from dotenv import load_dotenv

from src.jquants_client import JQuantsAuthError, JQuantsClient
from src.pipeline import RULE_LABELS, POSITIVE_RULES, run_screening, build_summary, enrich_with_market_data

load_dotenv()

st.set_page_config(page_title="日本株スクリーニング", layout="wide")

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
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("日本株スクリーニング（須田忠雄事務所ルール）")

st.info(
    "現在このアプリで自動判定できるのは、以下の条件です。\n\n"
    + "\n".join(f"- {label}" for label in RULE_LABELS.values())
    + "\n\n"
    "「新工場・新店舗の開示」「東証移籍」はTDnet開示タイトルのキーワード検出のため、"
    "誤検出の可能性があります。イベント一覧の開示タイトルで内容を確認してください。\n\n"
    "「オーナー経営」「取引先」は、大株主・取引先情報が構造化データとして提供されていないため、"
    "このバージョンでは未対応です（README参照）。\n\n"
    "「上場日」はJ-Quantsに存在しないため、株価データが取得できる最古の日付から推定した近似値"
    "（過去5年より前から取引されている場合は「5年以上前」）を表示しています。"
)

st.caption("契約プラン（Light）は遅延なしで過去5年分のデータを取得できます。")

max_end = dt.date.today() - dt.timedelta(days=1)
default_end = max_end
default_start = default_end - dt.timedelta(days=30)

col1, col2 = st.columns(2)
with col1:
    start_date = st.date_input("開始日", value=default_start, max_value=max_end)
with col2:
    end_date = st.date_input("終了日", value=default_end, max_value=max_end)

st.subheader("絞り込み条件（実行前に選択）")
count_target_rules = st.multiselect(
    "対象にする条件（「合致数」のカウント対象。複数条件に合致する銘柄を探すのに使う）",
    options=POSITIVE_RULES,
    default=POSITIVE_RULES,
    format_func=lambda k: RULE_LABELS[k],
)
min_match = st.slider(
    "最低いくつの条件に合致した銘柄を表示するか",
    min_value=1,
    max_value=max(len(count_target_rules), 1),
    value=1,
)
exclude_downward = st.checkbox("業績予想の下方修正歴がある銘柄を除外する", value=True)

if st.button("スクリーニング実行", type="primary"):
    if start_date > end_date:
        st.error("開始日は終了日より前にしてください。")
    else:
        with st.spinner("J-Quants からデータを取得し、条件判定しています…"):
            try:
                client = JQuantsClient()
                hits = run_screening(client, start_date, end_date)
                summary = build_summary(hits)
                if not summary.empty:
                    with st.spinner(f"合致した{len(summary)}銘柄の時価総額・PER・PBR・配当利回りを取得しています…"):
                        summary = enrich_with_market_data(client, summary)
                st.session_state["hits"] = hits
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
    hits = st.session_state["hits"]

    expected_cols = {f"{r}_matched" for r in count_target_rules}
    if not summary.empty and not expected_cols.issubset(summary.columns):
        st.warning("コード更新により前回の結果が古くなっています。もう一度「スクリーニング実行」を押してください。")
    elif summary.empty:
        st.warning("条件に合致した銘柄はありませんでした。")
    else:
        view = summary.copy()
        if count_target_rules:
            view["MatchedCountSelected"] = view[[f"{r}_matched" for r in count_target_rules]].sum(axis=1)
        else:
            view["MatchedCountSelected"] = 0
        view["MatchedConditions"] = view.apply(
            lambda row: "、".join(RULE_LABELS[r] for r in count_target_rules if row[f"{r}_matched"]),
            axis=1,
        )

        view = view[view["MatchedCountSelected"] >= min_match]
        if exclude_downward:
            view = view[~view["HasDownwardRevision"]]

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

        if "MarketCap" in view.columns:
            view["MarketCapOku"] = (view["MarketCap"] / 1e8).round(1)

        display_cols = [
            "Code", "CompanyName", "Sector", "MatchedCountSelected", "MatchedConditions",
            "StopHighDate", "MarketCapOku", "PER", "PBR", "DividendYield",
            "ListingDateDisplay", "HasDownwardRevision",
        ]
        display_cols = [c for c in display_cols if c in view.columns]
        display = view[display_cols].rename(columns={
            "Sector": "業種",
            "MatchedCountSelected": "合致数",
            "MatchedConditions": "合致した条件",
            "StopHighDate": "ストップ高日付",
            "MarketCapOku": "時価総額(億円)",
            "PER": "PER(予想/実績年率換算)",
            "PBR": "PBR",
            "DividendYield": "配当利回り",
            "ListingDateDisplay": "推定上場日（近似）",
            "HasDownwardRevision": "下方修正歴あり",
        }).sort_values("合致数", ascending=False)

        if "PER(予想/実績年率換算)" in display.columns:
            display["PER(予想/実績年率換算)"] = display["PER(予想/実績年率換算)"].round(1)
        if "PBR" in display.columns:
            display["PBR"] = display["PBR"].round(2)
        if "配当利回り" in display.columns:
            display["配当利回り"] = (display["配当利回り"] * 100).round(2)

        st.success(f"{len(display)} 銘柄が条件に合致しました（列見出しクリックでソート可能）。")
        st.dataframe(display, width="stretch", hide_index=True)
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

        with st.expander("個別イベントの一覧（いつ何が起きたかの詳細）"):
            st.dataframe(hits, width="stretch", hide_index=True)
            st.download_button(
                "CSVダウンロード（イベント一覧）",
                data=hits.to_csv(index=False).encode("utf-8-sig"),
                file_name="screening_events.csv",
                mime="text/csv",
            )

st.caption(
    "本アプリの結果は投資判断の参考情報であり、投資助言ではありません。"
    "自己判断・自己責任でご利用ください。"
)
