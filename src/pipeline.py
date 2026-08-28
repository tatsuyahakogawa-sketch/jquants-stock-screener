"""スクリーニング全体のオーケストレーション。

指定期間について株価・決算開示データを取得し、各ルールを適用して
「どの銘柄が、いつ、どの条件に合致したか」の一覧、および銘柄単位で
集約したサマリーを返す。
"""
from __future__ import annotations

import datetime as dt
import logging

import pandas as pd

from src import endpoints, rules, score, tdnet_client
from src.config import (
    LISTING_LOOKBACK_YEARS,
    LISTING_DATE_BOUNDARY_TOLERANCE_DAYS,
    MAX_TDNET_DISCLOSURES_FOR_SCREENING,
    PROFIT_DOUBLING_YEARS,
)
from src.jquants_client import JQuantsClient
from src.jst import today_jst

logger = logging.getLogger(__name__)

RULE_LABELS = {
    "stop_high": "ストップ高",
    "sales_growth_major": "売上高が大幅に増加（前年同期比+20%以上）",
    "sales_growth_explosive": "売上高が爆発的に増加（前年同期比+50%以上）",
    "sales_growth_doubling": "売上高が1年で2倍以上（選択期間に関係なく銘柄ごとの最新開示が対象）",
    "earnings_beat": "本決算が会社予想を上回った",
    "stock_split": "株式分割の発表",
    "stock_consolidation": "株式併合の発表",
    "equity_ratio_high": "自己資本比率60%以上",
    "profit_doubling": "経常利益が4年で2倍以上",
    "profit_growth_major": "経常利益が前年同期比+50%以上（1.5倍以上）",
    "pbr_low": "PBR1倍以下",
    "two_quarter_growth": "四半期決算2期連続増収増益",
    "market_upgrade_to_prime": "スタンダード/グロースからプライムへの市場変更の発表",
    "jpx_nikkei_400": "「JPX日経インデックス400」構成銘柄への選定・採用の発表",
    "new_facility_or_store": "新工場・新店舗の開示",
    "exchange_transfer_to_tokyo": "札幌/福岡/名古屋証取から東証への上場",
    "large_order": "大型・大口受注の発表",
    "world_first": "世界初の製品・サービスの発表",
    "downward_revision": "業績予想の下方修正（マイナス要因）",
}

# タイトルのキーワード一致で判定しているため誤検出の可能性があるルール
# （detail列・開示タイトルで内容を確認する運用が前提）。
TDNET_TITLE_BASED_RULES = [
    "stock_split",
    "stock_consolidation",
    "market_upgrade_to_prime",
    "jpx_nikkei_400",
    "new_facility_or_store",
    "exchange_transfer_to_tokyo",
    "large_order",
    "world_first",
]

# サマリーの「合致数」に含めるルール（downward_revisionはマイナス要因なので除外用に別扱い）
POSITIVE_RULES = [r for r in RULE_LABELS if r != "downward_revision"]
NEGATIVE_RULES = ["downward_revision"]

# PBR・自己資本比率は「いつ起きたか」というイベントではなく、開示時点での銘柄の
# 属性（状態）を見るルールのため、UI上はイベント条件とは別枠の絞り込みとして扱う。
ATTRIBUTE_RULES = ["pbr_low", "equity_ratio_high"]
EVENT_RULES = [r for r in POSITIVE_RULES if r not in ATTRIBUTE_RULES]

# 前年同期（〜PROFIT_DOUBLING_YEARS年前）との比較が必要なため、決算データを
# 数年分遡って取得しないと判定できないルール。選択されていない場合は取得期間を
# start〜endだけに絞って高速化する（run_screeningのselected_rules引数を参照）。
YOY_LOOKBACK_RULES = [
    "sales_growth_major",
    "sales_growth_explosive",
    "sales_growth_doubling",
    "two_quarter_growth",
    "profit_doubling",
    "profit_growth_major",
]

# YOY_LOOKBACK_RULES（sales_growth_doublingを除く。専用の別軸取得を持つため）の
# 各ルールが実際に必要とする比較用の遡り日数。profit_doublingだけが
# PROFIT_DOUBLING_YEARS(4年)前まで必要で、それ以外は前年同期
# (330〜400日前、各detect_*のgap_days.between参照)の比較で足りる。
# run_screeningの決算データ取得は全ルール共通でこの中の最大値
# (profit_doubling向け)を遡り日数として1回だけ取得するため、Lightプランの
# 取得可能期間の下限でクランプされた際に「実際にどのルールが影響を
# 受けたか」を判定するために使う（2026-08-27の2巡目のCodexレビューで
# 指摘・修正: 修正前はクランプが発生した時点で無条件に全YOY_LOOKBACK_RULES
# 分の警告を出しており、1年分の比較で足りるルールしか選択していない
# 場合でも不要な警告が出ていた）。
_LEGACY_LOOKBACK_RULE_REQUIRED_DAYS = {
    "sales_growth_major": 365 + 60,
    "sales_growth_explosive": 365 + 60,
    "two_quarter_growth": 365 + 60,
    "profit_doubling": 365 * PROFIT_DOUBLING_YEARS + 60,
    "profit_growth_major": 365 + 60,
}


def _years_before(date: dt.date, years: int) -> dt.date:
    """dateのちょうどyears年前の暦日を返す（うるう年の2/29はその年に
    2/29が無ければ2/28にする）。単純な365*years日での近似は、5年間に
    含まれるうるう日の数だけ実際の暦日と1〜2日ずれる（2026-08-27の
    Codexレビューで指摘: J-Quantsの契約プラン取得可能期間の起点は暦日
    ベースのため、日数での近似では境界ちょうどの開示を取りこぼしうる）。
    """
    try:
        return date.replace(year=date.year - years)
    except ValueError:
        return date.replace(month=2, day=28, year=date.year - years)


