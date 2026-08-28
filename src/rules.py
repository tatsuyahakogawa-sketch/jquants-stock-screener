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
import unicodedata

import pandas as pd

from src.config import (
    SALES_GROWTH_MAJOR_THRESHOLD,
    SALES_GROWTH_EXPLOSIVE_THRESHOLD,
    SALES_GROWTH_DOUBLING_THRESHOLD,
    EARNINGS_BEAT_THRESHOLD,
    EQUITY_RATIO_THRESHOLD,
    PROFIT_DOUBLING_YEARS,
    PROFIT_DOUBLING_MULTIPLE,
    PROFIT_GROWTH_MAJOR_THRESHOLD,
    DOWNWARD_REVISION_THRESHOLD,
    PBR_LOW_THRESHOLD,
)

# --- 列名定数（要: 実際のAPIレスポンスと突き合わせて確認） -----------------

QUOTES_CODE = "Code"
QUOTES_DATE = "Date"
QUOTES_CLOSE = "C"
QUOTES_UPPER_LIMIT_FLAG = "UL"  # "1"=ストップ高で終値確定, "0"=それ以外

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
STMT_BPS = "BPS"  # 1株あたり純資産（PBR計算用）

# TDnet開示タイトルのキーワード（誤検出のリスクがあるため、詳細列に開示タイトルを
# そのまま出し、ユーザー自身がリンク先で確認できるようにしている）
FACILITY_KEYWORDS = ["新工場", "工場新設", "新設工場", "生産拠点の新設", "第二工場", "第2工場"]
STORE_KEYWORDS = ["新店舗", "新規出店", "店舗新設", "新規開設"]
# 「新工場」「第二工場」等は閉鎖・計画中止のニュースにも一致してしまうため、
# これらの語を含むタイトルは逆方向（ネガティブ）の話として除外する。
FACILITY_STORE_EXCLUSION_KEYWORDS = ["閉鎖", "中止", "撤退", "廃止", "休止", "縮小"]
STOCK_SPLIT_KEYWORD = "株式分割"
STOCK_CONSOLIDATION_KEYWORD = "株式併合"
STOCK_SPLIT_KEYWORDS = [STOCK_SPLIT_KEYWORD, STOCK_CONSOLIDATION_KEYWORD]
# キーワード文字列とrule名の対応（分割と併合は逆方向の株価インパクトを持つ
# 別の事象のため、2026-08-24にユーザーの指定で別ルールに分離した）。
_STOCK_SPLIT_KEYWORD_TO_RULE = {
    STOCK_SPLIT_KEYWORD: "stock_split",
    STOCK_CONSOLIDATION_KEYWORD: "stock_consolidation",
}
# 「株式分割に伴う配当予想の修正」のように、分割・併合の決定そのものではなく
# その後始末（配当予想修正・株主優待変更・新株予約権調整等）だけを知らせる
# 開示は新規発表として扱わない。タイトル中のSTOCK_SPLIT_KEYWORDSが、常に
# これらの接続表現に支配された形でしか出現しない場合、新規発表ではないと判断する
# （逆に「株式分割及び定款の一部変更に関するお知らせ」のように独立して出現する
# 場合は、後ろに定款変更等が続いていても新規発表の一部として扱う）。
STOCK_SPLIT_FOLLOWUP_CONNECTORS = ["に伴う", "に伴い", "を受けた", "を受けて", "後の", "後に"]
# 接続表現が無くても、これらの語だけを主題にした開示は分割・併合の後日談・
# 事務的な知らせであることが多いため、独立した出現が無い場合の除外判定に使う。
STOCK_SPLIT_FOLLOWUP_TOPIC_KEYWORDS = [
    "配当予想の修正", "配当予想修正", "株主優待", "新株予約権",
    "発行済株式数", "実施日", "基準日", "効力発生", "自己株式",
]
LARGE_ORDER_KEYWORDS = ["大型受注", "大口受注", "大型案件受注"]
# 「開示基準変更」は受注そのものの発表ではなく開示ルールの変更のお知らせのため除外する。
LARGE_ORDER_EXCLUSION_KEYWORDS = ["開示基準", "取消", "中止", "解除"]
WORLD_FIRST_KEYWORDS = ["世界初"]
REGIONAL_EXCHANGES = ["札幌証券取引所", "福岡証券取引所", "名古屋証券取引所"]
TOKYO_EXCHANGE_KEYWORDS = ["東京証券取引所", "東証"]
# 「JPX日経インデックス400」構成銘柄への選定・採用の発表を検出するキーワード。
# 実際のTDnet開示タイトルは「JPX」「日経インデックス」「400」の間の全角/半角・
# スペースの有無にばらつきがある（例:「ＪＰＸ日経インデックス４００」「JPX 日経
# インデックス 400」）ため、unicodedata.normalize("NFKC", title)で正規化した上で
# 「日経インデックス」「400」「構成銘柄」の3語がすべて含まれるかで判定する
# （2026-08-24に実データ26件で確認・ユーザーの指定で追加。「JPX日経中小型株指数」
# 「NEXT FUNDS...上場投信」等の別指数・別銘柄の決算短信は"400"または"構成銘柄"を
# 含まないため誤検出しない）。
JPX_NIKKEI_400_KEYWORDS = ["日経インデックス", "400", "構成銘柄"]
# 「構成銘柄からの除外に関するお知らせ」のように、選定・採用ではなく逆方向
# （除外）の発表にも上記3語が一致してしまうため、除外系のタイトルは除く
# （2026-08-24のCodexレビューで指摘。2021〜2026年の実データでは除外系の
# 自社開示は確認できなかったが、将来的な発生に備えて防御的に除外する）。
JPX_NIKKEI_400_EXCLUSION_KEYWORDS = ["除外", "非選定", "解除"]


