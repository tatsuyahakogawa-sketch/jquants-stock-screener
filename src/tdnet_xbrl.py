"""TDnet決算短信の「サマリー情報」XBRL(ixbrl.htm)から、J-Quants(/fins/summary)
互換の列名で財務数値を抽出するパーサー。

地方単独上場企業(札幌・福岡・名古屋)はJ-Quants対象外だが、TDnet開示APIが返す
決算短信の`url_xbrl`から取得できるサマリー情報XBRLは、東証上場企業の決算短信と
同一の標準タクソノミ(tse-ed-t:...)を使っている（2026-08-19に実データで確認済み。
札幌・福岡・名古屋単独上場企業でも同じ`tse-`プレフィックスのタグが使われる）。
これを使い、既存のsrc/rules.pyが前提とするJ-Quants由来の列名(Code, DiscDate,
CurPerType, CurPerEn, CurFYEn, Sales, OP, OdP, NP, EqAR, FSales, FOP, FNP)と
同じ形の行を返すことで、判定ロジック本体(rules.py)は変更せず再利用する。

実データ確認結果(2026-08-19、コード3346・1999で確認)で判明した注意点:
  - 四半期報告(1Q/2Q/3Q)は`QuarterlyPeriod`タグ(値"1"/"2"/"3")を持つ。本決算(FY)は
    このタグを持たない
  - 四半期の実績値は`CurrentAccumulatedQ{n}Duration...ResultMember`、本決算は
    `CurrentYearDuration...ResultMember`のcontextRefに入る
  - 連結(Consolidated)と単体(NonConsolidated)の両方が同じタグ名(例:NetSales)で
    別contextRefに入っていることがあるため、連結を優先し単体にフォールバックする
  - 純利益は連結だと`ProfitAttributableToOwnersOfParent`、単体のみの場合は
    `NetIncome`と、連結有無でタグ名自体が変わる
  - 予想値のcontextRefは四半期報告では`CurrentYearDuration...ForecastMember`
    (＝当期の予想、CurFYEnと同じ期を指す)だが、本決算では`NextYearDuration...
    ForecastMember`(＝来期の予想で、CurFYEnとは別の期を指す)になる。本決算行の
    CurFYEnに来期予想を紐付けると異なる期を同一視してしまい誤判定になるため、
    本決算の行では予想値(FSales/FOP/FNP)を取得しない(Noneのまま)
  - **iXBRLの`sign="-"`属性を見落とすと符号が反転したまま読んでしまう**
    （実例: あるコードの前年同期の営業利益は赤字(sign="-"付きで92→実際は-92百万円)
    だったが、sign属性を見ずにテキストだけ読むと+92百万円の黒字と誤読する）
  - **iXBRLの`scale`属性で単位が決まる**（金額系タグはscale="6"=100万円単位、
    比率系タグはscale="-2"=パーセント表示からの比率換算）。「百万円未満切捨て」
    という注記から百万円単位だろうと決め打ちせず、各ファクトのscale属性を
    そのまま使う（提出企業や書式によって単位が変わっても追従できるようにする）
  - **TDnetの開示添付ファイルは公開から約1〜1.5ヶ月で取得できなくなる**
    (2026-08-19に実機確認: 公開41日後は404、36日後は200)。そのため「前回の
    同時期の開示を後から個別に取りに行く」形での過去データのバックフィルは
    実質不可能。一方、決算短信は前年同期の実績値を比較用に本文中へ埋め込んで
    開示する慣行があるため、parse_tanshin_summary_rows()はこれも1行として
    一緒に抽出し、増収率等の前年同期比較(rules.detect_sales_growth等)に
    必要な最低2点を初回スキャンの時点から確保する
  - 四半期報告の貸借対照表側「前期末」比較(contextRef `PriorYearInstant`)は
    「前年の同じ四半期末」ではなく「直前の本決算末」を指しており、期末日
    (CurPerEn)とは異なる時点を指す。これをそのままEqAR/BPSとして前年同期行に
    入れると異なる時点の数値を同一視してしまうため、四半期報告の前年同期行では
    EqAR/BPSを取得しない(本決算報告の前年同期行は文字通り1年前の期末を指すため
    問題なく含める)
  - 当期行(cur_row)には`IsPrimary=True`、開示に埋め込まれた前年同期の実績
    (prior_row)や本決算行に付随する来期予想行(guidance_row)には`IsPrimary=False`
    を付与する。これらの埋め込み行は「今回の開示という1つのイベント」の一部で
    あり、実際に別の日に行われた開示ではないため、rules.detect_two_quarter_growth
    のような「直前の開示と比べる」ロジックにそのまま混ぜると、実在しない開示が
    間に挟まったかのように誤動作する(2026-08-19のCodexレビューで指摘、実際に
    2期連続判定が常に不成立になることを確認)。J-Quants由来のデータ(1開示=1行)
    にはこの列が無いため、rules.py側は列が無い場合は全行をIsPrimary相当として
    扱う後方互換を維持する
  - 本決算(FY)の開示には来期の通期予想(`NextYearDuration...ForecastMember`)も
    含まれる。当期行のCurFYEnに紐付けると期がずれるため取得しないことは既述の
    通りだが、この予想値自体は「翌期の会社予想の基準値」として、翌年のFY開示が
    来るまで下方修正検知(rules.detect_downward_revision)の唯一の比較対象になる。
    取得せず捨てると、翌期の最初の四半期開示でこの予想が下方修正されても
    比較対象が無く検知できない(2026-08-19のCodexレビューで指摘)。そのため
    CurFYEnを翌期に設定した予想専用の行(guidance_row、実績値は全てNone)として
    別途抽出する。CurFYEnは当期の+1年という決め打ちではなく、予想ファクト
    自身のcontextRefが指す実際のend/instant日付を使う（決算期変更等で来期が
    12ヶ月ちょうどでないケースに対応するため。2026-08-19のCodexレビューで指摘）
  - 売上高・営業利益・経常利益・純利益・自己資本比率・1株純資産・予想値は、
    売上高(NetSales)が見つかった連結/単体のどちらのスコープかを基準に、
    残り全てを同じスコープからだけ取得する（1行内で連結の売上高と単体の
    営業利益のような異なるスコープの数値が混在しないようにするため。
    2026-08-19のCodexレビューで指摘・修正。実データ5社(3346, 1999, 63270,
    38560, 75320)で従来通り正しく連結スコープが一貫して選ばれることを確認済み）
  - `FiscalYearEnd`タグはiXBRLの日付変換書式(`format="ixt:dateyearmonthdaycjk"`
    等、"2027年3月31日"のような表示用テキストを実際の値に変換する仕組み)を
    使わず、常にISO形式("2026-06-30")のテキストがそのまま入っていることを
    2026-08-19に実データ5社(3346, 1999, 63270, 38560, 75320)で確認済み
    （Codexから「dateyearmonthdayjp等の変換書式を考慮していないため
    日付が読めない場合がある」との指摘があったが、少なくともFiscalYearEndタグ
    に関しては該当しないことを実データで確認した。他の日付系タグ
    （株主総会予定日等）では実際にこの変換書式が使われていることも確認済みの
    ため、将来別のタグを扱う場合は都度確認が必要）

検出できないこと（既知の制約）:
  - 売上高の取得は`NetSales`タグのみを見ており、IFRS採用企業や銀行・保険・
    不動産業等が使う`OperatingRevenue`等の代替タグには対応していない
    （2026-08-19のCodexレビューで指摘）。地方単独上場企業は現状すべて
    `NetSales`タグの実データで確認できているため、確認が取れるまでは
    未検証のタグ名を推測で追加しない（CLAUDE.md「データの正確性を最優先」
    「誤った推測値を出さない」）。該当企業が現れた場合、その決算短信のXBRLを
    実データで確認した上でタグ候補に追加すること
  - 決算短信(`決算短信`をタイトルに含む開示)以外の財務情報は取得しない。
    「業績予想の修正に関するお知らせ」のような単独の予想修正開示は対象外
    （2026-08-19のCodexレビューで指摘）。そのため、決算短信を挟まずに単独で
    公表された下方修正は、次の決算短信でその修正後の予想が改めて開示される
    までrules.detect_downward_revision()では検知できない（検知が遅れるだけで
    誤った数値を出すわけではないが、直近の下方修正が「下方修正歴なし」に
    見えてしまう期間がありうる）。単独の予想修正開示が決算短信と同じ
    構造化サマリーXBRL(タクソノミ・タグ名)を持つかは未確認のため、
    確認が取れるまでは対象を広げない
"""
from __future__ import annotations