def run_screening(
    client: JQuantsClient,
    start: dt.date,
    end: dt.date,
    selected_rules: list[str] | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """start〜end の期間で全ルールを適用し、イベント単位の結果をまとめて返す。

    selected_rules: 画面上で選択中のルール名一覧。Noneの場合は従来通り常に
    数年分の決算データを遡って取得する（後方互換のデフォルト）。指定された
    場合、YOY_LOOKBACK_RULESが1つも含まれていなければ決算データの取得期間を
    start〜endに限定し、遡り取得を省略して高速化する。

    戻り値: (イベント単位の結果DataFrame(列: Code, CompanyName, Rule, RuleLabel,
    Date, Detail), ユーザーへの注意メッセージ一覧)。

    注意メッセージ（TDnet取得失敗・TDnet開示件数の上限超過等）は、以前は
    Python標準のwarnings.warn()で発していたが、warnings.catch_warnings()は
    プロセスグローバルなwarnings.filters/showwarning実装を書き換えるため
    スレッドセーフではない。Streamlitは複数セッションを同一プロセス内の
    別スレッドで同時実行しうるため、あるセッションの警告を別セッションが
    捕捉してしまう競合が起こりうる。そのため戻り値として明示的に返す方式に
    変更した（2026-08-24の2巡目のCodexレビューで指摘・修正）。
    """
    listed_info = endpoints.get_listed_info(client)
    name_map: dict[str, str] = {}
    sector_map: dict[str, str] = {}
    valid_codes: set[str] = set()
    if not listed_info.empty and "Code" in listed_info.columns:
        valid_codes = set(listed_info["Code"].astype(str))
        if "CoName" in listed_info.columns:
            name_map = dict(zip(listed_info["Code"], listed_info["CoName"]))
        if "S33Nm" in listed_info.columns:
            sector_map = dict(zip(listed_info["Code"], listed_info["S33Nm"]))
        # TOKYO PRO MARKETはプロ投資家向け市場で、個人が通常の口座では取引できず、
        # 決算開示(/fins/summary)も株価データもほぼ提供されないため、時価総額・
        # PER等が全く出せない「空欄だらけ」の行になってしまう。この銘柄群は
        # 通常のスクリーニング対象から除外する。
        if "MktNm" in listed_info.columns:
            pro_market_codes = set(
                listed_info.loc[listed_info["MktNm"] == "TOKYO PRO MARKET", "Code"].astype(str)
            )
            valid_codes -= pro_market_codes

    # quotes_df・statements_dfは、選択中のイベント/属性ルールだけでなく、
    # 結果テーブルの常時表示列（ストップ高日付はdetect_stop_high、
    # 「下方修正歴あり」列はdetect_downward_revisionの出力から作られ、
    # どちらも選択中のルールに関わらず常に表示・除外フィルターの対象と
    # なる。app.py参照）のためにも必要なため、selected_rulesの内容に
    # 関わらず常に取得する（2026-08-25の6巡目のCodexレビューでこの2つの
    # 取得をsales_growth_doubling/TDnet系ルールのみ選択時に丸ごと省略する
    # 最適化を入れたが、7巡目のレビューで「下方修正歴あり」列がその
    # 最適化により常にFalseになり、デフォルトON(exclude_downward)の除外
    # フィルターが効かなくなる不具合を指摘され、同じ理由でストップ高日付
    # 列も壊れることが分かったため、この最適化自体を取りやめた）。
    messages: list[str] = []
    quotes_df = endpoints.get_daily_quotes_range(client, start, end)
    # 増収率(YoY)・増収増益2期連続・経常利益4年倍増の各ルールは、対象開示より
    # 1〜4年前の同期(同じCurPerType)の開示と比較する必要がある。statements_df を
    # start〜end だけで取得すると比較対象の過去開示がそもそも取得できておらず、
    # 前年同期比較が常にNaNになってヒットが極端に少なくなってしまう
    # （選択期間が1年以上にならない限り比較不能）。比較用に必要な分だけ遡って
    # 取得し、実際のヒットは後段でstart〜end開示分に絞り込む。
    # YOY_LOOKBACK_RULESが選択されていない場合はこの遡り取得自体が不要なため、
    # start〜endのみに絞って取得を高速化する。sales_growth_doublingは専用の
    # 別軸取得を持ち、ここでのstatements_dfには使われないため、この遡り取得
    # の要否判定からは除外する（除外しないと、sales_growth_doublingと
    # YOY_LOOKBACK_RULES以外のルールだけを組み合わせて選択した場合にも、
    # 使われないデータのために約4年分遡った開始日を計算してしまう）。
    legacy_lookback_rules = [r for r in YOY_LOOKBACK_RULES if r != "sales_growth_doubling"]
    needs_lookback = selected_rules is None or any(r in legacy_lookback_rules for r in selected_rules)
    # 選択中の legacy_lookback_rules だけを見て、実際に必要な遡り日数の最大値
    # だけ遡る。以前は選択内容に関わらず一律でprofit_doubling向けの
    # 365*PROFIT_DOUBLING_YEARS+60日(約4年)を遡っていたため、1年分の比較で
    # 足りるsales_growth_major/explosive/two_quarter_growth/profit_growth_major
    # だけを選んだ場合でも、使われない3年分のデータのために大幅に余計な
    # 取得時間（冷えたキャッシュでLightプランの60件/分の制限下、数十分単位）が
    # かかっていた（2026-08-27の3巡目のCodexレビューで指摘・修正。
    # affected_rulesの判定でも同じ選択集合を使うため、後段で再計算せず
    # ここで求めた値を使い回す）。
    selected_legacy_rules = (
        legacy_lookback_rules if selected_rules is None
        else [r for r in legacy_lookback_rules if r in selected_rules]
    )
    if needs_lookback:
        comparison_lookback_days = max(_LEGACY_LOOKBACK_RULE_REQUIRED_DAYS[r] for r in selected_legacy_rules)
        statements_fetch_start = start - dt.timedelta(days=comparison_lookback_days)
    else:
        statements_fetch_start = start
    # startがユーザー選択で既に1〜2年以上前の場合、そこからさらに約4年+60日
    # (comparison_lookback_days)遡ると、Lightプランの契約プランの取得可能
    # 期間（過去5年、LISTING_LOOKBACK_YEARS）を超えてしまうことがある。
    # 超えた状態でget_statements_rangeを呼ぶとJ-Quantsから400エラー
    # （"Your subscription covers the following dates: ..."）で拒否され、
    # ユーザー自身が選んだstart〜end自体は取得可能な範囲内であるにも
    # 関わらずスクリーニングが全く実行できなくなる（実機で確認: 2026-08-27に
    # 開始日を2年前(2024-08-27)に設定しsales_growth_explosiveを選択した
    # ところ、遡り取得後の開始日が2020-06-29相当になり、5年の境界
    # (2021-08-27頃)を超えて拒否された）。
    # 365*5日での近似ではなく、実際の「5年前」の暦日で境界を計算する
    # （うるう年を含む場合、日数での近似は実際の境界より1〜2日新しい側に
    # ずれ、その1〜2日にちょうど該当する比較用開示だけを取りこぼしうる。
    # 2026-08-27の2巡目のCodexレビューで指摘・修正）。
    earliest_available_statements_date = _years_before(today_jst(), LISTING_LOOKBACK_YEARS)
    if statements_fetch_start < earliest_available_statements_date:
        # クランプによって、比較用に遡って取得したかった一部の決算データが
        # 実際には取得できていない。この状態でもAPI呼び出し自体は成功して
        # しまうため、本来ヒットすべき銘柄が「合致なし」として静かに扱われて
        # しまう可能性がある。データが揃わず判定できなかったことを
        # ユーザーに明示する（2026-08-27の2巡目のCodexレビューで指摘・修正）。
        if needs_lookback:
            # ただし影響があるのは、実際に必要とする遡り日数が今回
            # クランプされた範囲を超えているルールだけ（例: 1年分の比較で
            # 足りるsales_growth_major/explosive/two_quarter_growthは、
            # startが1年程度前までならクランプが発生していても無関係）。
            # 選択中の全YOY_LOOKBACK_RULESを一律に警告すると、影響を受けて
            # いないルールしか選んでいない場合にも不要な警告が出てしまう
            # （2026-08-27の2巡目のCodexレビューで指摘・修正。
            # selected_legacy_rulesは上のcomparison_lookback_days計算と
            # 同じものを使い回す）。
            available_days_from_start = (start - earliest_available_statements_date).days
            affected_rules = [
                r for r in selected_legacy_rules
                if _LEGACY_LOOKBACK_RULE_REQUIRED_DAYS[r] > available_days_from_start
            ]
            if affected_rules:
                affected_labels = "、".join(RULE_LABELS[r] for r in affected_rules)
                messages.append(
                    "選択期間の一部（開始日に近い側）は、比較に必要な前年同期以前の"
                    f"決算データがLightプランの取得可能期間（{earliest_available_statements_date:%Y-%m-%d}"
                    f"以降）を超えるため取得できませんでした。次の条件は、この期間の"
                    f"一部で正しく判定できていない可能性があります: {affected_labels}"
                )
        else:
            messages.append(
                f"開始日がLightプランの取得可能期間（{earliest_available_statements_date:%Y-%m-%d}"
                "以降）より前のため、それより前の決算データは取得できませんでした。"
            )
        statements_fetch_start = earliest_available_statements_date
    statements_df = endpoints.get_statements_range(client, statements_fetch_start, end)

    hits = [
        rules.detect_stop_high(quotes_df),
        rules.detect_sales_growth(statements_df),
        rules.detect_earnings_beat(statements_df),
        rules.detect_equity_ratio(statements_df),
        rules.detect_profit_doubling(statements_df),
        rules.detect_profit_growth_major(statements_df),
        rules.detect_low_pbr(statements_df, quotes_df),
        rules.detect_two_quarter_growth(statements_df),
        rules.detect_downward_revision(statements_df),
    ]

    # "sales_growth_doubling"(1年で売上高2倍)は選択期間(start〜end)内の
    # イベントではなく「銘柄が現在その状態にあるか」を判定するため、
    # start〜endに依存しない別軸で"今日"を基準に決算データを取得し直す
    # （endがユーザーの指定した過去日であっても、この判定だけは常に最新
    # 開示を見る必要があるため。上のstatements_dfをそのまま使うとendより
    # 後の開示が判定に使えず「常に最新」にならない）。選択されていない
    # 場合は無駄なAPI呼び出し・後段のenrich_with_market_data呼び出しを
    # 避けるためスキップする（2026-08-25のCodexレビューで指摘・修正）。
    doubling_requested = selected_rules is None or "sales_growth_doubling" in selected_rules
    if doubling_requested:
        today = today_jst()
        # detect_current_sales_doublingは直近の決算期とその前年同期
        # (330〜400日前)の1回分の比較しか行わないため、PROFIT_DOUBLING_YEARS
        # (4年)は不要。ただし単純に1年+バッファ(60日)だけ遡ると、銘柄の
        # 直近の決算期の開示自体が"今日"から見て古い場合（例: 決算期末から
        # 開示まで数ヶ月かかる銘柄、開示が遅れがちな銘柄）に、その前年同期
        # の比較対象がこの遡り範囲より前になってしまい取得できないことが
        # ある（2026-08-25の6巡目のCodexレビューで指摘・実例: "今日"が
        # 2026-05-01で直近開示が2026-02-10の場合、その前年同期2025-02-10は
        # 425日遡っただけでは範囲外になる）。開示自体が最大で1年ほど古い
        # 場合でも前年同期に届くよう、1年(前年同期比較用)+1年(開示の
        # 遅延に対する安全マージン)+バッファ(60日)を遡る。
        # get_statements_rangeは1日ごとに個別リクエストするため、4年分
        # (約1521日)を遡ると冷えたキャッシュではLightプランの呼び出し制限
        # (60件/分)だけで約25分以上かかり、このルールを選ぶだけで実用的で
        # なくなっていた（2026-08-25の5巡目のCodexレビューで指摘・修正）。
        # 2年+60日(約790日)ならその半分程度(約13分)で済む。この約13分という
        # 初回・キャッシュが冷えている場合のコストは、8巡目のCodexレビューで
        # 「一日ごとの個別リクエストという設計自体が非効率」と指摘された。
        # 理屈上は正しいが、これは本PR固有の問題ではなく、同じ
        # get_statements_rangeを使うprofit_doubling（経常利益4年倍増、
        # 遡り日数は本ルールの倍の約1520日）等、既存の複数年遡及ルールが
        # 以前から共通して持つ制約であり、日付単位バルク取得+ローカル/
        # Supabaseキャッシュという設計（src/endpoints.py参照）を採用している
        # 以上、初回だけこの遅さを受け入れる（2回目以降は当日分を除き
        # キャッシュ済みになるため高速）というのが既存の前提になっている。
        # 銘柄ごとの決算データを差分更新で蓄積する専用ストア（例:
        # src/regional_stocks.pyの地方株スキャンが採用している「前回スキャン
        # 日以降だけ追加取得」方式）に作り直すのがより良い解決策だが、それは
        # 本PRの範囲(sales_growth_doublingが選択期間を無視する不具合の修正)を
        # 超えるアーキテクチャ変更であり、他の複数年遡及ルールにも影響する
        # ため別タスクとして扱う（2026-08-25、ユーザーとの合意なくClaude Code
        # の判断でスコープ外とした）。
        doubling_lookback_days = 365 * 2 + 60
        doubling_statements_df = endpoints.get_statements_range(
            client, today - dt.timedelta(days=doubling_lookback_days), today
        )
        hits.append(rules.detect_current_sales_doubling(doubling_statements_df))

    # ユーザーがTDNET_TITLE_BASED_RULESを1つも選択していない場合（例: ストップ高・
    # PBRのみ選択）、TDnet開示件数がいくら多くても検索結果には反映されないため、
    # 件数上限チェック・警告は行わない（無関係な警告で「期間を絞り込め」と
    # 誤って促してしまうことを防ぐ。2026-08-24のCodexレビューで指摘・修正）。
    tdnet_rule_requested = selected_rules is None or any(r in TDNET_TITLE_BASED_RULES for r in selected_rules)

    try:
        disclosures_df = tdnet_client.get_disclosures_range(start, end)
        if tdnet_rule_requested and len(disclosures_df) > MAX_TDNET_DISCLOSURES_FOR_SCREENING:
            # 期間が広すぎる等でTDnet開示が大量に該当する場合、タイトルの
            # キーワード一致で判定するTDNET_TITLE_BASED_RULES（新工場・新店舗・
            # 東証移籍・株式分割・株式併合・プライム市場変更・「JPX日経
            # インデックス400」選定・大型受注・世界初の発表）の検索は行わない
            # （それ以上の処理を止めてユーザーに知らせ、期間を絞り込んでもらう。
            # 他のルール(J-Quants由来)の結果はそのまま返す。2026-08-24に
            # ユーザーが指定）。
            messages.append(
                f"指定期間のTDnet開示件数が{len(disclosures_df)}件と多く、上限"
                f"（{MAX_TDNET_DISCLOSURES_FOR_SCREENING}件）を超えたため、新工場・新店舗・"
                "東証移籍・株式分割・株式併合・プライム市場変更・「JPX日経インデックス400」選定・"
                "大型受注・世界初の発表の検索を行いませんでした。期間を絞り込んで再実行してください。"
            )
        else:
            hits.append(rules.detect_new_facility_or_store(disclosures_df))
            hits.append(rules.detect_exchange_transfer_to_tokyo(disclosures_df))
            hits.append(rules.detect_stock_split(disclosures_df))
            hits.append(rules.detect_market_upgrade_to_prime(disclosures_df))
            hits.append(rules.detect_jpx_nikkei_400_selection(disclosures_df))
            hits.append(rules.detect_large_order(disclosures_df))
            hits.append(rules.detect_world_first(disclosures_df))
    except Exception as e:
        # TDnetの非公式ミラーは個人運営で不安定なことがあるため、失敗しても
        # 他のルールの結果は返す（README参照）。ユーザーがTDNET_TITLE_BASED_RULES
        # を1つも選択していない場合は、この失敗が検索結果に一切影響しないため
        # メッセージも出さない（無関係な「7件の検索をスキップしました」表示で
        # 完了した検索結果が不完全であるかのように見せてしまうことを防ぐ。
        # 2026-08-24の3巡目のCodexレビューで指摘・修正）。
        if tdnet_rule_requested:
            messages.append(
                "TDnet開示情報の取得に失敗しました（新工場・新店舗・東証移籍・株式分割・株式併合・"
                f"プライム市場変更・「JPX日経インデックス400」選定・大型受注・世界初の発表の"
                f"検出をスキップします）: {e}"
            )

    hits = [h for h in hits if not h.empty]
    if not hits:
        return pd.DataFrame(columns=["Code", "CompanyName", "Rule", "RuleLabel", "Date", "Detail"]), messages

    result = pd.concat(hits, ignore_index=True)
    result = result.rename(columns={"rule": "Rule", "detail": "Detail"})
    result["Code"] = result["Code"].astype(str)
    if valid_codes:
        # 現在の上場銘柄マスタに存在しない銘柄（上場廃止済み等、決算開示だけが
        # 残っているケース）は、会社名も株価・時価総額も取得できず「空欄だらけ」
        # の行になってしまうため除外する。
        result = result.loc[result["Code"].isin(valid_codes)]
    result["CompanyName"] = result["Code"].map(name_map).fillna("")
    result["Sector"] = result["Code"].map(sector_map).fillna("")
    result["RuleLabel"] = result["Rule"].map(RULE_LABELS).fillna(result["Rule"])
    result["Date"] = pd.to_datetime(result["Date"])
    # 比較用に遡って取得した過去開示分がヒットに混ざらないよう、実際の開示日が
    # ユーザーの選択期間(start〜end)に入っているものだけに絞り込む。
    # TDnet由来のrule（stock_split・new_facility_or_store等）のDateは
    # pubdateの時刻情報を保持しているため（例: "2026-08-24 16:30:00"）、
    # pd.Timestamp(end)（=その日の0時0分）とのend<=比較では同日の開示が
    # ほぼ全て弾かれてしまっていた（UIのデフォルトが「終了日=開始日」の
    # 1日だけの範囲であるため、この不具合により対象ルールが実質機能して
    # いなかった。2026-08-24のCodexレビューで指摘・修正）。end翌日0時未満
    # という排他的な上限にすることで、時刻情報の有無によらずend当日を
    # 正しく含める。
    # "sales_growth_doubling"(1年で売上高2倍)は、四半期に1回しか出ない決算
    # 短信が選択期間(start〜end)にたまたま入っているかという「期間内の
    # イベント」ではなく、「銘柄が現在2倍成長という状態にあるか」という
    # 現在の状態を知りたい用途で使われる。UIのデフォルトである「終了日=
    # 開始日」の1日だけの範囲では、決算のタイミングと一致しない限り
    # ほとんど何もヒットしない（2026-08-25にユーザー報告・実データで確認：
    # 実際には多数の該当銘柄があるのに、期間フィルタのせいで0件になって
    # いた）。このルールだけ選択期間による絞り込みを行わない
    # （detect_current_sales_doublingが"今日"基準で銘柄ごとに最新の該当
    # 開示1件だけを既に返しているため、start〜endでの絞り込みはせずそのまま
    # 採用する。sales_growth_major/explosiveは従来通り「期間内のイベント」
    # として使えるよう、この特別扱いはsales_growth_doublingだけに限定する。
    # 2026-08-25のCodexレビューで、閾値を満たす行を先に集めてから最新を選ぶと
    # 成長が鈍化した後も古い開示がヒットし続けるバグを指摘され、選択は
    # detect_current_sales_doubling側（最新1件を選んでから閾値判定）に
    # 一本化した）。
    is_doubling = result["Rule"] == "sales_growth_doubling"
    in_range = (result["Date"] >= pd.Timestamp(start)) & (result["Date"] < pd.Timestamp(end) + pd.Timedelta(days=1))
    result = pd.concat([result.loc[is_doubling], result.loc[~is_doubling & in_range]], ignore_index=True)
    result = result.sort_values(["Date", "Code"]).reset_index(drop=True)
    return result[["Code", "CompanyName", "Sector", "Rule", "RuleLabel", "Date", "Detail"]], messages


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


# EPSはCurPerType（1Q/2Q/3Q/FY等）の期間累計値で開示されるため、実績EPSを
# 年率換算する際に使う（表示するPERには使わず、source_perという参考値・
# 乖離チェック用にのみ使う。過去実績EPSと最新株価を組み合わせて表示用PERを
# 計算しない）。
_EPS_ANNUALIZE_FACTOR = {"1Q": 4.0, "2Q": 2.0, "3Q": 4 / 3, "4Q": 1.0, "FY": 1.0}

# 予想EPS基準PER(calculated_per)と実績EPS基準PER(source_per)がこの割合以上
# 乖離した場合、EPSの取得ミス・分割調整前後の混在等を疑ってログに警告を出す。
_PER_SANITY_DIVERGENCE_THRESHOLD = 0.20


def _last_valid_value_with_date(df: pd.DataFrame, column: str) -> tuple[float | None, pd.Timestamp | None]:
    """dfの中でcolumnが数値として読める最後の（最新の）値と、その開示日を返す。

    決算短信は開示形式によって一部の項目（BPS等）が入っていない回があるため、
    直近1件だけでなく履歴全体から「最後に開示された値」を探す。開示日を返すのは、
    後で株式分割・併合による1株当たり指標のズレを補正するために必要なため。
    """
    if column not in df.columns or "DiscDate" not in df.columns:
        return None, None
    values = pd.to_numeric(df[column], errors="coerce").dropna()
    if values.empty:
        return None, None
    idx = values.index[-1]
    return values.loc[idx], df.loc[idx, "DiscDate"]


def _last_valid_value(df: pd.DataFrame, column: str) -> float | None:
    value, _ = _last_valid_value_with_date(df, column)
    return value


def _last_valid_full_year_value_with_date(df: pd.DataFrame, column: str) -> tuple[float | None, pd.Timestamp | None]:
    """dfの中でcolumnが数値として読める最後の値と開示日を、CurPerType=='FY'の行だけから探す。

    会社予想の修正開示(EarnForecastRevision等)の中には、CurPerTypeが四半期
    (1Q等)のまま会社予想値を更新しているものがあり、単純に「最後に開示された値」
    を取ると通期予想と四半期限定の値を区別できず、PER等の計算が大きく壊れる
    （例: 通期予想EPSのつもりで四半期分の小さい値を使ってしまう）。そのため
    会社予想EPS(FEPS)等は必ず通期(FY)区分の開示に限定する。
    """
    if column not in df.columns or "CurPerType" not in df.columns:
        return None, None
    return _last_valid_value_with_date(df.loc[df["CurPerType"] == "FY"], column)


def _last_valid_full_year_value(df: pd.DataFrame, column: str) -> float | None:
    value, _ = _last_valid_full_year_value_with_date(df, column)
    return value


def _last_valid_full_year_forecast_eps_with_date(df: pd.DataFrame) -> tuple[float | None, pd.Timestamp | None]:
    """会社予想EPSを、直近のFY区分開示から取得する。FEPS(今期・連結)→FNCEPS(今期・
    非連結)→NxFEPS(来期・連結)→NxFNCEPS(来期・非連結)の順にフォールバックする。

    - FEPS→FNCEPS: 決算発表直前の業績予想の修正等で、直近のFY区分開示がFEPS
      (連結)を出さずFNCEPS(非連結)だけを更新していることがある。
    - FEPS/FNCEPS→NxFEPS/NxFNCEPS: 直近のFY区分開示が本決算実績そのものの場合、
      今期分の予想(FEPS/FNCEPS)は実績確定済みのため空になり、来期の予想
      (NxFEPS/NxFNCEPS)だけが入っている（例: 4527・5711・3422は直近が本決算
      実績の開示で、FEPSは空・NxFEPSに来期予想が入っていた）。
    単純に_last_valid_full_year_value_with_date(df, "FEPS")を使うと、その開示
    より前の古いFEPSまで遡ってしまい直近の会社予想を反映できないため、直近の
    FY開示から1件ずつ確認し、同じ開示の中で優先順に確認する（excel_export.py
    のFOdP→FOP、IFRS採用企業向けフォールバックと同じ考え方）。
    """
    if "CurPerType" not in df.columns or "DiscDate" not in df.columns:
        return None, None
    fy_rows = df.loc[df["CurPerType"] == "FY"].sort_values("DiscDate")
    for _, row in fy_rows.iloc[::-1].iterrows():
        for column in ("FEPS", "FNCEPS", "NxFEPS", "NxFNCEPS"):
            if column not in row.index:
                continue
            value = pd.to_numeric(pd.Series([row[column]]), errors="coerce").iloc[0]
            if pd.notna(value):
                return float(value), row["DiscDate"]
    return None, None


def _dividend_forecast_or_trailing_with_date(df: pd.DataFrame) -> tuple[float | None, pd.Timestamp | None]:
    """年間配当（1株あたり）を、開示されている中で最も「今の実力」に近い値と開示日で返す。

    本決算実績の開示時点ではFDivAnn（当期の配当予想）は確定済みのため空になり、
    NxFDivAnn（来期の配当予想）が入る。会社によってはさらに、業績が定まらない
    等の理由で配当予想自体を出さない（FDivAnn・NxFDivAnnとも常に空）こともある。
    その場合は「無配」と区別するため、直前に実際に支払われた年間配当(DivAnn)を
    参考値として使う（会社予想ではなく実績配当に基づく利回りになる）。
    """
    if "CurPerType" not in df.columns:
        return None, None
    fy_rows = df.loc[df["CurPerType"] == "FY"]
    for column in ("FDivAnn", "NxFDivAnn", "DivAnn"):
        value, date = _last_valid_value_with_date(fy_rows, column)
        if value is not None and pd.notna(value):
            return value, date
    return None, None


def _dividend_forecast_or_trailing(df: pd.DataFrame) -> float | None:
    value, _ = _dividend_forecast_or_trailing_with_date(df)
    return value


def _last_valid_eps_annualized_with_date(df: pd.DataFrame) -> tuple[float | None, pd.Timestamp | None]:
    """実績EPS(EPS)の最後の開示値を、その開示時点のCurPerTypeに応じて年率換算し、開示日も返す。"""
    if "EPS" not in df.columns:
        return None, None
    eps_numeric = pd.to_numeric(df["EPS"], errors="coerce")
    valid_idx = eps_numeric.dropna().index
    if len(valid_idx) == 0:
        return None, None
    idx = valid_idx[-1]
    period_type = df.loc[idx, "CurPerType"] if "CurPerType" in df.columns else None
    factor = _EPS_ANNUALIZE_FACTOR.get(period_type, 1.0)
    return eps_numeric.loc[idx] * factor, df.loc[idx, "DiscDate"]


def _last_valid_eps_annualized(df: pd.DataFrame) -> float | None:
    value, _ = _last_valid_eps_annualized_with_date(df)
    return value


def _split_adjustment_since(price_history: pd.DataFrame, since_date) -> float:
    """since_date時点の1株当たり指標(EPS/BPS/年間配当等)を、現在の株式数基準に
    換算するための倍率を返す。

    J-QuantsのAdjC(調整後終値)は、その後に起きた株式分割・併合を遡って反映した
    終値のため、ある日の「調整後終値÷未調整終値」がその日以降に起きた分割等の
    累積倍率と一致する。決算開示のEPS/BPS/配当は開示当時の株式数のままで、後から
    分割があっても遡って調整されないため、開示日以降に分割があると現在の株価
    (調整済み)と組み合わせたPER/PBR/配当利回りの計算がずれる
    （例: 開示後に1→8の分割があると、PER/PBRが実際の8倍小さく、配当利回りが
    実際の8倍大きく出てしまう）。分割等が無い、またはデータが無い場合は1.0
    （無調整）を返す。

    薄商いで売買が成立しなかった日（O/H/L/C全てnullの行）がsince_date以前の
    直近行になる場合、そのまま使うとC/AdjCが両方nullで「分割等が無い」扱い
    (1.0固定)になってしまい、実際には分割があってもPER/PBR/配当利回りが
    誤って無調整のまま計算される。latest_close側の無取引日フォールバック
    （_nearest_close・compute_market_metricsの最新終値算出）により、以前は
    無取引日のせいでlatest_close自体が空欄になっていた銘柄でもPER/PBR/配当
    利回りが計算されるようになったため、この関数側の同種の欠陥も顕在化する
    （2026-08-24のCodexレビューで指摘・修正）。C・AdjCが両方揃っている行だけ
    を対象に直近を探す。
    """
    if since_date is None or pd.isna(since_date):
        return 1.0
    if price_history.empty or not {"Date", "C", "AdjC"}.issubset(price_history.columns):
        return 1.0
    p = price_history.copy()
    p["Date"] = pd.to_datetime(p["Date"], errors="coerce")
    p["C"] = _to_numeric(p["C"])
    p["AdjC"] = _to_numeric(p["AdjC"])
    p = p.dropna(subset=["Date", "C", "AdjC"]).sort_values("Date")
    on_or_before = p.loc[p["Date"] <= pd.Timestamp(since_date)]
    if on_or_before.empty:
        return 1.0
    row = on_or_before.iloc[-1]
    c = row["C"]
    adj_c = row["AdjC"]
    if c == 0:
        return 1.0
    return float(adj_c / c)


def _latest_operating_margin(fins: pd.DataFrame) -> float | None:
    """直近開示(期間区分を問わない)の売上高・営業利益から営業利益率(%)を計算する。

    四半期の値も含めて「直近の実力」を見る（経常利益率は10倍株候補スコア側で
    別途、前年同期比較の文脈で計算しているため、ここではPER等と同じく単純に
    最新開示1件のスナップショットとする）。
    """
    if "OP" not in fins.columns or "Sales" not in fins.columns:
        return None
    d = fins.copy()
    d["Sales"] = _to_numeric(d["Sales"])
    d["OP"] = _to_numeric(d["OP"])
    d = d.dropna(subset=["Sales", "OP", "DiscDate"])
    d = d.loc[d["Sales"] != 0]
    if d.empty:
        return None
    row = d.sort_values("DiscDate").iloc[-1]
    return float(row["OP"] / row["Sales"] * 100)


def compute_market_metrics(
    fins: pd.DataFrame,
    price_history: pd.DataFrame,
    fallback_price: float | None = None,
    fallback_price_date: dt.date | None = None,
) -> dict:
    """財務履歴と株価履歴から、時価総額・PER・PBR・配当利回り・最新終値等を計算する。

    enrich_with_market_data（銘柄集計テーブル用）とexcel_export（Excel出力用）の
    両方から呼ばれる共通ロジック。

    地方単独上場企業等、J-Quantsに株価データが無い(price_historyが空の)銘柄
    向けに、呼び出し側でyfinance等から取得した参考価格をfallback_priceとして
    渡せる。その場合、EPS/BPS/発行済株式数はJ-Quants由来のまま、株価だけを
    fallback_priceで補ってPER/PBR/配当利回りを計算する
    （price_sourceが"yfinance"になり、J-Quants実データとの区別ができる）。
    ただし時価総額だけは計算しない。発行済株式数(ShOutFY/TrShFY)はJ-Quantsが
    その銘柄を最後に追えていた時点(東証離脱前)の値のままで、fallback_priceは
    現在値のため、両者を掛け合わせると異なる時点の値を混在させた誤った時価
    総額になる（src/regional_stocks.pyのfetch_regional_share_priceのdocstring
    参照。2026-08-20のCodexレビューで指摘）。
    """
    latest_close = None
    latest_price_date = None
    price_source = None
    if not price_history.empty and "Date" in price_history.columns and "C" in price_history.columns:
        price_history = price_history.copy()
        price_history["Date"] = pd.to_datetime(price_history["Date"], errors="coerce")
        price_history["C"] = _to_numeric(price_history["C"])
        price_history = price_history.dropna(subset=["Date"]).sort_values("Date")
        # 出来高が無い日（薄商いで売買が成立しなかった日）は、/equities/bars/daily
        # がO/H/L/C全てnullの行を返すことがある。直近の行を無条件にlatest_closeと
        # すると、たまたま最新の取得日が無取引日だった銘柄で現在値・PER・PBR・
        # 時価総額・配当利回りが軒並み空欄になってしまう（実データで7902において
        # 2023-06-30・2023-07-06が無取引日と確認）。終値がある直近の行まで遡る
        # （2026-08-24にユーザー報告のバグ調査で発見・修正）。
        priced_history = price_history.dropna(subset=["C"])
        if not priced_history.empty:
            latest_close = priced_history["C"].iloc[-1]
            latest_price_date = priced_history["Date"].iloc[-1]
            price_source = "jquants"

    if (latest_close is None or pd.isna(latest_close)) and fallback_price is not None and pd.notna(fallback_price):
        latest_close = float(fallback_price)
        latest_price_date = pd.Timestamp(fallback_price_date) if fallback_price_date is not None else None
        price_source = "yfinance"

    feps = bps = shares_out = treasury_shares = div_ann = annualized_eps = None
    feps_date = bps_date = div_ann_date = annualized_eps_date = None
    roe = operating_margin = None
    if not fins.empty and "DiscDate" in fins.columns:
        fins = fins.copy()
        fins["DiscDate"] = pd.to_datetime(fins["DiscDate"], errors="coerce")
        fins = fins.dropna(subset=["DiscDate"]).sort_values("DiscDate")
        if not fins.empty:
            feps, feps_date = _last_valid_full_year_forecast_eps_with_date(fins)
            bps, bps_date = _last_valid_value_with_date(fins, "BPS")
            shares_out = _last_valid_value(fins, "ShOutFY")
            treasury_shares = _last_valid_value(fins, "TrShFY")
            div_ann, div_ann_date = _dividend_forecast_or_trailing_with_date(fins)
            annualized_eps, annualized_eps_date = _last_valid_eps_annualized_with_date(fins)
            roe = _last_valid_value(fins, "ROE")
            operating_margin = _latest_operating_margin(fins)

            # 開示日以降に株式分割・併合があった場合、1株当たり指標(EPS/BPS/配当)を
            # 現在の株式数基準に換算する（開示日ちょうどの倍率でその後の分割の
            # 有無に関わらず補正されるため、分割が無い場合は倍率1.0で無害）。
            # 既知の限界: price_sourceが"yfinance"（地方単独上場企業の株価
            # フォールバック）の場合、price_historyはJ-Quantsの株価履歴
            # （東証離脱前まで、AdjFactorも東証離脱後の分割・併合を含まない）
            # のため、東証離脱後に分割・併合が行われてもここでは検出できず
            # 倍率1.0のままになる。東証離脱後の分割・併合を検出できるデータ源が
            # 無く是正できないため、呼び出し側(excel_export.py)でyfinance参考値
            # である旨の注記に含めて開示する（2026-08-20のCodexレビューで指摘）。
            if feps is not None:
                feps = feps * _split_adjustment_since(price_history, feps_date)
            if bps is not None:
                bps = bps * _split_adjustment_since(price_history, bps_date)
            if div_ann is not None:
                div_ann = div_ann * _split_adjustment_since(price_history, div_ann_date)
            if annualized_eps is not None:
                annualized_eps = annualized_eps * _split_adjustment_since(price_history, annualized_eps_date)

    market_cap = per = pbr = dividend_yield = None
    calculated_per = source_per = per_difference_rate = None
    if latest_close is not None and pd.notna(latest_close):
        # PERは会社予想EPS(feps)だけを使う。過去実績EPSと最新株価を組み合わせて
        # PERを計算しない（会社予想が未開示・0以下ならPERはNoneのまま = 画面では
        # 「―」表示）。annualized_eps（実績EPSの年率換算）は表示には使わず、
        # calculated_perとの乖離チェック用の参考値(source_per)としてのみ使う。
        if feps and pd.notna(feps) and feps > 0:
            calculated_per = latest_close / feps
        per = calculated_per
        if annualized_eps and pd.notna(annualized_eps) and annualized_eps > 0:
            source_per = latest_close / annualized_eps
        if calculated_per is not None and source_per is not None and source_per != 0:
            per_difference_rate = abs(calculated_per - source_per) / source_per
            if per_difference_rate >= _PER_SANITY_DIVERGENCE_THRESHOLD:
                logger.warning(
                    "PERが予想EPS基準と実績EPS基準で%.0f%%乖離: 予想EPS基準PER=%.1f"
                    "（feps=%s, %s） 実績EPS基準PER=%.1f（annualized_eps=%s, %s）",
                    per_difference_rate * 100, calculated_per, feps, feps_date,
                    source_per, annualized_eps, annualized_eps_date,
                )
        if bps is not None and pd.notna(bps) and bps > 0:
            pbr = latest_close / bps
        if shares_out is not None and pd.notna(shares_out) and price_source != "yfinance":
            float_shares = shares_out - (treasury_shares if pd.notna(treasury_shares) else 0)
            market_cap = latest_close * float_shares
        if div_ann is not None and pd.notna(div_ann) and div_ann >= 0:
            # div_ann == 0 は「無配予定」の明示的な開示であり、未開示(NaN)とは区別する
            dividend_yield = div_ann / latest_close

    return {
        "latest_close": latest_close,
        "latest_price_date": latest_price_date,
        "price_source": price_source,
        "market_cap": market_cap,
        "per": per,
        "pbr": pbr,
        "dividend_yield": dividend_yield,
        "shares_out": shares_out,
        "roe": roe,
        "operating_margin": operating_margin,
        # 画面には出さないデバッグ用の基準日。per/pbr/dividend_yieldはいずれも
        # latest_close（=latest_price_date時点の終値）を分母/分子に使うため、
        # 分子側（EPS・BPS・配当）の開示日がlatest_price_dateとどれだけ
        # 離れているかを見れば、値がどの時点のデータの組み合わせかを追える。
        "metrics_as_of": {
            "latest_price_date": latest_price_date,
            "per_eps_date": feps_date,
            "bps_date": bps_date,
            "dividend_source_date": div_ann_date,
        },
        # PERの検算用デバッグ情報（画面には出さない）。calculated_perが実際に
        # 表示されるperと同じ値。source_perは実績EPS基準の参考値で、perには
        # 使わない。per_difference_rateが大きい場合はログにも警告が出る。
        "per_debug": {
            "forecast_eps": feps,
            "forecast_eps_date": feps_date,
            "calculated_per": calculated_per,
            "source_per": source_per,
            "per_difference_rate": per_difference_rate,
        },
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
    window_start = today_jst() - dt.timedelta(days=1) - dt.timedelta(days=365 * lookback_years)
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
        "EstimatedListingDate", "RecentlyListed", "ROE", "OperatingMargin",
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
            "ROE": metrics["roe"],
            "OperatingMargin": metrics["operating_margin"],
        })

    enrichment = pd.DataFrame(rows)
    return summary.merge(enrichment, on="Code", how="left")