def _to_numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _is_primary_mask(df: pd.DataFrame) -> pd.Series:
    """statements_dfの各行が「実際の開示」かどうかを示す真偽Seriesを返す
    （IsPrimary列が無ければ全行True。J-Quants由来データとの後方互換）。

    地方株のstatements_df(src/tdnet_xbrl.py由来)は、実際の開示行(IsPrimary=True)
    に加え、開示に埋め込まれた前年同期実績・翌期予想の合成行(IsPrimary=False)を
    含む。detect_sales_growth等の「前年同期と比較して増加した開示」を検出する
    関数群は、比較の基準値(prev_*)としては合成行を使ってよいが、検出結果
    (ヒット)自体は実際に行われた開示に限定する必要がある。合成行がヒットに
    含まれると、実際には別の（後の）開示で埋め込まれた過去の比較値が、あたかも
    その開示日に新しく判明した情報であるかのように表示されてしまい、特に
    直近の実際の開示が逆方向（減収・下方修正等）だった場合に誤解を招く
    （2026-08-19の5巡目のCodexレビューで指摘: detect_two_quarter_growthは
    既にIsPrimaryで絞り込んでいたが、detect_sales_growth等の他の関数には
    同じ絞り込みが漏れていた）。

    astype(bool)は、空のDataFrameとのconcatでIsPrimary列がobject dtypeに
    なりうるための明示的な型変換（src/regional_stocks.py参照）。
    """
    if "IsPrimary" not in df.columns:
        return pd.Series(True, index=df.index)
    return df["IsPrimary"].fillna(True).astype(bool)


def detect_stop_high(quotes_df: pd.DataFrame) -> pd.DataFrame:
    """ストップ高で終値が確定した(Code, Date)の一覧を返す。"""
    if quotes_df.empty or QUOTES_UPPER_LIMIT_FLAG not in quotes_df.columns:
        return pd.DataFrame(columns=[QUOTES_CODE, QUOTES_DATE, "rule", "detail"])
    flag = _to_numeric(quotes_df[QUOTES_UPPER_LIMIT_FLAG]).fillna(0)
    hit = quotes_df.loc[flag == 1, [QUOTES_CODE, QUOTES_DATE, QUOTES_CLOSE]].copy()
    hit["rule"] = "stop_high"
    hit["detail"] = "ストップ高で終値が確定 (Close=" + hit[QUOTES_CLOSE].astype(str) + ")"
    return hit.drop(columns=[QUOTES_CLOSE]).rename(columns={QUOTES_CODE: "Code", QUOTES_DATE: "Date"})


def _classify_stock_split_title(title: str) -> tuple[bool, str, str | None]:
    """タイトルが株式分割・併合の「新規決定・発表」を主題にしているか判定する。

    「株式分割及び定款の一部変更に関するお知らせ」のように、STOCK_SPLIT_KEYWORDS
    がSTOCK_SPLIT_FOLLOWUP_CONNECTORSに支配されず独立して出現する場合は、新規
    発表（後ろに定款変更・配当予想修正等が続いていてもまとめて発表された扱い）
    と判断する。「株式分割に伴う配当予想の修正に関するお知らせ」のように、常に
    接続表現の直後にしか出現しない場合は後日談の開示とみなし、除外する。
    戻り値: (新規発表とみなすか, 判定理由, 一致したキーワード「株式分割」/
    「株式併合」(無ければNone)）。分割と併合は逆方向の株価インパクトを持つ
    別の事象のため、呼び出し側でこのキーワードを見てrule("stock_split"/
    "stock_consolidation")を分ける（2026-08-24にユーザーの指定で分離した）。
    """
    matched_keyword = next((k for k in STOCK_SPLIT_KEYWORDS if k in title), None)
    if matched_keyword is None:
        return False, "", None

    for keyword in STOCK_SPLIT_KEYWORDS:
        pos = 0
        while (idx := title.find(keyword, pos)) != -1:
            after = title[idx + len(keyword):]
            if not any(after.startswith(c) for c in STOCK_SPLIT_FOLLOWUP_CONNECTORS):
                return True, f"「{keyword}」が新規決定の主題として単独で出現", keyword
            pos = idx + len(keyword)

    followup_topic = next((t for t in STOCK_SPLIT_FOLLOWUP_TOPIC_KEYWORDS if t in title), None)
    if followup_topic is not None:
        return (
            False,
            f"「{matched_keyword}に伴う」等の形でのみ出現し、「{followup_topic}」のみを主題とする後日談の開示と判断",
            matched_keyword,
        )
    return (
        False,
        f"「{matched_keyword}に伴う」等の形でのみ出現しており新規決定の発表ではないと判断",
        matched_keyword,
    )


