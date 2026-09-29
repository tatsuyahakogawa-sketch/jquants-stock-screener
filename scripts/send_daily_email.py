"""前営業日にDiscordへ送信した内容をまとめて、毎朝9:00 JST(平日)にメールで
再掲するバッチ。GitHub Actions(.github/workflows/daily_email_digest.yml)から
実行される想定（2026-09-01にユーザー指定）。

scripts/watch_and_notify.pyがDiscordへ送信するたびに
state["notified"][key] = {"sent_at": ..., "message": ...} として記録する
送信時刻(sent_at, JST)と送信本文(message)を読み、前営業日
（土日祝日を除く直前の営業日）に送信され、かつ対象銘柄・事象そのものの
日付も前営業日であるものだけを抽出してメールにまとめる（_collect_digest_messages
参照。watch_and_notify.py側のウォーターマークによるcatch-upで本来より古い
日付の内容が紛れ込むことがあるため、単純にsent_atの日付だけでは絞らない。
2026-09-29にユーザー指摘・修正）。
watch_and_notify.py自体の送信スケジュール（平日10:00・13:00・16:10 JST）は変更しない。

このスクリプトは読み取り専用で、data/notify_state.jsonを書き換えない
（GitHub Actions側もnotify-stateブランチへのpushを行わない。
.github/workflows/watch_and_notify.ymlが書き込む側、こちらは読むだけ）。

NOTIFY_EMAIL_TOは複数の宛先をカンマ区切りで指定できる（2026-09-01にユーザーが
社内の複数アドレスへの同報を指定したため）。

実行方法（ローカル確認用。通常はGitHub Actionsから実行される）:
    GMAIL_ADDRESS=... GMAIL_APP_PASSWORD=... NOTIFY_EMAIL_TO=a@example.com,b@example.com \
        python scripts/send_daily_email.py
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.jst import JST, today_jst
from src.market_calendar import is_market_holiday

logger = logging.getLogger(__name__)

STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "notify_state.json"

_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 587

# 本文中での並び順。ここに無いruleは末尾にまとめる。
_RULE_ORDER = [
    "stop_high",
    "stock_split",
    "stock_consolidation",
    "profit_growth_major",
    "ipo_approval",
    "ipo_listed",
]


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {"notified": {}}
    with STATE_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("notified", {})
    return data


def _parse_recipients(raw: str) -> list[str]:
    return [addr.strip() for addr in raw.split(",") if addr.strip()]


def _previous_business_day(today: dt.date) -> dt.date:
    day = today - dt.timedelta(days=1)
    while is_market_holiday(day):
        day -= dt.timedelta(days=1)
    return day


def _rule_sort_key(rule: str) -> int:
    try:
        return _RULE_ORDER.index(rule)
    except ValueError:
        return len(_RULE_ORDER)


def _event_date_from_key(key: str) -> dt.date | None:
    """通知済みキー(例: "stop_high|1234|2026-08-28"。TDnet由来のルールは
    末尾が"2026-08-28T15:30:00"のように時刻を含むことがある)から、対象と
    なった株価・開示そのものの日付を取り出す。

    これは「実際にDiscordへ送信した時刻」(sent_at)とは別物。
    watch_and_notify.py側のウォーターマークによるcatch-up（一時的な取得・
    送信失敗や長期休場明けの取りこぼしからの再送）が起きると、本来の
    日付より後にまとめて送信されることがあり、その場合sent_atの日付と
    この日付がずれる。
    """
    date_part = key.rsplit("|", 1)[-1][:10]
    try:
        return dt.date.fromisoformat(date_part)
    except ValueError:
        return None


def _collect_digest_messages(state: dict, target_day: dt.date) -> list[str]:
    """target_day（JST）分の内容として、target_day当日にDiscordへ実際に
    送信され、かつ対象銘柄・事象そのものの日付もtarget_dayであるメッセージ
    本文を、送信時刻の昇順・ルール種別順に並べて返す。

    sent_atの日付だけで絞ると、ウォーターマークによるcatch-up
    （_event_date_from_key docstring参照）で本来より古い日付の内容が
    target_day分の通知に紛れ込む。例えば土日を挟んで金曜分のストップ高が
    月曜に catch-up 送信された場合、sent_atは月曜になるが対象は金曜の
    データであり、月曜分のまとめメールに金曜の（既にDiscordで見ているはずの）
    情報が古いまま再掲されてしまう（2026-09-29にユーザー指摘・修正。
    catch-up自体はDiscord側の取りこぼし防止のために必要な挙動なので
    そのまま残し、日次まとめメールへの反映だけを対象日と一致する分に絞る）。

    watch_and_notify.pyがこの機能の追加より前に書き込んだ、値がTrueのままの
    古いエントリ（sent_at・messageを持たない）は対象外として無視する
    （実害は無い。対象となる日付はとうに過ぎているため）。
    """
    entries: list[tuple[dt.datetime, str, str]] = []
    for key, value in state["notified"].items():
        if not isinstance(value, dict):
            continue
        sent_at_str = value.get("sent_at")
        message = value.get("message")
        if not sent_at_str or not message:
            continue
        try:
            sent_at = dt.datetime.fromisoformat(sent_at_str)
        except ValueError:
            continue
        if sent_at.tzinfo is None:
            continue
        sent_at_jst = sent_at.astimezone(JST)
        if sent_at_jst.date() != target_day:
            continue
        if _event_date_from_key(key) != target_day:
            continue
        rule = key.split("|", 1)[0]
        entries.append((sent_at_jst, rule, message))
    entries.sort(key=lambda e: (_rule_sort_key(e[1]), e[0]))
    return [message for _, _, message in entries]


def _build_email_body(target_day: dt.date, messages: list[str]) -> str:
    lines = [f"{target_day:%Y-%m-%d}（前営業日）にDiscordへ通知した内容のまとめです。", ""]
    if not messages:
        lines.append("該当銘柄はありませんでした。")
    else:
        for message in messages:
            lines.append(message)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _send_email(
    smtp_user: str, smtp_password: str, to_addrs: list[str], subject: str, body: str
) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(body)
    with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(smtp_user, smtp_password)
        # to_addrsを明示的に渡す。省略した場合smtplibはTo/Cc/Bccヘッダを
        # 解析して宛先を決めるが、それに頼るよりヘッダ文字列の組み立てと
        # 実際の宛先リストを常に一致させておく方が確実。
        smtp.send_message(msg, to_addrs=to_addrs)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    today = today_jst()
    if is_market_holiday(today):
        logger.info("%s は休日のためスキップします。", today)
        return 0

    smtp_user = os.environ.get("GMAIL_ADDRESS")
    smtp_password = os.environ.get("GMAIL_APP_PASSWORD")
    to_addrs_raw = os.environ.get("NOTIFY_EMAIL_TO")
    if not smtp_user or not smtp_password or not to_addrs_raw:
        logger.error("GMAIL_ADDRESS / GMAIL_APP_PASSWORD / NOTIFY_EMAIL_TO が設定されていません。")
        return 1
    to_addrs = _parse_recipients(to_addrs_raw)
    if not to_addrs:
        logger.error("NOTIFY_EMAIL_TO から有効な宛先を抽出できませんでした: %r", to_addrs_raw)
        return 1

    state = _load_state()
    target_day = _previous_business_day(today)
    messages = _collect_digest_messages(state, target_day)

    subject = f"📈 株式スクリーニング日次まとめ（{target_day:%Y-%m-%d}分）"
    body = _build_email_body(target_day, messages)

    try:
        _send_email(smtp_user, smtp_password, to_addrs, subject, body)
    except Exception as e:
        # smtplibの例外はGmailアドレスを含みうるが、パスワード自体は
        # 含まないため、discord_notify.pyほど神経質に扱う必要はない。
        # それでも念のため例外の型名のみをログ・標準エラーに出す
        # （scripts/discord_notify.pyと同じ方針に合わせる）。
        logger.exception("メール送信に失敗しました")
        print(f"⚠️ メール送信に失敗しました（{type(e).__name__}）", file=sys.stderr)
        return 1

    logger.info("%s 分のまとめメールを送信しました（%d件）", target_day, len(messages))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