# 「10倍株候補スコア」の各加減点項目の表示ラベル（画面上の並び順にもなる）。
# 四半期成長加速(9)・52週高値接近(10)・出来高急増(11)はJ-Quants全銘柄の
# 長期株価一括取得が新たに必要になるため未実装（README参照）。
TENX_SCORE_LABELS = {
    "market_cap": "小型時価総額",
    "three_year_revenue_growth": "3期連続増収",
    "revenue_cagr": "3年間の売上CAGR",
    "profit_growth_exceeds_sales_growth": "利益成長が売上成長を上回る",
    "margin_improvement": "経常利益率の改善",
    "turnaround": "赤字から黒字への転換",
    "upward_revision": "会社予想の上方修正",
    "progress_ratio": "高進捗率",
    "dilution": "発行済株式数の増加（希薄化）",
    "downward_revision": "下方修正",
}

_YOY_GAP_MIN_DAYS = 330
_YOY_GAP_MAX_DAYS = 400


def _combine_profit(row: pd.Series) -> float | None:
    """経常利益(OdP)。IFRS採用企業等でOdPが無い場合は営業利益(OP)で代用する。"""
    value = row.get("OdP")
    if pd.isna(value):
        value = row.get("OP")
    return value if pd.notna(value) else None


def _latest_yoy_profit_comparison(g: pd.DataFrame) -> dict | None:
    """同一CurPerType・前年同期(330〜400日前)との比較が取れる最新の開示を1件返す。"""
    d = g.dropna(subset=["Sales"]).sort_values(["CurPerType", "CurPerEn"]).copy()
    if d.empty:
        return None
    d["prev_sales"] = d.groupby("CurPerType")["Sales"].shift(1)
    d["prev_period_end"] = d.groupby("CurPerType")["CurPerEn"].shift(1)
    d["prev_op"] = d.groupby("CurPerType")["OP"].shift(1)
    d["prev_odp"] = d.groupby("CurPerType")["OdP"].shift(1)
    gap_days = (d["CurPerEn"] - d["prev_period_end"]).dt.days
    valid = d.loc[gap_days.between(_YOY_GAP_MIN_DAYS, _YOY_GAP_MAX_DAYS) & d["prev_sales"].notna() & (d["prev_sales"] > 0)]
    if valid.empty:
        return None
    latest = valid.sort_values("DiscDate").iloc[-1]
    prev_profit = latest["prev_odp"] if pd.notna(latest["prev_odp"]) else latest["prev_op"]
    curr_profit = _combine_profit(latest)
    sales_growth = (latest["Sales"] - latest["prev_sales"]) / latest["prev_sales"]
    return {
        "sales_growth": sales_growth,
        "prev_profit": prev_profit if pd.notna(prev_profit) else None,
        "curr_profit": curr_profit,
        "sales": latest["Sales"],
        "prev_sales": latest["prev_sales"],
        "disc_date": latest["DiscDate"],
    }