def detect_stock_split(disclosures_df: pd.DataFrame) -> pd.DataFrame:
    """TDnet開示タイトルから株式分割・株式併合それぞれの「新規の決定・発表」を
    検出する。

    AdjFactor(株価調整係数)の変化を見る方法は、分割・併合が実際に効力を持つ日
    （株価調整に反映される日）しか検知できず、発表からかなり遅れる（発表時点
    では株価がまだ反応していないため、好材料/悪材料として先取りするには
    使えない）。株式分割・併合は発表時点で反応することが多いため、価格が
    変動する前に検知できるよう、TDnetの開示タイトルから発表日ベースで検出する。

    株式分割（1株を複数株に分ける。既存株主にとって好材料と受け取られる
    ことが多い）と株式併合（複数株を1株にまとめる。既存株主にとって悪材料と
    受け取られることが多い）は株価インパクトが逆方向の別の事象のため、
    "stock_split"（株式分割）と"stock_consolidation"（株式併合）の別ruleとして
    返す（2026-08-24にユーザーの指定で分離。以前は"stock_split"1本にまとめて
    いた）。

    タイトルに「株式分割」「株式併合」を含むだけでは判定しない。配当予想の修正・
    株主優待制度の変更・新株予約権の調整・発行済株式数の変更等、分割・併合の
    決定そのものではなく後日談・事務的な知らせだけの開示（例:「株式分割に伴う
    配当予想の修正に関するお知らせ」）は誤検出として除外する
    （_classify_stock_split_title参照。誤検出の可能性が完全に無くなる保証は
    ないため、内容は必ずリンク先で確認する）。
    """
    required = {"company_code", "title", "pubdate"}
    if disclosures_df.empty or not required.issubset(disclosures_df.columns):
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    df = disclosures_df.copy()
    titles = df["title"].fillna("")
    classified = titles.apply(_classify_stock_split_title)
    is_new = classified.apply(lambda c: c[0])
    hit = df.loc[is_new, ["company_code", "pubdate", "title"]].copy()
    if hit.empty:
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    matched_keyword = classified.loc[is_new].apply(lambda c: c[2])
    hit["rule"] = matched_keyword.map(_STOCK_SPLIT_KEYWORD_TO_RULE)
    hit["detail"] = "開示タイトル: " + hit["title"]
    hit["Date"] = pd.to_datetime(hit["pubdate"], errors="coerce")
    hit["event_type"] = hit["rule"]
    hit["event_date"] = hit["Date"]
    hit["source_title"] = hit["title"]
    hit["source_url"] = df.loc[is_new, "document_url"] if "document_url" in df.columns else None
    hit["match_reason"] = classified.loc[is_new].apply(lambda c: c[1])
    return hit[
        ["company_code", "Date", "rule", "detail", "event_type", "event_date", "source_title", "source_url", "match_reason"]
    ].rename(columns={"company_code": "Code"})


def detect_large_order(disclosures_df: pd.DataFrame) -> pd.DataFrame:
    """TDnet開示タイトルから「大型受注」「大口受注」等の発表を検出する。

    タイトルだけを見たキーワード一致であり、誤検出（例:開示基準の変更のお知らせ
    が「大口受注」を含む等）の可能性がある。detail列に開示タイトルそのものを
    入れているので、実際に使う際はリンク先で内容を確認すること。
    """
    required = {"company_code", "title", "pubdate"}
    if disclosures_df.empty or not required.issubset(disclosures_df.columns):
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    df = disclosures_df.copy()
    titles = df["title"].fillna("")
    mentions_keyword = titles.apply(lambda t: any(k in t for k in LARGE_ORDER_KEYWORDS))
    mentions_exclusion = titles.apply(lambda t: any(k in t for k in LARGE_ORDER_EXCLUSION_KEYWORDS))
    hit = df.loc[mentions_keyword & ~mentions_exclusion, ["company_code", "pubdate", "title"]].copy()
    if hit.empty:
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    hit["rule"] = "large_order"
    hit["detail"] = "開示タイトル: " + hit["title"]
    hit["Date"] = pd.to_datetime(hit["pubdate"], errors="coerce")
    return hit[["company_code", "Date", "rule", "detail"]].rename(columns={"company_code": "Code"})


def detect_world_first(disclosures_df: pd.DataFrame) -> pd.DataFrame:
    """TDnet開示タイトルから「世界初」を含む発表を検出する。

    タイトルだけを見たキーワード一致であり、誤検出の可能性がある。detail列に
    開示タイトルそのものを入れているので、実際に使う際はリンク先で内容を
    確認すること。
    """
    required = {"company_code", "title", "pubdate"}
    if disclosures_df.empty or not required.issubset(disclosures_df.columns):
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    df = disclosures_df.copy()
    titles = df["title"].fillna("")
    mask = titles.apply(lambda t: any(k in t for k in WORLD_FIRST_KEYWORDS))
    hit = df.loc[mask, ["company_code", "pubdate", "title"]].copy()
    if hit.empty:
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    hit["rule"] = "world_first"
    hit["detail"] = "開示タイトル: " + hit["title"]
    hit["Date"] = pd.to_datetime(hit["pubdate"], errors="coerce")
    return hit[["company_code", "Date", "rule", "detail"]].rename(columns={"company_code": "Code"})


