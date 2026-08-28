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
    届いていない」状態を避けるため、通知(候補)は1件ずつ個別のメッセージで
    送信し、成功するたびに直ちに通知済みとして記録・保存する（まとめて1通に
    送ると、2000文字超で複数リクエストに分割された際、途中で失敗した時点で
    それ以前に届いた分もまとめて未記録のままになり、次回実行で二重に届いて
    しまう。2026-08-27のCodexレビューで指摘・修正）。
  - GitHub Actionsの各実行は使い捨てのコンテナで、data/以下の変更は
    ワークフロー側でリポジトリにコミットしない限り次回実行時に残らない。
    通知済み状態・ウォーターマーク（state["jquants_watermark"]等。前回
    成功した走査の開始日。_scan_start参照）は日を跨いで重複通知・取りこぼしを
    防ぐために必須のため、ワークフロー側でこの変更をコミット・pushする
    （mainに直接コミットするとStreamlit Cloudの自動デプロイが不要にトリガー
    されるため、mainとは別のnotify-stateブランチに保存する。
    .github/workflows/watch_and_notify.yml参照。2026-08-27のCodexレビューで
    指摘・修正）。
  - 固定のLOOKBACK_DAYS(初回実行時のみ使用)だけに頼ると、ゴールデンウィーク
    等で市場休場日が実行間隔を超えて連続した場合にデータを取りこぼす
    （2026-08-27のCodexレビューで指摘）。次回はウォーターマーク
    （前回成功した走査の開始日）から走査することで、実行間隔が空いても
    （MAX_CATCH_UP_DAYSの上限まで）取りこぼさないようにする(_scan_start参照)。
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

# 初回実行時（まだウォーターマークが無い場合）の再走査窓。2回目以降は
# state["jquants_watermark"]/state["tdnet_watermark"]（前回成功した走査の
# 開始日）を使うため、この値は初回のみ意味を持つ。
LOOKBACK_DAYS = 5
# ウォーターマークを使っても、ゴールデンウィーク等の長期休場明けに大きく
# 遡りすぎないための上限（それより前の欠落は「取りこぼし」として許容する）。
MAX_CATCH_UP_DAYS = 30
# 経常利益の前年同期比較に必要な遡り日数（前年同期(330〜400日)+バッファ60日）。
PROFIT_GROWTH_LOOKBACK_DAYS = 365 + 60
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


def _scan_start(state: dict, watermark_key: str, today: dt.date) -> dt.date:
    """このソースを今回どこから走査するかを決める。

    固定のLOOKBACK_DAYSだけだと、ゴールデンウィーク等で市場休場日が
    連続した場合（例: 金曜の後、月〜木が祝日で次の実行が木曜になる等）に
    実際の実行間隔がLOOKBACK_DAYSを超え、その間に確定した株価・開示が
    一度も走査されないまま欠落しうる（2026-08-27のCodexレビューで指摘）。
    前回成功した走査の開始日をウォーターマークとして保存しておき、次回は
    そこから走査することで、実行間隔がどれだけ空いても（MAX_CATCH_UP_DAYSの
    上限まで）取りこぼさないようにする。
    """
    raw = state.get(watermark_key)
    if raw:
        try:
            watermark = dt.date.fromisoformat(raw)
            return max(watermark, today - dt.timedelta(days=MAX_CATCH_UP_DAYS))
        except ValueError:
            logger.warning("%sの値が不正なため無視します: %r", watermark_key, raw)
    return today - dt.timedelta(days=LOOKBACK_DAYS)


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
    start = _scan_start(state, "jquants_watermark", today)
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
    # ここまで到達した時点でこの範囲の走査自体は成功しているため、次回の
    # 開始日を進める（Discordへの通知が後で失敗しても、再走査で無駄な
    # API呼び出しが増えるだけで正しさには影響しないため、送信結果を待たずに
    # ここで更新してよい）。
    state["jquants_watermark"] = today.isoformat()
    return candidates, None


def _tdnet_candidates(today: dt.date, state: dict, seen_keys: set[str]) -> tuple[list[Candidate], str | None]:
    """TDnet由来: 株式分割・株式併合。J-Quantsとは独立したtry/exceptで囲む
    （_jquants_candidates docstring参照）。
    """
    start = _scan_start(state, "tdnet_watermark", today)
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
    state["tdnet_watermark"] = today.isoformat()  # _jquants_candidatesと同じ理由
    return candidates, None


def _ipo_candidates(today: dt.date, state: dict, seen_keys: set[str]) -> tuple[list[Candidate], str | None]:
    """(候補一覧, エラーメッセージ or None) を返す。"""
    try:
        listings = jpx_new_listings.fetch_new_listing_table()
    except Exception as e:
        logger.exception("JPX新規上場会社情報の取得に失敗しました")
        return [], f"⚠️ JPX新規上場会社情報の取得に失敗しました: {e}"

    candidates = []
    # 実行間隔が長期休場等で空いても承認発表を取りこぼさないよう、
    # _jquants_candidates/_tdnet_candidatesと同じウォーターマーク方式にする
    # （2026-08-27のCodexレビューで指摘・修正）。
    since = _scan_start(state, "ipo_watermark", today)
    approvals = jpx_new_listings.detect_new_listing_approvals(listings, since=since)
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

    state["ipo_watermark"] = today.isoformat()  # _jquants_candidatesと同じ理由
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

    if not all_candidates and not error_messages:
        logger.info("%s: 該当銘柄なし", today)
        # ヒットが無くてもウォーターマーク(state["*_watermark"])の更新は
        # 保存する必要があるため、このまま返さずstateを保存してから終える。
        _prune_state(state, today)
        _save_state(state)
        return 1 if had_error else 0

    # 1件ずつ個別のメッセージとして送信し、成功するたびに直ちに通知済みとして
    # 記録・保存する。全件をまとめて1通に送っていると、2000文字超で複数の
    # Discordリクエストに分割された際、途中のリクエストが失敗した時点で
    # それ以前に届いた分もまとめて未記録のままになり、次回実行で届いた分まで
    # 再送してしまっていた（2026-08-27のCodexレビューで指摘・修正）。
    if all_candidates:
        header = f"📅 {today:%Y-%m-%d} のスクリーニング結果（{len(all_candidates)}件）"
        discord_notify.send_discord_message(webhook_url, header)
        for candidate in all_candidates:
            discord_notify.send_discord_message(webhook_url, candidate.message)
            state["notified"][candidate.state_key] = True
            _prune_state(state, today)
            _save_state(state)

    if error_messages:
        discord_notify.send_discord_message(webhook_url, "\n\n".join(error_messages))

    return 1 if had_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