def _score_three_year_revenue_growth_and_cagr(g: pd.DataFrame) -> tuple[float, str, float, float | None]:
    fy = g.loc[(g["CurPerType"] == "FY") & g["Sales"].notna()].drop_duplicates(subset=["CurFYEn"], keep="last")
    fy = fy.sort_values("CurFYEn")
    sales_series = fy[["CurFYEn", "Sales"]].tail(4)
    values = sales_series["Sales"].tolist()
    padded = ([None] * max(0, 4 - len(values)) + values)[-4:]
    growth_points, growth_label = score.score_three_year_revenue_growth(padded)

    cagr_points, cagr = 0.0, None
    if len(sales_series) >= 4:
        first_row = sales_series.iloc[-4]
        last_row = sales_series.iloc[-1]
        gap_days = (last_row["CurFYEn"] - first_row["CurFYEn"]).days
        if abs(gap_days - 3 * 365) <= 120:
            cagr_points, cagr = score.score_revenue_cagr(first_row["Sales"], last_row["Sales"])
    return growth_points, growth_label, cagr_points, cagr


def _score_upward_revision_for_code(g: pd.DataFrame) -> tuple[float, float | None, int, pd.Timestamp | None]:
    d = g.dropna(subset=["FSales"]).sort_values(["CurFYEn", "DiscDate"]).copy()
    if d.empty:
        return 0.0, None, 0, None
    d["prev_fsales"] = d.groupby("CurFYEn")["FSales"].shift(1)
    d["prev_fodp"] = d.groupby("CurFYEn")["FOdP"].shift(1)
    d["prev_fop"] = d.groupby("CurFYEn")["FOP"].shift(1)
    valid = d.dropna(subset=["prev_fsales"])
    valid = valid.loc[valid["prev_fsales"] > 0]
    if valid.empty:
        return 0.0, None, 0, None

    latest = valid.sort_values("DiscDate").iloc[-1]
    sales_revision_pct = (latest["FSales"] - latest["prev_fsales"]) / latest["prev_fsales"]
    fodp_now = latest["FOdP"] if pd.notna(latest["FOdP"]) else latest["FOP"]
    fodp_prev = latest["prev_fodp"] if pd.notna(latest["prev_fodp"]) else latest["prev_fop"]
    profit_revision_pct = None
    if pd.notna(fodp_now) and pd.notna(fodp_prev) and fodp_prev > 0:
        profit_revision_pct = (fodp_now - fodp_prev) / fodp_prev

    same_fy = valid.loc[valid["CurFYEn"] == latest["CurFYEn"]].sort_values("DiscDate")
    is_upward = (same_fy["FSales"] > same_fy["prev_fsales"]).tolist()
    streak = 0
    for up in reversed(is_upward):
        if up:
            streak += 1
        else:
            break

    points = score.score_upward_revision(sales_revision_pct, profit_revision_pct, streak)
    return points, sales_revision_pct, streak, latest["DiscDate"]