import io
import time
import zipfile

import pandas as pd
import requests
from lxml import etree

_REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0"}
# TDnetの保持期限切れ(404)は再試行しても無駄だが、一時的なタイムアウト・
# 接続エラー・5xxはネットワークの瞬断で起こりうるため、これらだけ短い間隔で
# 数回再試行する（再試行しないと、その決算短信の財務データが永久に欠落する
# ため。regional_stocks.fetch_regional_statements()参照。2026-08-19のCodex
# レビューで指摘）。
_MAX_ATTEMPTS = 3
_RETRY_DELAY_SECONDS = 2


class TdnetXbrlError(Exception):
    pass


def _download(url: str) -> bytes:
    last_exc: requests.exceptions.RequestException | None = None
    for attempt in range(_MAX_ATTEMPTS):
        if attempt > 0:
            time.sleep(_RETRY_DELAY_SECONDS)
        try:
            resp = requests.get(url, timeout=60, headers=_REQUEST_HEADERS)
            resp.raise_for_status()
            return resp.content
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and status < 500:
                raise  # 404等、再試行しても解決しない恒久的なエラーは即座に諦める
            last_exc = exc
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc
    raise last_exc


def _find_summary_ixbrl_name(names: list[str]) -> str | None:
    candidates = [n for n in names if "/Summary/" in n and n.lower().endswith("-ixbrl.htm")]
    return candidates[0] if candidates else None


