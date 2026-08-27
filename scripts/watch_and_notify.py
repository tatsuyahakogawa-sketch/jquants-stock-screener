"""Discord Webhookへの定期監視・通知バッチ。

GitHub Actions(.github/workflows/watch_and_notify.yml)から平日
10:00・13:00 JSTに実行される想定。app.py（対話的なスクリーニング画面）とは
別に、ユーザーが常時監視してほしいと指定した以下の条件を毎回チェックし、
新しく該当した銘柄があればDiscordに通知する（2026-08-27にユーザー指定）。

  - ストップ高                                  -> rules.detect_stop_high
  - 株式分割・株式併合の発表                     -> rules.detect_stock_split
  - 経常利益が前年同期比+50%以上（1.5倍以上）     -> rules.detect_profit_growth_major
  - 東証本体への新規上場（上場承認発表・当日上場） -> src/jpx_new_listings.py

土日・日本の祝日は市場が休みで新しいデータが出ないためスキップする
(src/market_calendar.py)。

実行方法（ローカル確認用。通常はGitHub Actionsから実行される）:
    JQUANTS_API_KEY=... DISCORD_WEBHOOK_URL=... python scripts/watch_and_notify.py

設計メモ:
  - app.pyのrun_screening()は対話的なUI向けの安全弁（TDnet開示件数が
    MAX_TDNET_DISCLOSURES_FOR_SCREENING件を超えたらタイトル系ルールの検索を
    スキップしてユーザーに期間を絞り込むよう促す）を持つが、この定期監視は
    直近LOOKBACK_DAYS日分の市場全体のTDnet開示を毎回対象にするため、この
    件数はほぼ確実に上限を超える（2026-08-27に実機確認: 直近30日で
    ミラーAPI自体の上限10000件に到達）。この安全弁はユーザーが数年単位の
    期間を指定しうる対話的UI向けであり、この定期監視には合わない（本来
    検出できるはずの分割・併合が毎回「件数超過」で握りつぶされてしまう）
    ため、run_screening()を経由せずrules.py・endpoints.pyの関数を直接
    呼び出す。
  - Discord送信が失敗した場合に「送信済みとして記録されたのに実際には
    届いていない」状態を避けるため、通知済み状態(data/notify_state.json)への
    書き込みはDiscordへの送信が成功した後にだけ行う。
  - GitHub Actionsの各実行は使い捨てのコンテナで、data/以下の変更は
    ワークフロー側でリポジトリにコミットしない限り次回実行時に残らない。
    通知済み状態は日を跨いで重複通知を防ぐために必須のため、
    ワークフロー側でこのファイルの変更をコミット・pushする。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import discord_notify, endpoints, jpx_new_listings, rules, tdnet_client
from src.jquants_client import JQuantsClient
from src.jst import today_jst
from src.market_calendar import is_market_holiday

logger = logging.getLogger(__name__)

STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "notify_state.json"

# 実行が数日空いても取りこぼさないための固定の再走査窓（土日・祝日連続や
# 一時的な実行失敗を想定した余裕）。重複通知は通知済み状態(STATE_PATH)で
# 防ぐため、ここを広めに取っても再通知にはならない。
LOOKBACK_DAYS = 5
# 経常利益の前年同期比較に必要な遡り日数（前年同期(330〜400日)+バッファ60日）。
PROFIT_GROWTH_LOOKBACK_DAYS = 365 + 60
# JPX新規上場情報を「見た」とみなす遡り日数（承認発表の通知漏れ防止用）。
IPO_APPROVAL_LOOKBACK_DAYS = 5
# 通知済み状態を何日分保持するか（これより古いキーは削除してファイル肥大化を防ぐ）。
STATE_RETENTION_DAYS = 60

MARKET_RULE_LABELS = {
    "stop_high": "🔴 ストップ高",
    "stock_split": "✂️ 株式分割の発表",
    "stock_consolidation": "🔗 株式併合の発表",
    "profit_growth_major": "📈 経常利益が前年同期比+50%以上（1.5倍以上）",
}


class Candidate:
    def __init__(self, rule: str, code: str, date: dt.date, message: str):
        self.rule = rule
        self.code = code
        self.date = date
        self.message = message

    @property
    def state_key(self) -> str:
        return f"{self.rule}|{self.code}|{self.date.isoformat()}"


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {"notified": {}}
    with STATE_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("notified", {})
    return data


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


def _prune_state(state: dict, today: dt.date) -> None:
    cutoff_str = (today - dt.timedelta(days=STATE_RETENTION_DAYS)).isoformat()
    notified = state["notified"]
    state["notified"] = {
        key: value for key, value in notified.items() if key.rsplit("|", 1)[-1] >= cutoff_str
    }


def _to_date(value) -> dt.date:
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, dt.datetime):
        return value.date()
    return value


def _fetch_market_wide_hits(client: JQuantsClient, today: dt.date) -> tuple[pd.DataFrame, dict[str, str]]:
    """ストップ高・株式分割/併合・経常利益急増の候補を、直近LOOKBACK_DAYS分まとめて取得する。

    重複排除は呼び出し側(main)が通知済み状態と突き合わせて行うため、ここでは
    LOOKBACK_DAYS window内のヒットをそのまま返す。
    """
    start = today - dt.timedelta(days=LOOKBACK_DAYS)

    listed_info = endpoints.get_listed_info(client)
    name_map: dict[str, str] = {}
    if not listed_info.empty and "Code" in listed_info.columns and "CoName" in listed_info.columns:
        name_map = dict(zip(listed_info["Code"].astype(str), listed_info["CoName"]))

    quotes_df = endpoints.get_daily_quotes_range(client, start, today)
    disclosures_df = tdnet_client.get_disclosures_range(start, today)
    statements_df = endpoints.get_statements_range(
        client, today - dt.timedelta(days=PROFIT_GROWTH_LOOKBACK_DAYS), today
    )

    hits = [
        rules.detect_stop_high(quotes_df),
        rules.detect_stock_split(disclosures_df),
        rules.detect_profit_growth_major(statements_df),
    ]
    hits = [h for h in hits if not h.empty]
    if not hits:
        return pd.DataFrame(columns=["Code", "Date", "rule", "detail"]), name_map

    result = pd.concat(hits, ignore_index=True)
    result["Date"] = pd.to_datetime(result["Date"])
    # 経常利益急増の比較用に広く取得したstatements_dfの過去分のヒットが
    # そのまま混ざらないよう、実際の開示・イベント日がLOOKBACK_DAYS以内の
    # ものだけに絞る。
    in_range = (result["Date"] >= pd.Timestamp(start)) & (result["Date"] < pd.Timestamp(today) + pd.Timedelta(days=1))
    return result.loc[in_range].reset_index(drop=True), name_map


def _market_wide_candidates(client: JQuantsClient, today: dt.date, state: dict) -> list[Candidate]:
    hits, name_map = _fetch_market_wide_hits(client, today)
    candidates = []
    for _, row in hits.sort_values(["Date", "Code"]).iterrows():
        code = str(row["Code"])
        date = _to_date(row["Date"])
        rule = row["rule"]
        key = f"{rule}|{code}|{date.isoformat()}"
        if key in state["notified"]:
            continue
        label = MARKET_RULE_LABELS.get(rule, rule)
        name = name_map.get(code, "")
        message = f"{label}\n{code} {name}\n{row['detail']}（{date:%Y-%m-%d}）"
        candidates.append(Candidate(rule, code, date, message))
    return candidates


def _ipo_candidates(today: dt.date, state: dict) -> tuple[list[Candidate], str | None]:
    """(候補一覧, エラーメッセージ or None) を返す。"""
    try:
        listings = jpx_new_listings.fetch_new_listing_table()
    except Exception as e:
        logger.exception("JPX新規上場会社情報の取得に失敗しました")
        return [], f"⚠️ JPX新規上場会社情報の取得に失敗しました: {e}"

    candidates = []
    approvals = jpx_new_listings.detect_new_listing_approvals(
        listings, since=today - dt.timedelta(days=IPO_APPROVAL_LOOKBACK_DAYS)
    )
    for _, row in approvals.iterrows():
        key = f"ipo_approval|{row['Code']}|{row['ApprovalDate'].isoformat()}"
        if key in state["notified"]:
            continue
        message = (
            f"🆕 新規上場承認\n{row['Code']} {row['CompanyName']}（{row['MarketSegment']}）\n"
            f"上場承認日: {row['ApprovalDate']:%Y-%m-%d} / 上場予定日: {row['ListingDate']:%Y-%m-%d}"
        )
        candidates.append(Candidate("ipo_approval", row["Code"], row["ApprovalDate"], message))

    listed_today = jpx_new_listings.detect_listings_today(listings, today)
    for _, row in listed_today.iterrows():
        key = f"ipo_listed|{row['Code']}|{row['ListingDate'].isoformat()}"
        if key in state["notified"]:
            continue
        message = f"🎉 本日新規上場\n{row['Code']} {row['CompanyName']}（{row['MarketSegment']}）"
        candidates.append(Candidate("ipo_listed", row["Code"], row["ListingDate"], message))

    return candidates, None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    today = today_jst()
    if is_market_holiday(today):
        logger.info("%s は休日のためスキップします。", today)
        return 0

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        logger.error("DISCORD_WEBHOOK_URL が設定されていません。")
        return 1

    state = _load_state()
    had_error = False
    error_messages: list[str] = []

    market_candidates: list[Candidate] = []
    try:
        client = JQuantsClient()
        market_candidates = _market_wide_candidates(client, today, state)
    except Exception as e:
        logger.exception("市場全体の条件チェックに失敗しました")
        had_error = True
        error_messages.append(f"⚠️ ストップ高・株式分割/併合・経常利益急増のチェックに失敗しました: {e}")

    ipo_candidates, ipo_error = _ipo_candidates(today, state)
    if ipo_error:
        had_error = True
        error_messages.append(ipo_error)

    all_candidates = market_candidates + ipo_candidates
    body_messages = [c.message for c in all_candidates] + error_messages

    if not body_messages:
        logger.info("%s: 該当銘柄なし", today)
        return 1 if had_error else 0

    header = f"📅 {today:%Y-%m-%d} のスクリーニング結果（{len(all_candidates)}件）"
    discord_notify.send_discord_message(webhook_url, f"{header}\n\n" + "\n\n".join(body_messages))

    # 通知済み状態への書き込みはDiscordへの送信が成功した後にだけ行う
    # （送信が例外を送出した場合はここに到達せず、次回実行時に再送を試みる）。
    for candidate in all_candidates:
        state["notified"][candidate.state_key] = True
    _prune_state(state, today)
    _save_state(state)

    return 1 if had_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