def _score_progress_ratio_for_code(g: pd.DataFrame) -> tuple[float, str, float | None]:
    qtr = g.loc[g["CurPerType"].isin(["1Q", "2Q", "3Q"]) & g["OdP"].notna() & g["FOdP"].notna() & (g["FOdP"] != 0)]
    if qtr.empty:
        return 0.0, "判定不能", None
    latest = qtr.sort_values("DiscDate").iloc[-1]
    progress_now = latest["OdP"] / latest["FOdP"] * 100

    prev_year_progress = None
    same_period = qtr.loc[qtr["CurPerType"] == latest["CurPerType"]].sort_values("CurPerEn")
    gap_days = (latest["CurPerEn"] - same_period["CurPerEn"]).dt.days
    candidates = same_period.loc[gap_days.between(_YOY_GAP_MIN_DAYS, _YOY_GAP_MAX_DAYS)]
    if not candidates.empty:
        prev_row = candidates.sort_values("CurPerEn").iloc[-1]
        if prev_row["FOdP"] != 0:
            prev_year_progress = prev_row["OdP"] / prev_row["FOdP"] * 100

    points, label = score.score_progress_ratio(latest["CurPerType"], progress_now, prev_year_progress)
    return points, label, progress_now


def _score_dilution_for_code(g: pd.DataFrame) -> tuple[float, str]:
    d = g.dropna(subset=["ShOutFY"]).sort_values(["CurPerType", "CurPerEn"]).copy()
    if d.empty:
        return 0.0, "未判定"
    d["prev_sh"] = d.groupby("CurPerType")["ShOutFY"].shift(1)
    d["prev_period_end"] = d.groupby("CurPerType")["CurPerEn"].shift(1)
    d["prev_bps"] = d.groupby("CurPerType")["BPS"].shift(1)
    gap_days = (d["CurPerEn"] - d["prev_period_end"]).dt.days
    valid = d.loc[gap_days.between(_YOY_GAP_MIN_DAYS, _YOY_GAP_MAX_DAYS) & d["prev_sh"].notna() & (d["prev_sh"] > 0)]
    if valid.empty:
        return 0.0, "未判定"

    latest = valid.sort_values("DiscDate").iloc[-1]
    shares_growth = (latest["ShOutFY"] - latest["prev_sh"]) / latest["prev_sh"]
    bps_growth = None
    if pd.notna(latest["BPS"]) and pd.notna(latest["prev_bps"]) and latest["prev_bps"] > 0:
        bps_growth = (latest["BPS"] - latest["prev_bps"]) / latest["prev_bps"]
    looks_like_split = score.looks_like_stock_split(shares_growth, bps_growth)
    return score.score_dilution(shares_growth, looks_like_split)


