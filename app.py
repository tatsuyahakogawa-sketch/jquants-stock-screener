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
from src.pipeline import (
    RULE_LABELS,
    EVENT_RULES,
    ATTRIBUTE_RULES,
    TDNET_TITLE_BASED_RULES,
    run_screening,
    build_summary,
    enrich_with_market_data,
    compute_tenx_scores,
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

_MARKET_CAP_CEILINGS = {"30億円以下": 30, "100億円以下": 100, "300億円以下": 300, "500億円以下": 500, "1000億円以下": 1000}
_PER_CEILINGS = {"10以下": 10, "15以下": 15, "20以下": 20, "30以下": 30}
_DIVIDEND_FLOORS = {"1%以上": 1, "2%以上": 2, "3%以上": 3, "4%以上": 4, "5%以上": 5}
_ROE_FLOORS = {"10%以上": 10, "15%以上": 15, "20%以上": 20}
_MARGIN_FLOORS = {"5%以上": 5, "10%以上": 10, "15%以上": 15, "20%以上": 20}
_NO_LIMIT = "制限なし"

# プリセット。テーマ性の強いもの（半導体・AI・データセンター・宇宙・量子等）は、
# 会社名や業種名の単純一致では誤判定が多く実用にならないため今回は含めていない
# （テーマ検索機能の実装待ち）。ここにあるのは既存データで確実に判定できるものだけ。
_PRESETS = {
    "テンバガー": {
        "tenx_enabled": True,
        "market_cap_ceiling": "300億円以下",
        "margin_floor": "10%以上",
    },
    "高配当": {
        "dividend_floor": "4%以上",
        "exclude_downward": True,
    },
    "地方銀行": {
        "sector_filter": "銀行業",
    },
    "黒字転換": {
        # スコアでの絞り込みは行わない方針のため、スコアを表示して
        # 「加点理由」に黒字転換が出た銘柄を目視できるようにするだけ。
        "tenx_enabled": True,
    },
    "爆発決算": {
        "selected_events": ["sales_growth_explosive", "earnings_beat"],
        "event_logic": "OR検索",
    },
    "割安株": {
        "selected_attributes": ["pbr_low"],
        "per_ceiling": "15以下",
    },
    "IPO": {
        "recently_listed_only": True,
    },
}

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
    "次の条件はTDnet開示タイトルのキーワード検出のため、誤検出の可能性があります: "
    + "、".join(RULE_LABELS[r] for r in TDNET_TITLE_BASED_RULES)
    + "。銘柄コードを確認し、TDnet（適時開示情報閲覧サービス）等で開示内容を確認してください。\n\n"
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


def _apply_settings(settings: dict) -> None:
    """プリセット・保存済み条件を各ウィジェットのkeyへ反映して再実行する。

    この下にある対象ウィジェットが生成される前（このブロック内）で
    session_stateへ書き込む必要がある（Streamlitの制約）。
    """
    st.session_state["tenx_enabled"] = settings.get("tenx_enabled", False)
    st.session_state["market_cap_ceiling"] = settings.get("market_cap_ceiling", _NO_LIMIT)
    st.session_state["per_ceiling"] = settings.get("per_ceiling", _NO_LIMIT)
    st.session_state["dividend_floor"] = settings.get("dividend_floor", _NO_LIMIT)
    st.session_state["roe_floor"] = settings.get("roe_floor", _NO_LIMIT)
    st.session_state["margin_floor"] = settings.get("margin_floor", _NO_LIMIT)
    st.session_state["exclude_downward"] = settings.get("exclude_downward", True)
    st.session_state["sector_filter"] = settings.get("sector_filter", "")
    st.session_state["recently_listed_only"] = settings.get("recently_listed_only", False)
    st.session_state["event_logic"] = settings.get("event_logic", "OR検索")
    preset_events = set(settings.get("selected_events", []))
    st.session_state["material_events_pills"] = [r for r in _MATERIAL_EVENT_RULES if r in preset_events]
    st.session_state["performance_events_pills"] = [r for r in _PERFORMANCE_EVENT_RULES if r in preset_events]
    preset_attrs = set(settings.get("selected_attributes", []))
    for rule in ATTRIBUTE_RULES:
        st.session_state[f"attr_{rule}"] = rule in preset_attrs
    st.rerun()


def _current_settings_snapshot() -> dict:
    return {
        "tenx_enabled": st.session_state.get("tenx_enabled", False),
        "market_cap_ceiling": st.session_state.get("market_cap_ceiling", _NO_LIMIT),
        "per_ceiling": st.session_state.get("per_ceiling", _NO_LIMIT),
        "dividend_floor": st.session_state.get("dividend_floor", _NO_LIMIT),
        "roe_floor": st.session_state.get("roe_floor", _NO_LIMIT),
        "margin_floor": st.session_state.get("margin_floor", _NO_LIMIT),
        "exclude_downward": st.session_state.get("exclude_downward", True),
        "sector_filter": st.session_state.get("sector_filter", ""),
        "recently_listed_only": st.session_state.get("recently_listed_only", False),
        "event_logic": st.session_state.get("event_logic", "OR検索"),
        "selected_events": st.session_state.get("selected_events", []),
        "selected_attributes": [r for r in ATTRIBUTE_RULES if st.session_state.get(f"attr_{r}")],
    }


st.markdown("**プリセット**（クリックで下の条件に反映されます。あとから自由に変更できます）")
st.caption(
    "半導体・AI・データセンター・宇宙・量子等のテーマ別プリセットは、会社名の単純一致では"
    "精度が出ないため今回は含めていません（テーマ検索機能の実装待ち）。"
)
preset_cols = st.columns(4)
for i, preset_name in enumerate(_PRESETS):
    if preset_cols[i % 4].button(preset_name, key=f"preset_{preset_name}"):
        _apply_settings(_PRESETS[preset_name])

with st.expander("💾 検索条件の保存・呼び出し（このブラウザを閉じるまで有効）"):
    saved_searches = st.session_state.setdefault("saved_searches", {})
    save_col1, save_col2 = st.columns([3, 1])
    with save_col1:
        save_name = st.text_input("保存名", key="save_search_name", placeholder="例: 銀行高配当", label_visibility="collapsed")
    with save_col2:
        if st.button("⭐ 現在の条件を保存"):
            if save_name.strip():
                saved_searches[save_name.strip()] = _current_settings_snapshot()
                st.success(f"「{save_name.strip()}」として保存しました。")
            else:
                st.warning("保存名を入力してください。")
    if saved_searches:
        load_col1, load_col2 = st.columns([3, 1])
        with load_col1:
            load_name = st.selectbox("保存済みの条件", options=list(saved_searches.keys()), label_visibility="collapsed")
        with load_col2:
            if st.button("この条件を呼び出す"):
                _apply_settings(saved_searches[load_name])

st.divider()

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

with st.container(border=True):
    st.markdown("### 📈 業績")
    performance_events = st.pills(
        "対象にする業績条件",
        options=_PERFORMANCE_EVENT_RULES,
        format_func=lambda k: RULE_LABELS[k],
        selection_mode="multi",
        default=[],
        key="performance_events_pills",
        label_visibility="collapsed",
    )
    st.caption("黒字転換（今後追加予定・現在は10倍株候補スコアの加点理由でのみ確認できます）")
    st.caption(f"{len(performance_events)}件選択中")

# st.pillsは複数ウィジェットを同じ変数名にできないため、2カードの選択結果をここで結合する
# （検索ロジック自体はEVENT_RULES全体に対して変わらず動く）。
selected_events = list(material_events) + list(performance_events)
st.session_state["selected_events"] = selected_events

with st.container(border=True):
    st.markdown("### 💰 財務")
    attr_cols = st.columns(len(ATTRIBUTE_RULES))
    selected_attributes = []
    for attr_col, rule in zip(attr_cols, ATTRIBUTE_RULES):
        with attr_col:
            if st.checkbox(RULE_LABELS[rule], key=f"attr_{rule}"):
                selected_attributes.append(rule)
    exclude_downward = st.checkbox("業績予想の下方修正歴がある銘柄を除外する", value=True, key="exclude_downward")

    fin_col1, fin_col2 = st.columns(2)
    with fin_col1:
        per_ceiling = st.radio("PER（上限）", options=[_NO_LIMIT, *_PER_CEILINGS], key="per_ceiling")
        roe_floor = st.radio("ROE（下限）", options=[_NO_LIMIT, *_ROE_FLOORS], key="roe_floor")
    with fin_col2:
        dividend_floor = st.radio("配当利回り（下限）", options=[_NO_LIMIT, *_DIVIDEND_FLOORS], key="dividend_floor")
        margin_floor = st.radio("営業利益率（下限）", options=[_NO_LIMIT, *_MARGIN_FLOORS], key="margin_floor")

    sector_filter = st.text_input(
        "業種で絞り込む（部分一致・任意）", key="sector_filter", placeholder="例: 銀行業"
    )
    recently_listed_only = st.checkbox(
        "上場5年以内（推定）の銘柄のみ", key="recently_listed_only"
    )
    selected_count = (
        len(selected_attributes) + int(exclude_downward) + int(per_ceiling != _NO_LIMIT)
        + int(dividend_floor != _NO_LIMIT) + int(roe_floor != _NO_LIMIT) + int(margin_floor != _NO_LIMIT)
        + int(bool(sector_filter.strip())) + int(recently_listed_only)
    )
    st.caption(f"{selected_count}件選択中")

with st.container(border=True):
    st.markdown("### ⭐ テンバガー候補")
    tenx_enabled = st.checkbox("10倍株候補スコアを利用する", key="tenx_enabled")
    st.caption(
        "時価総額・増収率・利益成長等をJ-Quantsの決算データだけで採点し、結果テーブルに"
        "スコアと加点・減点理由を表示します。未来の業績を推計するものではありません。"
    )
    if tenx_enabled:
        st.caption(
            "四半期成長の加速・52週高値接近・出来高急増は、全銘柄分の長期株価データを新たに"
            "取得する必要があるため未実装です（今後追加予定）。"
        )
        market_cap_ceiling = st.radio(
            "時価総額（上限・テンバガー候補では重要）",
            options=[_NO_LIMIT, *_MARKET_CAP_CEILINGS],
            key="market_cap_ceiling",
        )
    else:
        market_cap_ceiling = st.session_state.get("market_cap_ceiling", _NO_LIMIT)

if st.button("スクリーニング実行", type="primary"):
    if not selected_events and not selected_attributes:
        st.error("イベント条件または属性条件を1つ以上選択してください。")
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
                    if tenx_enabled:
                        with st.spinner("10倍株候補スコアを計算しています…"):
                            tenx_scores = compute_tenx_scores(
                                client, start_date, end_date, summary,
                                downward_penalize_only=not exclude_downward,
                            )
                            summary = summary.merge(tenx_scores, on="Code", how="left")
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

    expected_cols = {f"{r}_matched" for r in (selected_events + selected_attributes)}
    if tenx_enabled:
        expected_cols.add("TenXScore")
    if not summary.empty and not expected_cols.issubset(summary.columns):
        st.warning("コード更新により前回の結果が古くなっています。もう一度「スクリーニング実行」を押してください。")
    elif summary.empty:
        st.warning("条件に合致した銘柄はありませんでした。")
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
            view["MarketCapOku"] = (view["MarketCap"] / 1e8).round(1)
            # 上限フィルターは⭐テンバガー候補カードが開いている(tenx_enabled)時だけ
            # 効かせる。閉じている間はウィジェットが非表示になり値を変更できないため、
            # 見えないフィルターが働き続けることを避ける。
            if tenx_enabled and market_cap_ceiling != _NO_LIMIT:
                view = view[view["MarketCapOku"] <= _MARKET_CAP_CEILINGS[market_cap_ceiling]]
        if per_ceiling != _NO_LIMIT and "PER" in view.columns:
            view = view[view["PER"] <= _PER_CEILINGS[per_ceiling]]
        if dividend_floor != _NO_LIMIT and "DividendYield" in view.columns:
            view = view[(view["DividendYield"] * 100) >= _DIVIDEND_FLOORS[dividend_floor]]
        if roe_floor != _NO_LIMIT and "ROE" in view.columns:
            view = view[(view["ROE"] * 100) >= _ROE_FLOORS[roe_floor]]
        if margin_floor != _NO_LIMIT and "OperatingMargin" in view.columns:
            view = view[view["OperatingMargin"] >= _MARGIN_FLOORS[margin_floor]]
        if sector_filter.strip() and "Sector" in view.columns:
            view = view[view["Sector"].str.contains(sector_filter.strip(), na=False)]
        if recently_listed_only and "RecentlyListed" in view.columns:
            view = view[view["RecentlyListed"].fillna(False)]

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

        # 危険フラグ：参考情報のみで、絞り込み条件には使わない。大株主売却・継続企業注記・
        # 公募増資（希薄化以外の意味での）は、J-Quantsの決算開示データからは検出できないため
        # 含めていない（README・仕様確認済み）。
        def _danger_flags(row) -> str:
            flags = []
            if row.get("HasDownwardRevision"):
                flags.append("⚠下方修正")
            negative_reasons = row.get("NegativeReasons")
            if isinstance(negative_reasons, str) and "発行済株式数" in negative_reasons:
                flags.append("⚠希薄化")
            return "、".join(flags)

        view["DangerFlags"] = view.apply(_danger_flags, axis=1)

        display_cols = [
            "Code", "CompanyName", "Sector", "MatchedCountSelected", "MatchedConditions",
            "StopHighDate", "MarketCapOku", "PER", "PBR", "DividendYield", "ROE", "OperatingMargin",
            "ListingDateDisplay", "HasDownwardRevision", "DangerFlags",
        ]
        rename_map = {
            "Sector": "業種",
            "MatchedCountSelected": "合致数",
            "MatchedConditions": "合致した条件",
            "StopHighDate": "ストップ高日付",
            "MarketCapOku": "時価総額(億円)",
            "PER": "PER(予想/実績年率換算)",
            "PBR": "PBR",
            "DividendYield": "配当利回り",
            "ROE": "ROE",
            "OperatingMargin": "営業利益率",
            "ListingDateDisplay": "推定上場日（近似）",
            "HasDownwardRevision": "下方修正歴あり",
            "DangerFlags": "危険フラグ（参考情報）",
        }
        sort_key = "合致数"
        if tenx_enabled and "TenXScore" in view.columns:
            view["TenXStars"] = view["TenXScore"].apply(
                lambda s: "★" * max(0, min(5, round(s / 2))) if pd.notna(s) else ""
            )
            display_cols += [
                "TenXScore", "TenXStars", "MarketCapBand", "ThreeYearRevenueGrowth", "RevenueCAGR",
                "ProfitGrowthRate", "MarginImprovement", "Turnaround", "UpwardRevisionPct",
                "UpwardRevisionCount", "ProgressRatio", "PositiveReasons", "NegativeReasons",
                "UndeterminedItems",
            ]
            rename_map.update({
                "TenXScore": "10倍株候補スコア",
                "TenXStars": "評価",
                "MarketCapBand": "時価総額区分",
                "ThreeYearRevenueGrowth": "3期連続増収",
                "RevenueCAGR": "売上CAGR",
                "ProfitGrowthRate": "経常利益成長率",
                "MarginImprovement": "経常利益率改善幅",
                "Turnaround": "黒字転換",
                "UpwardRevisionPct": "上方修正率",
                "UpwardRevisionCount": "上方修正回数",
                "ProgressRatio": "通期予想進捗率",
                "PositiveReasons": "加点理由",
                "NegativeReasons": "減点理由",
                "UndeterminedItems": "判定不能項目",
            })
            sort_key = "10倍株候補スコア"

        display_cols = [c for c in display_cols if c in view.columns]
        display = view[display_cols].rename(columns=rename_map).sort_values(sort_key, ascending=False)

        for pct_col in ["売上CAGR", "経常利益成長率", "上方修正率"]:
            if pct_col in display.columns:
                display[pct_col] = (display[pct_col] * 100).round(1)
        if "経常利益率改善幅" in display.columns:
            display["経常利益率改善幅"] = display["経常利益率改善幅"].round(1)
        if "通期予想進捗率" in display.columns:
            display["通期予想進捗率"] = display["通期予想進捗率"].round(1)

        if "PER(予想/実績年率換算)" in display.columns:
            display["PER(予想/実績年率換算)"] = display["PER(予想/実績年率換算)"].round(1)
        if "PBR" in display.columns:
            display["PBR"] = display["PBR"].round(1)
        if "配当利回り" in display.columns:
            display["配当利回り"] = (display["配当利回り"] * 100).round(1)
        if "ROE" in display.columns:
            display["ROE"] = (display["ROE"] * 100).round(1)
        if "営業利益率" in display.columns:
            display["営業利益率"] = display["営業利益率"].round(1)

        st.success(f"{len(display)} 銘柄が条件に合致しました（列見出しクリックでソート可能）。")
        if "危険フラグ（参考情報）" in display.columns:
            styled = display.style.map(
                lambda v: "color: #d32f2f; font-weight: bold" if v else "",
                subset=["危険フラグ（参考情報）"],
            )
            st.dataframe(styled, width="stretch", hide_index=True)
        else:
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
