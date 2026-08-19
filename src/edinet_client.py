"""EDINET(金融庁)から有価証券報告書の「事業の内容」「大株主の状況」「潜在株式の
状況」を取得するクライアント。

これらはJ-Quantsには無い自由記述項目だが、EDINETの開示書類には実データとして
存在する。ただし個別の構造化要素としては取れず、1つのテキストブロック要素の中に
HTML（大株主は表）がまるごと入っている（2026-07-29 実データで確認済み、
詳細はREADME.md参照）。LLMによる要約・推測は行わず、開示されている内容を
そのまま抜き出して使う。

Subscription-Key（無料、電話番号登録必須。
https://api.edinet-fsa.go.jp/api/auth/index.aspx?mode=1 ）が必要。
証券コード→EDINETコードの対応表は認証不要の静的CSVから取得できる。
"""
from __future__ import annotations

import datetime as dt
import io
import os
import re
import zipfile

import pandas as pd
import requests
from lxml import etree

from src import cache
from src.jst import today_jst

BASE_URL = "https://api.edinet-fsa.go.jp/api/v2"
CODE_LIST_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"

# 有価証券報告書・訂正有価証券報告書（大量保有報告書等の他の書類種別は対象外）
_YUHO_DOC_TYPE_CODES = {"120", "130"}

_BUSINESS_OVERVIEW_TAGS = ["DescriptionOfBusinessTextBlock"]
_SHAREHOLDERS_TAGS = ["MajorShareholdersTextBlock"]
_POTENTIAL_SHARES_TAGS = ["PotentialSharesTextBlock", "DilutedSharesTextBlock"]


class EdinetAuthError(Exception):
    pass


def _api_key() -> str:
    key = os.environ.get("EDINET_API_KEY")
    if not key:
        raise EdinetAuthError(
            "EDINET_API_KEY が設定されていません。.env に EDINET_API_KEY=... を追記してください"
            "（無料登録: https://api.edinet-fsa.go.jp/api/auth/index.aspx?mode=1 ）。"
        )
    return key


def _load_code_list() -> pd.DataFrame:
    """証券コード→EDINETコードの対応表を取得する（認証不要、日次更新なので1日キャッシュ）。"""
    today_str = today_jst().strftime("%Y%m%d")
    cached = cache.load("edinet_code_list", today_str)
    if cached is not None:
        return cached

    resp = requests.get(CODE_LIST_URL, timeout=30)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        csv_name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
        with zf.open(csv_name) as f:
            # EDINETコードリストはヘッダーが2行目、cp932(Shift-JIS系)エンコード
            df = pd.read_csv(f, encoding="cp932", skiprows=1)

    cache.save("edinet_code_list", today_str, df)
    return df


def get_edinet_code(stock_code: str) -> str | None:
    """証券コード(4桁または5桁)からEDINETコード(例: E01753)を引く。"""
    df = _load_code_list()
    if df.empty or "証券コード" not in df.columns:
        return None
    code = str(stock_code)
    candidates = {code}
    if code.isdigit() and len(code) == 4:
        candidates.add(code + "0")
    match = df.loc[df["証券コード"].astype(str).str.strip().isin(candidates)]
    if match.empty:
        return None
    return str(match.iloc[0]["ＥＤＩＮＥＴコード"]) if "ＥＤＩＮＥＴコード" in match.columns else str(match.iloc[0].iloc[0])


def _get_documents_for_date(date: dt.date, key: str) -> list[dict]:
    # 当日分はまだ提出が続いている途中の可能性があるため、キャッシュの
    # 読み書き両方をスキップして毎回取り直す（キャッシュしてしまうと、
    # その日の後で提出された書類を翌日まで拾えなくなる）。
    is_today = date == today_jst()
    date_str = date.strftime("%Y%m%d")
    if not is_today:
        cached = cache.load("edinet_documents", date_str)
        if cached is not None:
            return cached.to_dict("records")

    resp = requests.get(
        f"{BASE_URL}/documents.json",
        params={"date": date.strftime("%Y-%m-%d"), "type": 2, "Subscription-Key": key},
        timeout=30,
    )
    if resp.status_code == 401:
        raise EdinetAuthError(f"EDINET認証に失敗しました（APIキーを確認してください）: {resp.text}")
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if not is_today:
        df = pd.DataFrame(results) if results else pd.DataFrame()
        cache.save("edinet_documents", date_str, df)
    return results