def _score_one_code(code: str, g: pd.DataFrame, market_cap: float | None, has_downward: bool, downward_penalize_only: bool) -> dict:
    market_cap_oku = market_cap / 1e8 if market_cap is not None and pd.notna(market_cap) else None
    cap_points, cap_label = score.score_market_cap(market_cap_oku)

    growth_points, growth_label, cagr_points, cagr = _score_three_year_revenue_growth_and_cagr(g)

    yoy = _latest_yoy_profit_comparison(g)
    profit_growth_pct = None
    profit_vs_sales_points, profit_vs_sales_label = 0.0, "判定不能"
    margin_points, margin_diff = 0.0, None
    turnaround_points, turned = 0.0, False
    if yoy is not None:
        if yoy["prev_profit"] is not None and yoy["prev_profit"] > 0 and yoy["curr_profit"] is not None:
            profit_growth_pct = (yoy["curr_profit"] - yoy["prev_profit"]) / yoy["prev_profit"]
        profit_vs_sales_points, profit_vs_sales_label = score.score_profit_growth_exceeds_sales_growth(
            yoy["sales_growth"], profit_growth_pct, yoy["prev_profit"], yoy["curr_profit"]
        )
        if yoy["curr_profit"] is not None and yoy["sales"]:
            margin_now = yoy["curr_profit"] / yoy["sales"] * 100
            margin_prev = (
                yoy["prev_profit"] / yoy["prev_sales"] * 100
                if yoy["prev_profit"] is not None and yoy["prev_sales"]
                else None
            )
            if margin_prev is not None:
                margin_diff = margin_now - margin_prev
        margin_points = score.score_margin_improvement(margin_diff)

        recent_profits = g.assign(_profit=g.apply(_combine_profit, axis=1)).dropna(subset=["_profit"]).sort_values("DiscDate")
        sustained = False
        if len(recent_profits) >= 3:
            sustained = bool((recent_profits["_profit"].tail(3) > 0).all())
        turnaround_points, turned = score.score_turnaround(yoy["prev_profit"], yoy["curr_profit"], sustained)

    revision_points, sales_revision_pct, revision_streak, _ = _score_upward_revision_for_code(g)
    progress_points, progress_label, progress_pct = _score_progress_ratio_for_code(g)
    dilution_points, dilution_label = _score_dilution_for_code(g)
    downward_points = score.score_downward_revision(has_downward, downward_penalize_only)

    points_map = {
        "market_cap": cap_points,
        "three_year_revenue_growth": growth_points,
        "revenue_cagr": cagr_points,
        "profit_growth_exceeds_sales_growth": profit_vs_sales_points,
        "margin_improvement": margin_points,
        "turnaround": turnaround_points,
        "upward_revision": revision_points,
        "progress_ratio": progress_points,
        "dilution": dilution_points,
        "downward_revision": downward_points,
    }
    total = sum(points_map.values())
    positive_reasons = "、".join(TENX_SCORE_LABELS[k] for k, v in points_map.items() if v > 0)
    negative_reasons = "、".join(TENX_SCORE_LABELS[k] for k, v in points_map.items() if v < 0)
    undetermined = "、".join(
        label for label, cond in [
            (TENX_SCORE_LABELS["market_cap"], cap_label == "判定不能"),
            (TENX_SCORE_LABELS["three_year_revenue_growth"], growth_label == "判定不能"),
            (TENX_SCORE_LABELS["progress_ratio"], progress_label == "判定不能"),
            (TENX_SCORE_LABELS["dilution"], dilution_label == "未判定"),
        ] if cond
    )

    return {
        "Code": code,
        "TenXScore": total,
        "MarketCapBand": cap_label,
        "ThreeYearRevenueGrowth": growth_label,
        "RevenueCAGR": cagr,
        "ProfitGrowthRate": profit_growth_pct,
        "MarginImprovement": margin_diff,
        "Turnaround": turned,
        "UpwardRevisionPct": sales_revision_pct,
        "UpwardRevisionCount": revision_streak,
        "ProgressRatio": progress_pct,
        "PositiveReasons": positive_reasons,
        "NegativeReasons": negative_reasons,
        "UndeterminedItems": undetermined,
    }


