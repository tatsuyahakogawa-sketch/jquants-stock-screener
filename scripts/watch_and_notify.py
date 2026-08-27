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


def _hits_to_candidates(
    hits: list[pd.DataFrame],
    name_map: dict[str, str],
    window_start: dt.date,
    today: dt.date,
    state: dict,
    seen_keys: set[str],
) -> list[Candidate]:
    """検出結果のDataFrame群を、まだ通知していないCandidateのリストに変換する。

    重複排除は、前回までに通知済みの状態(state)に加えて、今回の実行内で
    既に候補になったキー(seen_keys)も見る。同一銘柄・同一ルールで日付が
    同じ複数行（例: 同日中に original disclosure と訂正が両方出た場合）が
    1回のhits集合の中に混ざっていても、stateへの反映はDiscord送信成功後まで
    行わないため、seen_keysが無いとstate側のチェックだけでは二重に候補へ
    入ってしまう（2026-08-27のCodexレビューで指摘・修正）。
    """
    hits = [h for h in hits if not h.empty]
    if not hits:
        return []

    result = pd.concat(hits, ignore_index=True)
    result["Date"] = pd.to_datetime(result["Date"])
    in_range = (
        (result["Date"] >= pd.Timestamp(window_start))
        & (result["Date"] < pd.Timestamp(today) + pd.Timedelta(days=1))
    )
    result = result.loc[in_range]

    candidates = []
    for _, row in result.sort_values(["Date", "Code"]).iterrows():
        code = str(row["Code"])
        date = _to_date(row["Date"])
        rule = row["rule"]
        key = f"{rule}|{code}|{date.isoformat()}"
        if key in state["notified"] or key in seen_keys:
            continue
        seen_keys.add(key)
        label = MARKET_RULE_LABELS.get(rule, rule)
        name = name_map.get(code, "")
        message = f"{label}\n{code} {name}\n{row['detail']}（{date:%Y-%m-%d}）"
        candidates.append(Candidate(rule, code, date, message))
    return candidates


def _jquants_candidates(today: dt.date, state: dict, seen_keys: set[str]) -> tuple[list[Candidate], str | None]:
    """J-Quants由来: ストップ高・経常利益急増。TDnetとは独立して失敗しうる
    （TDnetの非公式ミラーは個人運営で不安定なことがある。CLAUDE.md参照）ため、
    このチェック全体を専用のtry/exceptで囲み、TDnet側の失敗と互いに
    影響しないようにする（2026-08-27のCodexレビューで指摘・修正）。
    """
    start = today - dt.timedelta(days=LOOKBACK_DAYS)
    try:
        client = JQuantsClient()

        name_map: dict[str, str] = {}
        try:
            listed_info = endpoints.get_listed_info(client)
            if not listed_info.empty and "Code" in listed_info.columns and "CoName" in listed_info.columns:
                name_map = dict(zip(listed_info["Code"].astype(str), listed_info["CoName"]))
        except Exception:
            logger.warning("上場銘柄マスタの取得に失敗しました（会社名なしで続行します）", exc_info=True)

        # 株価四本値は営業日の大引け後にしか当日分が更新されない
        # (CLAUDE.md参照)。10:00/13:00 JSTはどちらも大引け(15:00頃)より前で、
        # 当日を含めて取得すると空のレスポンスが返る。get_daily_quotes_by_date
        # は日付だけの恒久キーでキャッシュし、get_statements_by_dateのような
        # 当日限定のam/pm一時キーを持たないため、この空レスポンスがそのまま
        # 恒久的にキャッシュされてしまい、大引け後に実際のデータが揃っても
        # 二度と取得されずストップ高を取りこぼす（2026-08-27のCodexレビューで
        # 指摘）。当日はそもそもデータが存在しないため、最初から対象に含めない。
        quotes_df = endpoints.get_daily_quotes_range(client, start, today - dt.timedelta(days=1))
        statements_df = endpoints.get_statements_range(
            client, today - dt.timedelta(days=PROFIT_GROWTH_LOOKBACK_DAYS), today
        )
        hits = [
            rules.detect_stop_high(quotes_df),
            rules.detect_profit_growth_major(statements_df),
        ]
    except Exception as e:
        logger.exception("J-Quants由来の条件チェックに失敗しました")
        return [], f"⚠️ ストップ高・経常利益急増のチェックに失敗しました: {e}"

    candidates = _hits_to_candidates(hits, name_map, start, today, state, seen_keys)
    return candidates, None


def _tdnet_candidates(today: dt.date, state: dict, seen_keys: set[str]) -> tuple[list[Candidate], str | None]:
    """TDnet由来: 株式分割・株式併合。J-Quantsとは独立したtry/exceptで囲む
    （_jquants_candidates docstring参照）。
    """
    start = today - dt.timedelta(days=LOOKBACK_DAYS)
    try:
        # force_refresh=True: 当日分の開示はまだ全件公開されていない可能性が
        # あり、一度キャッシュされると同じ(start, today)の範囲指定では
        # その不完全な結果を返し続けてしまう（tdnet_client.get_disclosures_range
        # のdocstring参照）。このバッチは同じ(start, today)を平日10:00と13:00の
        # 2回呼ぶため、force_refreshしないと13:00の実行が10:00時点の結果を
        # 再利用してしまい、その間に出た分割・併合の発表を取りこぼす
        # （2026-08-27のCodexレビューで指摘・修正）。
        disclosures_df = tdnet_client.get_disclosures_range(start, today, force_refresh=True)
        hits = [rules.detect_stock_split(disclosures_df)]

        name_map: dict[str, str] = {}
        if not disclosures_df.empty and {"company_code", "company_name"}.issubset(disclosures_df.columns):
            name_map = dict(zip(disclosures_df["company_code"].astype(str), disclosures_df["company_name"]))
    except Exception as e:
        logger.exception("TDnet由来の条件チェックに失敗しました")
        return [], f"⚠️ 株式分割/併合のチェックに失敗しました（TDnet取得エラー）: {e}"

    candidates = _hits_to_candidates(hits, name_map, start, today, state, seen_keys)
    return candidates, None


def _ipo_candidates(today: dt.date, state: dict, seen_keys: set[str]) -> tuple[list[Candidate], str | None]:
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
        if key in state["notified"] or key in seen_keys:
            continue
        seen_keys.add(key)
        message = (
            f"🆕 新規上場承認\n{row['Code']} {row['CompanyName']}（{row['MarketSegment']}）\n"
            f"上場承認日: {row['ApprovalDate']:%Y-%m-%d} / 上場予定日: {row['ListingDate']:%Y-%m-%d}"
        )
        candidates.append(Candidate("ipo_approval", row["Code"], row["ApprovalDate"], message))

    listed_today = jpx_new_listings.detect_listings_today(listings, today)
    for _, row in listed_today.iterrows():
        key = f"ipo_listed|{row['Code']}|{row['ListingDate'].isoformat()}"
        if key in state["notified"] or key in seen_keys:
            continue
        seen_keys.add(key)
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
    seen_keys: set[str] = set()

    jquants_candidates, jquants_error = _jquants_candidates(today, state, seen_keys)
    if jquants_error:
        had_error = True
        error_messages.append(jquants_error)

    tdnet_candidates, tdnet_error = _tdnet_candidates(today, state, seen_keys)
    if tdnet_error:
        had_error = True
        error_messages.append(tdnet_error)

    ipo_candidates, ipo_error = _ipo_candidates(today, state, seen_keys)
    if ipo_error:
        had_error = True
        error_messages.append(ipo_error)

    all_candidates = jquants_candidates + tdnet_candidates + ipo_candidates
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