def find_latest_yuho(edinet_code: str, fiscal_year_end: dt.date | None = None) -> dict | None:
    """指定EDINETコードの直近の有価証券報告書（訂正含む）のメタデータを検索する。

    EDINETの書類一覧API(documents.json)は日付単位でしか検索できないため、
    決算期末(fiscal_year_end)が分かる場合は提出期限（通常3ヶ月以内）付近の
    期間だけを走査する。不明な場合は直近1年分を走査する（呼び出し回数が
    かさむので事前にfiscal_year_endを渡すことを推奨）。
    """
    key = _api_key()
    today = today_jst()
    if fiscal_year_end is not None:
        window_start = fiscal_year_end + dt.timedelta(days=1)
        window_end = min(fiscal_year_end + dt.timedelta(days=130), today)
    else:
        window_end = today
        window_start = window_end - dt.timedelta(days=365)

    found = []
    d = window_end
    while d >= window_start:
        for doc in _get_documents_for_date(d, key):
            if doc.get("edinetCode") == edinet_code and doc.get("docTypeCode") in _YUHO_DOC_TYPE_CODES:
                found.append(doc)
        d -= dt.timedelta(days=1)

    if not found:
        return None
    found.sort(key=lambda x: x.get("submitDateTime", ""))
    return found[-1]


def _download_xbrl_zip(doc_id: str, key: str) -> bytes:
    cache_dir = os.path.join("data", "cache", "edinet_xbrl")
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{doc_id}.zip")
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()

    resp = requests.get(
        f"{BASE_URL}/documents/{doc_id}",
        params={"type": 1, "Subscription-Key": key},
        timeout=60,
    )
    if resp.status_code == 401:
        raise EdinetAuthError(f"EDINET認証に失敗しました（APIキーを確認してください）: {resp.text}")
    resp.raise_for_status()
    with open(path, "wb") as f:
        f.write(resp.content)
    return resp.content