def compute_tenx_scores(
    client: JQuantsClient,
    start: dt.date,
    end: dt.date,
    summary: pd.DataFrame,
    downward_penalize_only: bool = True,
) -> pd.DataFrame:
    """summaryの各銘柄について「10倍株候補スコア」を計算する。

    財務データはrun_screeningが比較用に遡って取得している範囲と同じ条件で
    再取得するが、日付単位でローカルキャッシュされているため追加のAPI呼び出しは
    発生しない（キャッシュファイルを再度読み込むだけ）。
    """
    empty_cols = [
        "Code", "TenXScore", "MarketCapBand", "ThreeYearRevenueGrowth", "RevenueCAGR",
        "ProfitGrowthRate", "MarginImprovement", "Turnaround", "UpwardRevisionPct",
        "UpwardRevisionCount", "ProgressRatio", "PositiveReasons", "NegativeReasons",
        "UndeterminedItems",
    ]
    if summary.empty:
        return pd.DataFrame(columns=empty_cols)

    comparison_lookback_days = 365 * PROFIT_DOUBLING_YEARS + 60
    statements_fetch_start = start - dt.timedelta(days=comparison_lookback_days)
    # run_screeningと同じ理由でLightプランの取得可能期間（過去5年）の下限で
    # クランプする（startが既に1〜2年以上前だと、遡り取得後の開始日が
    # 5年の境界を超えてJ-Quantsに400エラーで拒否されるため）。境界は
    # 日数での近似ではなく実際の「5年前」の暦日で計算する（run_screening参照）。
    earliest_available_statements_date = _years_before(today_jst(), LISTING_LOOKBACK_YEARS)
    statements_fetch_start = max(statements_fetch_start, earliest_available_statements_date)
    statements_df = endpoints.get_statements_range(client, statements_fetch_start, end)

    codes = summary["Code"].astype(str).tolist()
    if statements_df.empty:
        f = pd.DataFrame(columns=["Code"])
    else:
        f = statements_df.copy()
        f["Code"] = f["Code"].astype(str)
        f = f.loc[f["Code"].isin(codes)].copy()
        f["DiscDate"] = pd.to_datetime(f["DiscDate"], errors="coerce")
        f["CurFYEn"] = pd.to_datetime(f["CurFYEn"], errors="coerce")
        f["CurPerEn"] = pd.to_datetime(f.get("CurPerEn"), errors="coerce")
        for col in ["Sales", "OP", "OdP", "FSales", "FOP", "FOdP", "ShOutFY", "BPS"]:
            f[col] = pd.to_numeric(f[col], errors="coerce") if col in f.columns else float("nan")
        f = f.dropna(subset=["Code", "DiscDate", "CurPerEn"])

    market_cap_map = dict(zip(summary["Code"].astype(str), summary.get("MarketCap", pd.Series(dtype="float64"))))
    if "HasDownwardRevision" in summary.columns:
        downward_map = dict(zip(summary["Code"].astype(str), summary["HasDownwardRevision"]))
    else:
        downward_map = {}

    rows = []
    for code in codes:
        g = f.loc[f["Code"] == code] if not f.empty else f
        if g.empty:
            rows.append({
                "Code": code, "TenXScore": 0.0, "MarketCapBand": score.score_market_cap(
                    (market_cap_map.get(code) or float("nan")) / 1e8
                )[1],
                "ThreeYearRevenueGrowth": "判定不能", "RevenueCAGR": None, "ProfitGrowthRate": None,
                "MarginImprovement": None, "Turnaround": False, "UpwardRevisionPct": None,
                "UpwardRevisionCount": 0, "ProgressRatio": None, "PositiveReasons": "",
                "NegativeReasons": "", "UndeterminedItems": "決算データ",
            })
            continue
        rows.append(_score_one_code(code, g, market_cap_map.get(code), bool(downward_map.get(code, False)), downward_penalize_only))

    return pd.DataFrame(rows)