def _parse_facts(tree) -> tuple[dict[tuple[str, str], object], dict[str, tuple[str | None, str | None, str | None]]]:
    """ixbrl.htmの木から (タグローカル名, contextRef) -> 要素(lxml Element) の辞書と、
    contextRef -> (start, end, instant) の辞書を作る。値の取り出しは_fact_number/
    _fact_text側でscale/sign属性を見ながら行う。
    """
    facts: dict[tuple[str, str], object] = {}
    for elem in tree.xpath("//*[local-name()='nonFraction' or local-name()='nonNumeric']"):
        name = elem.get("name")
        ctx = elem.get("contextRef")
        if not name or not ctx:
            continue
        local = name.split(":")[-1]
        facts.setdefault((local, ctx), elem)

    contexts: dict[str, tuple[str | None, str | None, str | None]] = {}
    for c in tree.xpath("//*[local-name()='context']"):
        cid = c.get("id")
        if not cid:
            continue
        start = c.xpath(".//*[local-name()='startDate']/text()")
        end = c.xpath(".//*[local-name()='endDate']/text()")
        instant = c.xpath(".//*[local-name()='instant']/text()")
        contexts[cid] = (
            start[0].strip() if start else None,
            end[0].strip() if end else None,
            instant[0].strip() if instant else None,
        )
    return facts, contexts


def _fact_text(elem) -> str | None:
    text = "".join(elem.itertext()).strip()
    return text or None


def _fact_number(elem) -> float | None:
    """nonFraction要素の実際の数値を、scale・sign属性を反映して返す。

    iXBRLでは表示テキストがそのまま実値とは限らない。例えば
    scale="6"のNetSales="378"は実際には378×10^6=378,000,000円、
    sign="-"付きのOperatingIncome="92"は実際には-92百万円(赤字)を意味する。
    これを見落とすと単位や符号を誤り、誤った判定に直結するため必ず両方見る。
    """
    if elem.get("{http://www.w3.org/2001/XMLSchema-instance}nil") == "true":
        return None
    text = _fact_text(elem)
    if text is None:
        return None
    cleaned = text.replace(",", "")
    if cleaned in ("", "－", "-", "―", "…"):
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None

    scale = elem.get("scale")
    if scale is not None:
        try:
            value *= 10 ** int(scale)
        except ValueError:
            pass
    if elem.get("sign") == "-":
        value = -value
    return value


def _quarterly_period(facts: dict[tuple[str, str], object]) -> str | None:
    for (name, _ctx), elem in facts.items():
        if name == "QuarterlyPeriod":
            text = _fact_text(elem)
            return text.strip() if text else None
    return None