def detect_sales_growth(statements_df: pd.DataFrame) -> pd.DataFrame:
    """同一の決算期タイプ(TypeOfCurrentPeriod)の前年同期と比べ、売上高が
    大幅(+20%以上)または爆発的(+50%以上)に増えた開示の一覧。

    +50%以上の増収は「大幅(+20%以上)」の条件も数値としては満たしているため、
    両方のruleタグ(sales_growth_major, sales_growth_explosive)を持つ行として
    それぞれ返す（1行に付き1タグではなく、該当する分だけ複数行）。以前は
    growth_rateに応じて1つのタグだけを選んでいたため、"sales_growth_major"
    （ラベル表記は「+20%以上」）だけを選択したユーザーの絞り込みから、実際には
    +20%以上でもある60%成長の銘柄が漏れていた（2026-08-19のCodexレビューで
    指摘、実データで確認）。

    「1年で2倍(+100%以上)」(sales_growth_doubling)はこの関数では扱わない。
    この関数は「選択期間内に閾値を超える開示があったか」というイベントとして
    扱うが、sales_growth_doublingは「銘柄が現在その状態にあるか」という状態
    として扱う必要があり、両者は選ぶべき開示行が異なる（例: 売上が
    100→250→260と推移した場合、260時点の前年同期比はわずか+4%なのに、この
    関数の単純な閾値フィルタだと250の開示(+150%)がいつまでもヒットし続けて
    しまう）。sales_growth_doublingはdetect_current_sales_doublingで別途
    判定する（2026-08-25のCodexレビューで指摘・修正）。
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
    # ヒット自体は実際の開示行に限定する（_is_primary_mask docstring参照）。
    # prev_net_sales等の比較基準値の計算自体は上のgroupby/shiftで既に全行
    # （合成行含む）を使っているため、ここで絞ってもその計算には影響しない。
    valid &= _is_primary_mask(df)

    growth = (df[STMT_NET_SALES] - df["prev_net_sales"]) / df["prev_net_sales"]
    df["growth_rate"] = growth
    df["detail"] = "前年同期比 売上高 " + (df["growth_rate"] * 100).round(1).astype(str) + "% 増"

    hit_major = df.loc[valid & (growth >= SALES_GROWTH_MAJOR_THRESHOLD)].copy()
    hit_major["rule"] = "sales_growth_major"
    hit_explosive = df.loc[valid & (growth >= SALES_GROWTH_EXPLOSIVE_THRESHOLD)].copy()
    hit_explosive["rule"] = "sales_growth_explosive"

    hit = pd.concat([hit_major, hit_explosive], ignore_index=True)
    result = hit[[STMT_CODE, STMT_DISCLOSED_DATE, "rule", "detail"]].rename(
        columns={STMT_CODE: "Code", STMT_DISCLOSED_DATE: "Date"}
    )
    return result


def detect_current_sales_doubling(statements_df: pd.DataFrame) -> pd.DataFrame:
    """銘柄ごとに、実際に開示された(IsPrimary)直近の決算期の開示1件だけを
    見て、前年同期比で売上高が2倍(+100%)以上かどうかを判定する
    （「1年で売上高2倍」を、特定の開示日が選択期間に入っているかという
    イベントとしてではなく、銘柄が"現在"その状態にあるかという状態として
    扱うための専用関数。detect_sales_growthのように閾値を満たす行だけを
    集めて「最新のもの」を選ぶと、閾値を満たさなくなった今の状態ではなく
    過去に一度でも閾値を満たした古い開示がいつまでも残り続けてしまう
    （例: 売上が100→250→260と推移した場合、260の開示時点の前年同期比は
    わずか+4%なのに、250の開示（+150%）がヒットとして残り続ける）。
    そのため、まず銘柄ごとに直近の決算期の開示1件を選んでから、その1件が
    閾値を満たすかどうかを判定する順序にする。2026-08-25のCodexレビューで
    指摘・修正）。
    """
    required = {STMT_CODE, STMT_PERIOD_TYPE, STMT_PERIOD_END, STMT_NET_SALES, STMT_DISCLOSED_DATE}
    if statements_df.empty or not required.issubset(statements_df.columns):
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    df = statements_df.copy()
    # Sales(売上高)がNaNの行もここではまだ除外しない。もし銘柄の直近の
    # 決算期の開示がたまたまSalesを欠いている場合、ここで先に除外すると
    # 後段の「直近の決算期を選ぶ」処理が1つ古い決算期を「最新」として
    # 誤選択してしまい、古いが数値の揃った開示を"現在"の状態であるかの
    # ように返してしまう。Sales欠損の有無に関わらずまず直近の決算期を
    # 選び、その1件についてだけ判定不能（≒ヒットなし）として扱う
    # （2026-08-25の4巡目のCodexレビューで指摘・修正）。
    df[STMT_NET_SALES] = _to_numeric(df[STMT_NET_SALES])
    df[STMT_PERIOD_END] = pd.to_datetime(df[STMT_PERIOD_END], errors="coerce")
    df[STMT_DISCLOSED_DATE] = pd.to_datetime(df[STMT_DISCLOSED_DATE], errors="coerce")
    df = df.dropna(subset=[STMT_PERIOD_END, STMT_DISCLOSED_DATE])

    # 同一銘柄・同一決算期(CurPerType/CurPerEn)について複数の行が存在する
    # ことがある: (a) 実際の開示(IsPrimary=True)の訂正報告、(b) 他の開示に
    # 埋め込まれた合成行(IsPrimary=False、前年同期実績)がその後の開示で
    # 遡及的に修正された値を反映している場合。後段のshift(1)は「1決算期=
    # 1行」という前提で前年同期の行を求めるため、重複が残っていると
    # どちらと比較されるか不定になる。実際の開示か合成行かに関わらず、
    # 開示日(DiscDate)が最も新しい1件を優先する（合成行のDiscDateは、
    # それが埋め込まれていた"親"の開示の開示日を表すため、より新しい
    # 開示に埋め込まれた合成行の方が、古い実際の開示自体よりも新しい
    # 情報を反映していることがある。単純に「実際の開示なら常に優先」と
    # すると、逆にこのケースを取りこぼしてしまう。2026-08-25の6巡目の
    # Codexレビューで一度「実際の開示を優先」で修正したが、7巡目のレビューで
    # このケースを指摘され、開示日ベースの比較に修正した）。
    # 同一日に複数回開示された場合（同日中の訂正等）、DiscDateだけでは
    # タイになる。pandasのsort_values既定のquicksortはタイの並び順を
    # 保証しないため、kind="stable"で明示的に安定ソートし、取得元
    # (get_all_pages)が返した順序（実務上はより後に処理された開示ほど
    # 後ろに来ると期待できる）をタイブレークとして使う。J-Quantsの
    # /fins/summaryにはDiscDateより細かい時刻・通番の列が無いため、
    # これが現状得られる最善のタイブレークになる（2026-08-27の3巡目の
    # Codexレビューで指摘・修正）。
    key_cols = [STMT_CODE, STMT_PERIOD_TYPE, STMT_PERIOD_END]
    df = df.sort_values(STMT_DISCLOSED_DATE, kind="stable")
    df = df.drop_duplicates(subset=key_cols, keep="last")
    df = df.sort_values([STMT_CODE, STMT_PERIOD_TYPE, STMT_PERIOD_END])

    df["prev_net_sales"] = df.groupby([STMT_CODE, STMT_PERIOD_TYPE])[STMT_NET_SALES].shift(1)
    df["prev_period_end"] = df.groupby([STMT_CODE, STMT_PERIOD_TYPE])[STMT_PERIOD_END].shift(1)
    gap_days = (df[STMT_PERIOD_END] - df["prev_period_end"]).dt.days
    has_comparable_prev = df["prev_net_sales"].notna() & (df["prev_net_sales"] > 0) & gap_days.between(330, 400)
    growth = (df[STMT_NET_SALES] - df["prev_net_sales"]) / df["prev_net_sales"]

    is_primary = _is_primary_mask(df)
    if not is_primary.any():
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    # 銘柄ごとに、実際の開示(IsPrimary)のうち対象の決算期末日(CurPerEn)が
    # 最も新しい1件だけを選ぶ（決算期タイプ(1Q/2Q/3Q/FY)は問わない）。
    # 開示日(DiscDate)ではなく決算期末日で選ぶのは、より新しい決算期が
    # 既に開示された後に、それより古い決算期の訂正開示が出た場合、
    # 開示日基準だと訂正開示の方が新しいという理由だけで古い決算期の行が
    # 「最新」として選ばれてしまい、実際に知りたい直近の決算期の成長率とは
    # 無関係な、古い決算期同士の比較結果を返してしまうため（例: 2025年
    # 1Qの数値が2026年1Q開示後に訂正された場合、訂正後の2025年1Q行が
    # 開示日基準では最新になってしまうが、比較すべきは2026年1Qの成長率。
    # 2026-08-25の3巡目のCodexレビューで指摘・修正）。
    latest_idx = (
        df.loc[is_primary].sort_values(STMT_PERIOD_END).groupby(STMT_CODE).tail(1).index
    )
    latest = df.loc[latest_idx].copy()
    latest["growth_rate"] = growth.loc[latest_idx]
    latest["has_comparable_prev"] = has_comparable_prev.loc[latest_idx]

    hit = latest.loc[latest["has_comparable_prev"] & (latest["growth_rate"] >= SALES_GROWTH_DOUBLING_THRESHOLD)].copy()
    if hit.empty:
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    hit["rule"] = "sales_growth_doubling"
    hit["detail"] = "前年同期比 売上高 " + (hit["growth_rate"] * 100).round(1).astype(str) + "% 増（直近の開示時点）"
    return hit[[STMT_CODE, STMT_DISCLOSED_DATE, "rule", "detail"]].rename(
        columns={STMT_CODE: "Code", STMT_DISCLOSED_DATE: "Date"}
    )


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
    # ヒット自体は実際の開示行に限定する（_is_primary_mask docstring参照）。
    is_primary = _is_primary_mask(df)

    hit = df.loc[is_fy_actual & has_prev_forecast & beat & is_primary].copy()
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
    df["_is_primary"] = _is_primary_mask(df)
    df = df.sort_values([STMT_CODE, STMT_FY_END])

    target_days = 365 * PROFIT_DOUBLING_YEARS
    tolerance_days = 45  # 決算期変更等のブレを許容する

    hits = []
    for code, group in df.groupby(STMT_CODE):
        group = group.reset_index(drop=True)
        for i in range(len(group)):
            cur = group.iloc[i]
            if not cur["_is_primary"]:
                # ヒット自体は実際の開示行に限定する（_is_primary_mask docstring
                # 参照）。比較対象(prev)の探索は合成行を含めたgroup全体から行う。
                continue
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


def detect_profit_growth_major(statements_df: pd.DataFrame) -> pd.DataFrame:
    """同一の決算期タイプ(CurPerType)の前年同期と比べ、経常利益(OdP)が
    +50%以上(1.5倍以上)に増えた開示の一覧。scripts/watch_and_notify.py専用
    （比較期間が前年同期(1年)である点がPROFIT_DOUBLING_YEARS(4年)比較の
    profit_doublingと異なる。detect_sales_growthと同じ考え方で、売上高の
    代わりに経常利益で判定する）。

    前年同期が赤字（0以下）の場合は「何倍」という比率に意味が無いため対象外
    とする（黒字転換はdetect_downward_revisionの逆で別軸の話であり、この
    ルールでは扱わない）。

    J-Quantsは「1開示=1行」だが、同一期(Code, CurPerType, CurPerEn)について
    訂正決算短信等で後から再度開示されることがあり、その場合は同じ期の行が
    複数行存在する。愚直にgroupby().shift(1)すると、どちらが「前の期」に
    なるかがAPIの返却順（実質的には元データの並び順）に依存してしまい、
    訂正で閾値を下回った後でも訂正前の値でヒットし続けたり、同一期の重複行
    同士が0日ギャップで前後関係にされたりする不具合になる
    （detect_current_sales_doublingと同じ問題。2026-08-27のCodexレビューで
    指摘）。比較の計算に入る前に、同一期内は最新の開示日(DiscDate)の行だけに
    絞る。

    同一期内の絞り込み(最新DiscDateの行だけを残す)は、OdPの数値変換・欠損
    除外より前に行う。先にOdPが欠損の行を除外してしまうと、取り下げ・訂正で
    最新の開示がOdPを欠く場合にその行が絞り込みの土俵にすら乗らず、古い
    （既に取り下げられたはずの）非欠損の行がそのまま「その期の代表行」として
    生き残り、訂正前の数値で+50%と誤判定しうる（2026-08-28の8巡目のCodexレビューで
    指摘・修正。同一日タイのケースとは別の、訂正後の値が欠損になるケース）。
    """
    required = {STMT_CODE, STMT_PERIOD_TYPE, STMT_PERIOD_END, STMT_ORDINARY_PROFIT, STMT_DISCLOSED_DATE}
    if statements_df.empty or not required.issubset(statements_df.columns):
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    df = statements_df.copy()
    df[STMT_PERIOD_END] = pd.to_datetime(df[STMT_PERIOD_END], errors="coerce")
    df[STMT_DISCLOSED_DATE] = pd.to_datetime(df[STMT_DISCLOSED_DATE], errors="coerce")
    df = df.dropna(subset=[STMT_PERIOD_END, STMT_DISCLOSED_DATE])

    # 同一日に複数回開示された場合（同日中の訂正等）、DiscDateだけではタイに
    # なる。pandasのsort_values既定のquicksortはタイの並び順を保証しない
    # ため、kind="stable"で明示的に安定ソートし、取得元(get_all_pages)が
    # 返した順序をタイブレークとして使う（detect_current_sales_doublingと
    # 同じ対応。2026-08-27の3巡目のCodexレビューで指摘・修正）。
    key_cols = [STMT_CODE, STMT_PERIOD_TYPE, STMT_PERIOD_END]
    df = df.sort_values(STMT_DISCLOSED_DATE, kind="stable")
    df = df.drop_duplicates(subset=key_cols, keep="last")
    df = df.sort_values([STMT_CODE, STMT_PERIOD_TYPE, STMT_PERIOD_END])

    # 同一期の絞り込みが終わった後で数値変換する。ここでOdPが欠損になる行
    # （取り下げ等でその期の最新開示に数値が無い場合）を明示的にdropnaは
    # しない。その期自体はヒット対象外になり（growthがNaNになり後段のvalid
    # 判定で自然に除外される）、かつ次の期からの比較基準(prev_ordinary_profit)
    # としても「不明」のままNaNが伝播し、誤って古い値まで遡って比較される
    # ことはない。
    df[STMT_ORDINARY_PROFIT] = _to_numeric(df[STMT_ORDINARY_PROFIT])

    df["prev_ordinary_profit"] = df.groupby([STMT_CODE, STMT_PERIOD_TYPE])[STMT_ORDINARY_PROFIT].shift(1)
    df["prev_period_end"] = df.groupby([STMT_CODE, STMT_PERIOD_TYPE])[STMT_PERIOD_END].shift(1)

    valid = df["prev_ordinary_profit"].notna() & (df["prev_ordinary_profit"] > 0)
    gap_days = (df[STMT_PERIOD_END] - df["prev_period_end"]).dt.days
    valid &= gap_days.between(330, 400)
    valid &= _is_primary_mask(df)

    growth = (df[STMT_ORDINARY_PROFIT] - df["prev_ordinary_profit"]) / df["prev_ordinary_profit"]
    df["growth_rate"] = growth
    df["detail"] = "前年同期比 経常利益 " + (df["growth_rate"] * 100).round(1).astype(str) + "% 増"

    hit = df.loc[valid & (growth >= PROFIT_GROWTH_MAJOR_THRESHOLD)].copy()
    hit["rule"] = "profit_growth_major"
    return hit[[STMT_CODE, STMT_DISCLOSED_DATE, "rule", "detail"]].rename(
        columns={STMT_CODE: "Code", STMT_DISCLOSED_DATE: "Date"}
    )


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
    # ヒット自体は実際の開示行に限定する（_is_primary_mask docstring参照）。
    # prior_row(IsPrimary=False)は予想値(FSales/FOP/FNP)を持たないため、既に
    # 上のdropna(subset=[STMT_FORECAST_PROFIT])で除外される。guidance_row
    # (IsPrimary=False)は理論上は予想値を持つが、同一(Code, CurFYEn)内で最も
    # 早い開示になるため通常はhas_prev=Falseとなりヒット候補にならない。将来の
    # 構造変更に備えた保険として明示的にも絞り込む。
    is_primary = _is_primary_mask(df)

    hit = df.loc[has_prev & lowered & is_primary].copy()
    hit["rule"] = "downward_revision"
    hit["detail"] = (
        "通期純利益予想 下方修正 " + hit["prev_forecast_profit"].astype(str)
        + " → " + hit[STMT_FORECAST_PROFIT].astype(str)
    )
    return hit[[STMT_CODE, STMT_DISCLOSED_DATE, "rule", "detail"]].rename(
        columns={STMT_CODE: "Code", STMT_DISCLOSED_DATE: "Date"}
    )


def detect_two_quarter_growth(statements_df: pd.DataFrame) -> pd.DataFrame:
    """直近2四半期が共に増収増益（売上高・経常利益とも前年同期を上回る）の開示を検出する。

    増収増益の判定は同一銘柄・同一決算期タイプ(CurPerType)内で前年同期と比較する
    （detect_sales_growthと同じ手法）。「2期連続」は、対象の開示とその銘柄の
    時系列上ひとつ前の開示（決算期タイプが変わってもよい。例: 1Q→2Q）の
    両方が増収増益であり、かつ両者の決算期末(CurPerEn)が概ね1四半期分
    （60〜120日）しか離れていないことを見る。

    statements_dfに`IsPrimary`列がある場合（src/tdnet_xbrl.py経由の地方株データ。
    実際の開示ではなく、開示に埋め込まれた前年同期実績・翌期予想の合成行を
    IsPrimary=Falseで含む）、「直前の開示」の判定はIsPrimary=True（実際にその日
    行われた開示）の行だけを対象にする。合成行を混ぜたまま時系列で1つ前を
    見ると、実在しない開示が間に挟まったことになり2期連続判定が常に不成立に
    なる（2026-08-19のCodexレビューで指摘、実データで確認）。J-Quants由来の
    データ(1開示=1行、IsPrimary列なし)ではこの列が無いため全行を対象にする
    既存動作のまま変わらない。

    地方株はTDnet添付ファイルの保持期限切れ等により特定の四半期の開示だけ
    取得できないことがある（例: 1Qと3Qは取得できたが2Qが欠落）。この場合、
    単純に「時系列上ひとつ前の開示」を見るだけでは1Qと3Qを連続する2期と
    誤認してしまう。決算期末の間隔が1四半期分から大きく外れる場合は
    「連続した2期」とみなさない（2026-08-19の3巡目のCodexレビューで指摘）。
    """
    required = {
        STMT_CODE, STMT_PERIOD_TYPE, STMT_PERIOD_END, STMT_DISCLOSED_DATE,
        STMT_NET_SALES, STMT_ORDINARY_PROFIT,
    }
    if statements_df.empty or not required.issubset(statements_df.columns):
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    df = statements_df.copy()
    df[STMT_NET_SALES] = _to_numeric(df[STMT_NET_SALES])
    df[STMT_ORDINARY_PROFIT] = _to_numeric(df[STMT_ORDINARY_PROFIT])
    df[STMT_PERIOD_END] = pd.to_datetime(df[STMT_PERIOD_END], errors="coerce")
    df[STMT_DISCLOSED_DATE] = pd.to_datetime(df[STMT_DISCLOSED_DATE], errors="coerce")
    df = df.dropna(subset=[STMT_NET_SALES, STMT_ORDINARY_PROFIT, STMT_PERIOD_END, STMT_DISCLOSED_DATE])

    df = df.sort_values([STMT_CODE, STMT_PERIOD_TYPE, STMT_PERIOD_END])
    df["prev_sales"] = df.groupby([STMT_CODE, STMT_PERIOD_TYPE])[STMT_NET_SALES].shift(1)
    df["prev_profit"] = df.groupby([STMT_CODE, STMT_PERIOD_TYPE])[STMT_ORDINARY_PROFIT].shift(1)
    df["prev_period_end"] = df.groupby([STMT_CODE, STMT_PERIOD_TYPE])[STMT_PERIOD_END].shift(1)

    gap_days = (df[STMT_PERIOD_END] - df["prev_period_end"]).dt.days
    yoy_ok = gap_days.between(330, 400)
    df["grew_yoy"] = (
        yoy_ok
        & df["prev_sales"].notna() & (df["prev_sales"] > 0) & (df[STMT_NET_SALES] > df["prev_sales"])
        & df["prev_profit"].notna() & (df["prev_profit"] > 0) & (df[STMT_ORDINARY_PROFIT] > df["prev_profit"])
    )

    # 「2期連続」判定のため、銘柄内を時系列(開示日)順に並べ直して直前の開示と比較する
    # （実際の開示行のみを対象にする。上のdocstring・_is_primary_mask docstring参照）
    seq = df.loc[_is_primary_mask(df)]
    seq = seq.sort_values([STMT_CODE, STMT_DISCLOSED_DATE])
    seq = seq.copy()
    seq["prev_grew_yoy"] = seq.groupby(STMT_CODE)["grew_yoy"].shift(1)
    seq["prev_period_end_seq"] = seq.groupby(STMT_CODE)[STMT_PERIOD_END].shift(1)
    seq_gap_days = (seq[STMT_PERIOD_END] - seq["prev_period_end_seq"]).dt.days
    is_adjacent_period = seq_gap_days.between(60, 120)
    two_in_a_row = seq["grew_yoy"] & seq["prev_grew_yoy"].fillna(False) & is_adjacent_period.fillna(False)

    hit = seq.loc[two_in_a_row].copy()
    if hit.empty:
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    hit["rule"] = "two_quarter_growth"
    hit["detail"] = (
        "増収増益2期連続（売上 " + hit["prev_sales"].round(0).astype(str) + "→" + hit[STMT_NET_SALES].round(0).astype(str)
        + "、経常利益 " + hit["prev_profit"].round(0).astype(str) + "→" + hit[STMT_ORDINARY_PROFIT].round(0).astype(str)
        + "）"
    )
    return hit[[STMT_CODE, STMT_DISCLOSED_DATE, "rule", "detail"]].rename(
        columns={STMT_CODE: "Code", STMT_DISCLOSED_DATE: "Date"}
    )


def detect_low_pbr(statements_df: pd.DataFrame, quotes_df: pd.DataFrame) -> pd.DataFrame:
    """PBR(株価純資産倍率)が1倍以下の開示を検出する。

    開示時点のBPS(1株純資産)と、開示日以前で直近の株価終値(quotes_df)から計算する
    （現在の株価ではなく、開示時点のPBRで判定する）。
    """
    required_stmt = {STMT_CODE, STMT_DISCLOSED_DATE, STMT_BPS}
    required_quotes = {QUOTES_CODE, QUOTES_DATE, QUOTES_CLOSE}
    if statements_df.empty or not required_stmt.issubset(statements_df.columns):
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])
    if quotes_df.empty or not required_quotes.issubset(quotes_df.columns):
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    df = statements_df.copy()
    df[STMT_BPS] = _to_numeric(df[STMT_BPS])
    df[STMT_DISCLOSED_DATE] = pd.to_datetime(df[STMT_DISCLOSED_DATE], errors="coerce")
    df = df.dropna(subset=[STMT_BPS, STMT_DISCLOSED_DATE])
    df = df.loc[df[STMT_BPS] > 0]
    if df.empty:
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    quotes = quotes_df[[QUOTES_CODE, QUOTES_DATE, QUOTES_CLOSE]].copy()
    quotes[QUOTES_DATE] = pd.to_datetime(quotes[QUOTES_DATE], errors="coerce")
    quotes[QUOTES_CLOSE] = _to_numeric(quotes[QUOTES_CLOSE])
    quotes = quotes.dropna(subset=[QUOTES_DATE, QUOTES_CLOSE])
    quotes = quotes.rename(columns={QUOTES_CODE: "Code", QUOTES_DATE: "Date", QUOTES_CLOSE: "Close"})
    quotes = quotes.sort_values("Date")

    df = df.rename(columns={STMT_CODE: "Code", STMT_DISCLOSED_DATE: "Date"}).sort_values("Date")
    merged = pd.merge_asof(df, quotes, by="Code", on="Date", direction="backward")

    valid = merged[STMT_BPS].notna() & merged["Close"].notna()
    merged["pbr"] = pd.NA
    merged.loc[valid, "pbr"] = merged.loc[valid, "Close"] / merged.loc[valid, STMT_BPS]
    merged["pbr"] = _to_numeric(merged["pbr"])

    result = merged.loc[valid & (merged["pbr"] <= PBR_LOW_THRESHOLD)].copy()
    if result.empty:
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    result["rule"] = "pbr_low"
    result["detail"] = "PBR " + result["pbr"].round(2).astype(str) + "倍"
    return result[["Code", "Date", "rule", "detail"]]


MARKET_UPGRADE_TO_PRIME_KEYWORDS = ["プライム市場への市場区分変更", "プライム市場への上場市場区分変更"]
# 申請準備等を後から取り下げる「開示事項の中止」のお知らせにも同じキーワードが
# 含まれてしまうため、中止系のタイトルは逆方向（ネガティブ）の話として除外する。
MARKET_UPGRADE_TO_PRIME_EXCLUSION_KEYWORDS = ["中止", "取りやめ", "取り下げ", "延期"]


def detect_market_upgrade_to_prime(disclosures_df: pd.DataFrame) -> pd.DataFrame:
    """TDnet開示タイトルから、スタンダード/グロース市場からプライム市場への
    市場区分変更の「申請」または「承認」の発表を検出する。

    以前はequities/masterの日次スナップショットを比較して実際の変更日（効力
    発生日）を検知していたが、これは申請・承認の発表からかなり後になる。
    株価は発表時点で反応することが多いため、株式分割と同様にTDnetの開示
    タイトルから発表日ベースで検出する（タイトルのキーワード一致のため、
    内容は必ずリンク先で確認する。「プライム市場からの」移行、すなわち
    プライムからの降格はキーワードに含まれないため誤検出しない）。
    """
    required = {"company_code", "title", "pubdate"}
    if disclosures_df.empty or not required.issubset(disclosures_df.columns):
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    df = disclosures_df.copy()
    titles = df["title"].fillna("")
    mentions_keyword = titles.apply(lambda t: any(k in t for k in MARKET_UPGRADE_TO_PRIME_KEYWORDS))
    mentions_exclusion = titles.apply(
        lambda t: any(k in t for k in MARKET_UPGRADE_TO_PRIME_EXCLUSION_KEYWORDS)
    )
    hit = df.loc[mentions_keyword & ~mentions_exclusion, ["company_code", "pubdate", "title"]].copy()
    if hit.empty:
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    hit["rule"] = "market_upgrade_to_prime"
    hit["detail"] = "開示タイトル: " + hit["title"]
    hit["Date"] = pd.to_datetime(hit["pubdate"], errors="coerce")
    return hit[["company_code", "Date", "rule", "detail"]].rename(columns={"company_code": "Code"})


def detect_jpx_nikkei_400_selection(disclosures_df: pd.DataFrame) -> pd.DataFrame:
    """TDnet開示タイトルから「JPX日経インデックス400」構成銘柄への選定・採用
    （継続選定を含む）の発表を検出する。

    「日経225」（日経平均株価）自体の構成銘柄入れ替えは日本経済新聞社側の発表の
    みで、対象企業自身がTDnet開示するケースが実データ上ほぼ確認できなかった
    （2026-08-24に2021〜2026年の全開示を確認）。「JPX日経インデックス400」は
    対象企業自身が例年8月頃に「構成銘柄への選定/採用に関するお知らせ」という
    定型タイトルで開示しており、確実に検出できるためこちらを対象にする
    （ユーザーへの確認済み）。
    """
    required = {"company_code", "title", "pubdate"}
    if disclosures_df.empty or not required.issubset(disclosures_df.columns):
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    df = disclosures_df.copy()
    normalized_titles = df["title"].fillna("").apply(lambda t: unicodedata.normalize("NFKC", t))
    mentions_keyword = normalized_titles.apply(lambda t: all(k in t for k in JPX_NIKKEI_400_KEYWORDS))
    mentions_exclusion = normalized_titles.apply(
        lambda t: any(k in t for k in JPX_NIKKEI_400_EXCLUSION_KEYWORDS)
    )
    hit = df.loc[mentions_keyword & ~mentions_exclusion, ["company_code", "pubdate", "title"]].copy()
    if hit.empty:
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"])

    hit["rule"] = "jpx_nikkei_400"
    hit["detail"] = "開示タイトル: " + hit["title"]
    hit["Date"] = pd.to_datetime(hit["pubdate"], errors="coerce")
    return hit[["company_code", "Date", "rule", "detail"]].rename(columns={"company_code": "Code"})


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
