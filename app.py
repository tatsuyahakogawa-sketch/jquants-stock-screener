"""須田忠雄事務所ルール スクリーニングの Streamlit UI。

実行方法:
    streamlit run app.py

事前に .env を用意し、J-QuantsのAPIキー（ダッシュボードの「設定 > APIキー」で発行）
を設定しておくこと。詳細は README.md を参照。
"""
from __future__ import annotations

import datetime as dt
import os

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

from src import excel_export
from src.jquants_client import JQuantsAuthError, JQuantsClient
from src.pipeline import RULE_LABELS, POSITIVE_RULES, run_screening, build_summary, enrich_with_market_data

load_dotenv()

# Streamlit Community CloudではAPIキーをst.secrets（Settings > Secrets）で
# 設定するが、jquants_client.py/edinet_client.pyはos.environから読む設計の
# ため、ここでst.secretsの値をos.environに橋渡しする（ローカル実行で
# secrets.tomlが無い場合はStreamlitSecretNotFoundErrorになるので何もしない）。
try:
    for _key in ("JQUANTS_API_KEY", "EDINET_API_KEY"):
        if _key in st.secrets:
            os.environ[_key] = st.secrets[_key]
except Exception:
    pass

st.set_page_config(page_title="日本株スクリーニング", layout="wide")


def _check_password() -> bool:
    """クラウド公開時、無関係な人に開かれてAPIキー（レート制限）を消費されるのを
    防ぐための簡易パスワード認証。secrets.tomlにapp_passwordが設定されていない
    場合（ローカル実行等）は認証をスキップする。
    """
    try:
        correct = st.secrets["app_password"]
    except (KeyError, FileNotFoundError):
        return True
    if st.session_state.get("authenticated"):
        return True

    st.title("日本株スクリーニング")
    password = st.text_input("パスワード", type="password")
    if password:
        if password == correct:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("パスワードが違います")
    return False


if not _check_password():
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
    default=[r for r in POSITIVE_RULES if r != "equity_ratio_high"],
    format_func=lambda k: RULE_LABELS[k],
)
rule_count = len(count_target_rules)
if rule_count <= 1:
    # st.sliderはmin_value < max_valueを要求するため、対象条件が1つ（または0）の
    # ときはスライダーを出さず固定値にする（1つしか無ければ「最低1つ」で確定するため）。
    min_match = 1
else:
    min_match = st.slider(
        "最低いくつの条件に合致した銘柄を表示するか",
        min_value=1,
        max_value=rule_count,
        value=1,
    )
exclude_downward = st.checkbox("業績予想の下方修正歴がある銘柄を除外する", value=True)

if st.button("スクリーニング実行", type="primary"):
    if rule_count == 0:
        st.error("対象にする条件を1つ以上選択してください。")
    elif start_date > end_date:
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