def _consolidation_rank(context_ref: str) -> int:
    # "ConsolidatedMember"は"NonConsolidatedMember"の部分文字列のため、
    # 単体(NonConsolidated)を先に判定する。
    if "NonConsolidatedMember" in context_ref:
        return 1
    if "ConsolidatedMember" in context_ref:
        return 0
    return 2


def _find_fact(
    facts: dict[tuple[str, str], object],
    tag_candidates: list[str],
    context_prefix: str,
    required_substring: str,
    *,
    required_scope: str | None = None,
):
    """tag_candidatesのいずれか、かつcontextRefがcontext_prefixで始まり
    required_substringを含むファクトを探す。連結(Consolidated)を優先する。
    戻り値: (要素, 見つかったcontextRef)。無ければ(None, None)。

    required_scope（"ConsolidatedMember"または"NonConsolidatedMember"）を
    指定すると、そのスコープのcontextRefだけを対象にする。指定しないと
    タグごとに独立して連結/単体を選んでしまい、例えば売上高は連結・
    営業利益はそのタグだけ単体、のように1行内でスコープが混在した数値に
    なりうる（2026-08-19のCodexレビューで指摘）。_extract_row()では
    売上高で決まったスコープを他の全指標に対しても固定して使う。
    """
    matches = []
    for tag in tag_candidates:
        for (name, ctx), elem in facts.items():
            if name != tag or not ctx.startswith(context_prefix) or required_substring not in ctx:
                continue
            # "ConsolidatedMember"は"NonConsolidatedMember"の部分文字列のため、
            # required_scope not in ctx という単純な包含チェックだと単体側の
            # contextRefも連結スコープの条件を誤って満たしてしまう。_scope_of()
            # の判定結果同士を比較する。
            if required_scope is not None and _scope_of(ctx) != required_scope:
                continue
            matches.append((_consolidation_rank(ctx), ctx, elem))
    if not matches:
        return None, None
    matches.sort(key=lambda t: t[0])
    return matches[0][2], matches[0][1]


def _scope_of(context_ref: str) -> str | None:
    if "NonConsolidatedMember" in context_ref:
        return "NonConsolidatedMember"
    if "ConsolidatedMember" in context_ref:
        return "ConsolidatedMember"
    return None


def _extract_row(
    facts: dict[tuple[str, str], object],
    contexts: dict[str, tuple[str | None, str | None, str | None]],
    code: str,
    disclosed_date,
    period_type: str,
    actual_prefix: str,
    *,
    include_balance_sheet: bool,
    forecast_prefix: str | None,
    is_primary: bool,
) -> dict | None:
    sales_elem, sales_ctx = _find_fact(facts, ["NetSales"], actual_prefix, "ResultMember")
    if sales_elem is None:
        return None
    scope = _scope_of(sales_ctx)
    op_elem, _ = _find_fact(facts, ["OperatingIncome"], actual_prefix, "ResultMember", required_scope=scope)
    odp_elem, _ = _find_fact(facts, ["OrdinaryIncome"], actual_prefix, "ResultMember", required_scope=scope)
    np_elem, _ = _find_fact(
        facts, ["ProfitAttributableToOwnersOfParent", "NetIncome"], actual_prefix, "ResultMember",
        required_scope=scope,
    )

    eqar_elem = bps_elem = None
    if include_balance_sheet:
        instant_prefix = actual_prefix.replace("Duration", "Instant")
        eqar_elem, _ = _find_fact(facts, ["CapitalAdequacyRatio"], instant_prefix, "ResultMember", required_scope=scope)
        bps_elem, _ = _find_fact(facts, ["NetAssetsPerShare"], instant_prefix, "ResultMember", required_scope=scope)

    fsales_elem = fop_elem = fnp_elem = None
    if forecast_prefix is not None:
        fsales_elem, _ = _find_fact(facts, ["NetSales"], forecast_prefix, "ForecastMember", required_scope=scope)
        fop_elem, _ = _find_fact(facts, ["OperatingIncome"], forecast_prefix, "ForecastMember", required_scope=scope)
        fnp_elem, _ = _find_fact(
            facts, ["ProfitAttributableToOwnersOfParent", "NetIncome"], forecast_prefix, "ForecastMember",
            required_scope=scope,
        )

    period_end = None
    if sales_ctx and sales_ctx in contexts:
        _start, end, instant = contexts[sales_ctx]
        period_end = end or instant

    return {
        "Code": code,
        "DiscDate": disclosed_date,
        "CurPerType": period_type,
        "CurPerEn": pd.to_datetime(period_end, errors="coerce") if period_end else pd.NaT,
        "Sales": _fact_number(sales_elem),
        "OP": _fact_number(op_elem) if op_elem is not None else None,
        "OdP": _fact_number(odp_elem) if odp_elem is not None else None,
        "NP": _fact_number(np_elem) if np_elem is not None else None,
        "EqAR": _fact_number(eqar_elem) if eqar_elem is not None else None,
        "FSales": _fact_number(fsales_elem) if fsales_elem is not None else None,
        "FOP": _fact_number(fop_elem) if fop_elem is not None else None,
        "FNP": _fact_number(fnp_elem) if fnp_elem is not None else None,
        "BPS": _fact_number(bps_elem) if bps_elem is not None else None,
        "IsPrimary": is_primary,
    }