def _find_text_block(zip_bytes: bytes, tag_names: list[str]) -> str | None:
    """XBRL(iXBRL)のzipから、指定タグ名（ローカル名、名前空間prefix無視）の
    テキストブロック要素を探し、中身のHTMLをプレーンテキストに変換して返す。

    EDINETはInline XBRL形式のため、通常のXBRL要素(<jpcrp_cor:XxxTextBlock>)と
    してだけでなく、HTML本文中の<ix:nonNumeric name="jpcrp_cor:XxxTextBlock">に
    包まれている場合もあるため両方を探す。
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        candidates = [
            n for n in zf.namelist()
            if n.lower().endswith((".xbrl", ".xml", ".htm", ".html"))
        ]
        for name in candidates:
            try:
                content = zf.read(name)
                tree = etree.fromstring(content, parser=etree.XMLParser(recover=True, huge_tree=True))
            except Exception:
                continue
            if tree is None:
                continue

            for tag in tag_names:
                # 通常のXBRL要素として（名前空間prefixは無視してローカル名で探す）
                elems = tree.xpath(f"//*[local-name()='{tag}']")
                for elem in elems:
                    text = _element_to_text(elem)
                    if text:
                        return text

                # Inline XBRLの<ix:nonNumeric name="...:Tag">としても探す
                elems = tree.xpath(
                    f"//*[local-name()='nonNumeric' and contains(@name, '{tag}')]"
                )
                for elem in elems:
                    text = _element_to_text(elem)
                    if text:
                        return text
    return None


def _element_to_text(elem) -> str | None:
    """要素の中身（ネストしたHTMLタグを含む）をプレーンテキストに変換する。"""
    raw = "".join(elem.itertext()).strip()
    if raw:
        # HTML実体参照や連続する空白・改行を整理
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()
    return None


def _find_shareholders_table(zip_bytes: bytes) -> pd.DataFrame | None:
    """大株主の状況（MajorShareholdersTextBlock）から埋め込まれたHTML表を抜き出す。"""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        candidates = [
            n for n in zf.namelist()
            if n.lower().endswith((".xbrl", ".xml", ".htm", ".html"))
        ]
        for name in candidates:
            try:
                content = zf.read(name)
                tree = etree.fromstring(content, parser=etree.XMLParser(recover=True, huge_tree=True))
            except Exception:
                continue
            if tree is None:
                continue

            elems = tree.xpath("//*[local-name()='MajorShareholdersTextBlock']")
            elems += tree.xpath(
                "//*[local-name()='nonNumeric' and contains(@name, 'MajorShareholdersTextBlock')]"
            )
            for elem in elems:
                html_fragment = etree.tostring(elem, method="html", encoding="unicode")
                try:
                    tables = pd.read_html(io.StringIO(html_fragment))
                except ValueError:
                    continue
                if tables:
                    return _normalize_shareholders_table(tables[0])
    return None


def _find_shareholder_columns(df: pd.DataFrame) -> tuple:
    name_col = share_col = pct_col = None
    for col in df.columns:
        col_str = str(col)
        if name_col is None and ("氏名" in col_str or "名称" in col_str):
            name_col = col
        if share_col is None and "株式数" in col_str:
            share_col = col
        if pct_col is None and ("比率" in col_str or "割合" in col_str):
            pct_col = col
    return name_col, share_col, pct_col


def _normalize_shareholders_table(table: pd.DataFrame) -> pd.DataFrame:
    """列名の表記ゆれ（氏名/名称、株式数/所有株式数、比率/割合等）を吸収する。

    EDINETの表はヘッダーが日付見出し等と2段になっており、pandas.read_htmlが
    ヘッダー行を検出できずデータ行として読み込んでしまうことがある。その場合、
    「氏名」または「名称」を含む行を探してヘッダーとして採用し、それより前の
    行と「計」「合計」の行（株主ではない）は除外する。
    """
    df = table.copy()
    name_col, share_col, pct_col = _find_shareholder_columns(df)

    if name_col is None:
        header_row_idx = None
        for idx, row in df.iterrows():
            if row.astype(str).str.contains("氏名|名称").any():
                header_row_idx = idx
                break
        if header_row_idx is None:
            return table
        df.columns = df.loc[header_row_idx]
        df = df.loc[header_row_idx + 1:].reset_index(drop=True)
        name_col, share_col, pct_col = _find_shareholder_columns(df)

    if name_col is None:
        return table

    out = df[[c for c in [name_col, share_col, pct_col] if c is not None]].copy()
    out.columns = [c for c, orig in zip(["株主名", "所有株式数", "所有比率"], [name_col, share_col, pct_col]) if orig is not None]
    out = out[~out["株主名"].astype(str).str.contains("計|合計")]
    return out.reset_index(drop=True)


def fetch_yuho_texts(stock_code: str, fiscal_year_end: dt.date | None = None) -> dict:
    """証券コードから、直近の有報の事業概要・大株主・潜在株式の状況を取得する。

    戻り値: {"business_overview": str|None, "shareholders": DataFrame|None,
             "potential_shares": str|None, "doc_id": str|None}
    該当書類が無い、またはEDINET_API_KEY未設定の場合は全項目Noneで返す
    （呼び出し側でエラーにせず、その項目を空欄のままにする運用のため）。
    タグが見つからない場合は「開示が無い」ことと「取得に失敗した」ことを
    区別できないため、いずれもNoneとして扱う（無いものを「該当なし」と
    断定的に書かない）。
    """
    empty = {"business_overview": None, "shareholders": None, "potential_shares": None, "doc_id": None}
    try:
        edinet_code = get_edinet_code(stock_code)
        if edinet_code is None:
            return empty
        doc = find_latest_yuho(edinet_code, fiscal_year_end)
        if doc is None:
            return empty
        key = _api_key()
        zip_bytes = _download_xbrl_zip(doc["docID"], key)
    except EdinetAuthError:
        # EDINET_API_KEY未設定・無効の場合は機能を無効化するだけで、
        # J-Quants側の他の項目には影響させない（README参照）。
        return empty
    except Exception:
        return empty

    return {
        "business_overview": _find_text_block(zip_bytes, _BUSINESS_OVERVIEW_TAGS),
        "shareholders": _find_shareholders_table(zip_bytes),
        "potential_shares": _find_text_block(zip_bytes, _POTENTIAL_SHARES_TAGS),
        "doc_id": doc.get("docID"),
    }