def _extract_guidance_row(
    facts: dict[tuple[str, str], object],
    contexts: dict[str, tuple[str | None, str | None, str | None]],
    code: str,
    disclosed_date,
) -> dict | None:
    """本決算(FY)開示に含まれる来期の通期会社予想(NextYearDuration...
    ForecastMember)だけを抽出し、CurFYEnを翌期の予想専用の行として返す
    （実績値(Sales/OP/OdP/NP/EqAR/BPS)は全てNone）。予想値が1つも無ければNone。
    IsPrimary=False（モジュールdocstring参照。実在の開示ではなく、当期の開示に
    埋め込まれた翌期予想であるため）。

    CurFYEnは当期のCurFYEnに単純に+1年するのではなく、予想ファクト自身の
    contextRefが指すend/instant日付をそのまま使う。決算期変更等で来期が
    12ヶ月ちょうどでない場合、+1年で決め打ちすると実際の予想対象期間と
    ずれてしまうため（2026-08-19のCodexレビューで指摘）。対応するcontextRefが
    見つからない場合はCurFYEn=NaTとし、以降のFY単位の突き合わせでは無視
    される（誤った期に紐付けるより安全なため、決め打ちの代替値は使わない）。
    """
    fsales_elem, fsales_ctx = _find_fact(facts, ["NetSales"], "NextYearDuration", "ForecastMember")
    fop_elem, fop_ctx = _find_fact(facts, ["OperatingIncome"], "NextYearDuration", "ForecastMember")
    fnp_elem, fnp_ctx = _find_fact(
        facts, ["ProfitAttributableToOwnersOfParent", "NetIncome"], "NextYearDuration", "ForecastMember"
    )
    anchor_ctx = fsales_ctx or fop_ctx or fnp_ctx
    if anchor_ctx is None:
        return None

    # 売上高予想のスコープ(連結/単体)を基準に、営業利益・純利益予想も同じ
    # スコープだけを対象にする（_extract_row()と同じ理由。スコープ混在防止）。
    scope = _scope_of(anchor_ctx)
    if scope is not None:
        fsales_elem, fsales_ctx = _find_fact(
            facts, ["NetSales"], "NextYearDuration", "ForecastMember", required_scope=scope
        )
        fop_elem, fop_ctx = _find_fact(
            facts, ["OperatingIncome"], "NextYearDuration", "ForecastMember", required_scope=scope
        )
        fnp_elem, fnp_ctx = _find_fact(
            facts, ["ProfitAttributableToOwnersOfParent", "NetIncome"], "NextYearDuration", "ForecastMember",
            required_scope=scope,
        )
        anchor_ctx = fsales_ctx or fop_ctx or fnp_ctx

    fsales = _fact_number(fsales_elem) if fsales_elem is not None else None
    fop = _fact_number(fop_elem) if fop_elem is not None else None
    fnp = _fact_number(fnp_elem) if fnp_elem is not None else None
    if fsales is None and fop is None and fnp is None:
        return None

    next_fy_end = None
    if anchor_ctx and anchor_ctx in contexts:
        _start, end, instant = contexts[anchor_ctx]
        next_fy_end = end or instant

    return {
        "Code": code,
        "DiscDate": disclosed_date,
        "CurPerType": "FY",
        "CurPerEn": pd.NaT,
        "CurFYEn": pd.to_datetime(next_fy_end, errors="coerce") if next_fy_end else pd.NaT,
        "Sales": None,
        "OP": None,
        "OdP": None,
        "NP": None,
        "EqAR": None,
        "FSales": fsales,
        "FOP": fop,
        "FNP": fnp,
        "BPS": None,
        "IsPrimary": False,
    }


def parse_tanshin_summary_rows(zip_bytes: bytes, code: str, disclosed_date) -> list[dict]:
    """決算短信サマリー情報XBRLのzipバイト列を、rules.pyのSTMT_*列と同じ形の
    行のリストに変換する。当期分(IsPrimary=True)に加え、開示に埋め込まれている
    前年同期の実績(IsPrimary=False)、本決算の場合はさらに来期予想
    (IsPrimary=False)も1行として抽出するため、最大3行を返す
    （モジュールdocstring参照。パースできない/必要なタグが無い場合は空リスト）。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            name = _find_summary_ixbrl_name(zf.namelist())
            if name is None:
                return []
            content = zf.read(name)
    except zipfile.BadZipFile:
        return []

    tree = etree.fromstring(content, parser=etree.XMLParser(recover=True, huge_tree=True))
    if tree is None:
        return []
    facts, contexts = _parse_facts(tree)
    if not facts:
        return []

    quarter = _quarterly_period(facts)
    is_quarterly = quarter in ("1", "2", "3")
    if is_quarterly:
        cur_period_type = f"{quarter}Q"
        cur_prefix = f"CurrentAccumulatedQ{quarter}Duration"
        prior_prefix = f"PriorAccumulatedQ{quarter}Duration"
        forecast_prefix = "CurrentYearDuration"  # 進行中の当期予想(CurFYEnと同じ期)
    else:
        cur_period_type = "FY"
        cur_prefix = "CurrentYearDuration"
        prior_prefix = "PriorYearDuration"
        forecast_prefix = None  # 予想値は来期分(NextYearDuration)でCurFYEnとは別の期のため取得しない

    fy_end_elem, _ = _find_fact(facts, ["FiscalYearEnd"], "CurrentYearInstant", "")
    fy_end_raw = _fact_text(fy_end_elem) if fy_end_elem is not None else None
    if fy_end_raw is None:
        return []
    cur_fy_end = pd.to_datetime(fy_end_raw, errors="coerce")
    if pd.isna(cur_fy_end):
        return []

    cur_row = _extract_row(
        facts, contexts, code, disclosed_date, cur_period_type, cur_prefix,
        include_balance_sheet=True, forecast_prefix=forecast_prefix, is_primary=True,
    )
    if cur_row is None:
        return []
    cur_row["CurFYEn"] = cur_fy_end
    rows = [cur_row]

    prior_row = _extract_row(
        facts, contexts, code, disclosed_date, cur_period_type, prior_prefix,
        include_balance_sheet=not is_quarterly, forecast_prefix=None, is_primary=False,
    )
    if prior_row is not None:
        prior_row["CurFYEn"] = cur_fy_end - pd.DateOffset(years=1)
        rows.append(prior_row)

    if not is_quarterly:
        guidance_row = _extract_guidance_row(facts, contexts, code, disclosed_date)
        if guidance_row is not None and pd.notna(guidance_row["CurFYEn"]):
            rows.append(guidance_row)

    return rows


def fetch_tanshin_statement_rows(url_xbrl: str, code: str, disclosed_date) -> list[dict]:
    """決算短信のurl_xbrl(TDnet開示APIの列)をダウンロードしてparse_tanshin_summary_rowsに渡す。
    ダウンロード失敗（TDnetの添付ファイル保持期限切れによる404等）やパース失敗時は空リストを返す。
    """
    try:
        zip_bytes = _download(url_xbrl)
    except requests.exceptions.RequestException:
        return []
    return parse_tanshin_summary_rows(zip_bytes, code, disclosed_date)
