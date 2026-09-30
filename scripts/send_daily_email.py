"""当日にDiscordへ送信した内容をまとめて、watch_and_notify.pyの当日最終
実行枠(16:10 JST)完了後に1日1回メールで再掲するバッチ。GitHub Actions
(.github/workflows/daily_email_digest.yml)から実行される想定
（2026-09-01にユーザー指定。以前は毎朝9:00 JSTに前営業日分をまとめる
仕様だったが、(1) GitHub Actionsのschedule実行はしばしば遅延し、
2026-09-30に9:00→11:30 JSTまで遅延した実例が発生、(2) 定刻通りに動いても
そもそも「前日分」しか載らず鮮度が低い、という2つの問題をユーザーに指摘され、
2026-09-30に「当日分を1回で」に変更した）。

起動タイミングは当初「16:30 JST固定cron」（watch_and_notify.pyの最終実行枠
16:10 JSTの後に猶予を見込んだ時刻）で実装したが、Codexレビューで固定時刻に
頼る設計そのものに以下4点の欠陥を指摘され、日時の推測をやめて
notify_state.json自身に刻んだ実行実績のマーカーで判定する方式に作り直した
（2026-09-30）:
  1. watch_and_notify.py側のschedule実行自体がGitHub Actionsのschedule
     遅延で16:30より後にずれ込んだ場合（10:00・13:00枠・旧9:00メール枠の
     いずれも数時間規模の遅延実績があり、16:10枠だけが遅延しない保証は
     無い）、まとめメールがその日の状態更新前の古い状態を読んでしまい、
     target_dayが翌日に変わるため当日分の一部が永久にメールから漏れる。
  2. 1.の対策として.github/workflows/daily_email_digest.ymlにwatch-and-
     notifyワークフロー自体の完了イベント(workflow_run)を追加すると、
     今度は16:30固定cronとworkflow_runの両方から同じ日に重複送信されうる
     （このスクリプトは読み取り専用でnotify-stateへの書き込みを行わない
     設計のため、送信済みマーカーを自分で書けず、単純な時刻ガードだけでは
     防げない）。
  3. workflow_runは平日中に10:00/13:00/16:10 JSTの3回発火しうるため、
     「時刻が15:00 JST以降か」だけで「最終実行枠(16:10)か」を判定すると、
     GitHub Actionsのschedule遅延で13:00枠が15:00以降にずれ込んだ場合に
     それを最終実行枠と誤認し、16:10枠がまだ完了していない時点で送って
     しまう。
  4. workflow_runの`completed`はwatch-and-notify側が失敗・中断した場合にも
     発火するため、取得自体ができなかった日に「該当銘柄はありませんでした」
     という（実際には「確認できていない」だけの）誤った空メールを送ってしまう。

これらは全て「いつ実行されたか」という時刻の推測に頼っていたことが原因のため、
代わりに「watch_and_notify.py自身が、当日の最終実行枠として4情報源全ての
取得を試みた直後にnotify_state.jsonへ書き込むマーカー」
(state["last_run_schedule"]・state["last_run_date"]。watch_and_notify.py
_TRIGGER_SCHEDULE参照)を判定基準にした。このマーカーはGitHub Actionsの
`github.event.schedule`（実際に発火したcron式そのもの。同じワークフローに
複数のscheduleがあっても区別できる）をそのまま使うため、実行が何時にずれ
込んでも「どのcronで発火したか」を誤らない。1.の対策としてこのワークフロー
自体はwatch-and-notifyの完了イベント(workflow_run)でのみ起動し、固定cronは
持たない（2.の重複を経路自体を1つに絞ることで防ぐ）。3.は_TARGET_TRIGGERS
に最終実行枠のcron文字列を明示することで、時刻ではなくマーカーの値そのもの
で判定するため誤らない。4.はマーカーがwatch_and_notify.py側の処理が実際に
そこまで到達した場合にしか更新されないため、失敗して何も取得できなかった
日はマーカーが前日以前のままとなり、この関数が「まだ完了していない」と
正しく判定してメール送信自体をスキップする（詳細はwatch_and_notify.py
main()のstate["last_run_schedule"]周辺のコメント参照）。

上記の設計にも、さらに次の5点がCodexレビューで指摘された（2026-09-30、
2巡目）:
  5. workflow_run一本に絞っても、10:00・13:00枠の完了イベントに起因する
     起動がGitHub Actionsのジョブ開始待ち等でたまたま16:10枠のpush後に
     状態を読んでしまうと「最終実行枠が完了している」と誤って判定し、
     16:10枠自身の完了イベントに起因する起動と合わせて同じ内容を
     重複送信しうる。
  6. 個々の情報源が失敗していても4情報源全ての取得を「試みた」時点で
     マーカーを無条件に設定していたため、例えばJQuantsClientの構築に
     失敗しつつTDnet/IPOは正常に0件だった日に、実際には半分の情報源しか
     確認できていないのに「該当銘柄はありませんでした」と断定的に伝えて
     しまう。
  7. stock_split・stock_consolidation（TDnet由来）の1日遅れ許容が前営業日
     「全体」を対象にしていたため、前営業日の朝に本来検出できていたはず
     の開示がTDnetミラーAPIの障害等で取りこぼされ今日catch-upで初めて
     検出された場合も、16:10枠より後に出た正常な開示と区別できず
     まとめメールに含めてしまう（本来防ぎたかった古いcatch-upの再発）。
  8. workflow_dispatch（手動実行）時、FORCE_SEND判定より前に休日チェックを
     行っていたため、休日に動作確認・手動リカバリのつもりで実行しても
     何も送らず終了してしまう。
  9. watch_and_notify.pyの16:10枠実行がJST深夜を跨ぐほど遅延すると、この
     スクリプト自身のtoday_jst()とマーカーのlast_run_dateが一致しなくなり、
     1.と同種のデータ欠落が日付境界でだけ再発する。

対応: 5.はwatch_and_notify.py側にGitHub Actionsの実行ID
(state["last_run_id"]。環境変数GITHUB_RUN_IDはActionsが自動的に渡す)も
記録させ、このスクリプトを起動したworkflow_runイベントの元実行ID
(TRIGGERING_RUN_ID環境変数＝github.event.workflow_run.id)と完全一致する
場合のみ送信する(_resolve_todays_final_notifier_run参照)。6.は
watch_and_notify.py側にstate["last_run_had_error"]も記録させ、0件かつ
had_errorがTrueの日は「該当銘柄はありませんでした」ではなく「一部の
情報源でチェックが完了できなかった可能性があります」という文言に変える
(_build_email_body参照)。7.はTDnet由来のruleに限り、キーが持つ実際の
開示時刻(_event_datetime_from_key)を見て、前営業日の最終実行枠(16:10 JST)
以降だけを許容するよう絞った(_is_within_allowed_lag参照。財務情報のみの
profit_growth_majorは開示"時刻"という概念自体が無いため対象外で、従来
通り前営業日全体を許容する)。8.はFORCE_SENDの判定を休日チェックより前に
移動した。9.はtarget_dayを「このスクリプト自身のtoday_jst()」ではなく
「マーカーに刻まれたlast_run_date（watch_and_notify.pyが実際に処理した
対象日）」そのものにし、日付の一致判定も前後1日までのずれを許容するよう
緩めた(_resolve_todays_final_notifier_run参照)。

scripts/watch_and_notify.pyがDiscordへ送信するたびに
state["notified"][key] = {"sent_at": ..., "message": ...} として記録する
送信時刻(sent_at, JST)と送信本文(message)を読み、当日に送信され、かつ
対象銘柄・事象そのものの日付も当日であるものだけを抽出してメールにまとめる
（_collect_digest_messages参照。watch_and_notify.py側のウォーターマークに
よるcatch-upで本来より古い日付の内容が紛れ込むことがあるため、単純に
sent_atの日付だけでは絞らない。2026-09-29にユーザー指摘・修正）。
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


# watch_and_notify.pyの最終実行枠(16:10 JST)のcron式（.github/workflows/
# watch_and_notify.yml参照）。schedule実行時、watch_and_notify.py側は
# github.event.scheduleをそのままstate["last_run_schedule"]に書き込む
# （TRIGGER_SCHEDULE環境変数）ため、この文字列と完全一致するかどうかで
# 「当日の最終実行枠が完了したか」を判定できる。
#
# 以前は"manual"（watch_and_notify.pyのworkflow_dispatchによる手動実行）
# もここに含めていたが、平日の最終実行枠(16:10 JST)より前に
# watch_and_notify.pyを手動実行すると（動作確認・当日中の即時確認等で
# 実際に何度も行っている）、その完了イベントで自動的にまとめメールが
# 送信されてしまい、その後の本来の16:10枠の完了でも改めて送信されるため
# 同じ日に重複送信されうるとCodexレビューで指摘された。このスクリプト側
# には送信済みかどうかの永続的な記録が無く（読み取り専用の設計）、
# 「今日まだ送っていないか」を別途確認する手段が無いため、自動送信の
# 対象は最終実行枠のcronだけに絞り、手動実行を契機にした自動送信は
# 行わない（2026-09-30のCodexレビューで指摘・修正）。手動実行後に
# 明示的にまとめメールを送りたい場合は、このワークフロー自体を
# workflow_dispatch（FORCE_SEND=true）で手動実行する。
_TARGET_TRIGGERS = {"10 7 * * 1-5"}


def _resolve_todays_final_notifier_run(
    state: dict, today: dt.date, triggering_run_id: str | None
) -> dt.date | None:
    """watch_and_notify.pyの最終実行枠(16:10 JST)相当の実行が完了していれば
    その対象日(target_day)を、完了していなければNoneを返す
    （モジュールdocstring参照。時刻の推測に頼らない）。

    - 日付は厳密な完全一致ではなく前後1日までのずれを許容する。実行が
      JST深夜を跨いでずれ込んだ場合（例: watch_and_notify.pyの16:10枠が
      深夜近くまで遅延し、日付をまたいでから完了する等）、このスクリプト
      自身のtoday_jst()とマーカーのlast_run_dateが厳密には一致しなくなる
      ことがあり、完全一致だけを条件にすると16:30固定cronの時と同種の
      データ欠落がJST日付境界でだけ再発してしまう（2026-09-30のCodex
      レビューで指摘・修正）。target_dayはtoday（このスクリプト自身の
      実行日）ではなく、マーカーに刻まれたlast_run_date（watch_and_notify.py
      が実際に処理した対象日）をそのまま返す。
    - triggering_run_idを渡した場合（workflow_run経由の起動。
      TRIGGERING_RUN_ID環境変数＝github.event.workflow_run.id）、
      state["last_run_id"]と完全一致することも要求する。これが無いと、
      10:00・13:00枠の完了イベントに起因する起動が、GitHub Actionsの
      ジョブ開始待ち等でたまたま16:10枠のpush後に状態を読んでしまった
      場合に「最終実行枠が完了している」と誤って判定し、16:10枠自身の
      完了イベントに起因する起動と合わせて同じ内容を重複送信してしまう
      （2026-09-30のCodexレビューで指摘・修正）。Noneの場合はこの相関を
      行わない（workflow_dispatch等、workflow_run以外からの起動）。
    """
    last_run_date_str = state.get("last_run_date")
    if not last_run_date_str:
        return None
    try:
        last_run_date = dt.date.fromisoformat(last_run_date_str)
    except ValueError:
        return None
    if abs((today - last_run_date).days) > 1:
        return None
    if state.get("last_run_schedule") not in _TARGET_TRIGGERS:
        return None
    if triggering_run_id is not None and str(state.get("last_run_id")) != str(triggering_run_id):
        return None
    return last_run_date


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
    """dayの直前の営業日を返す。

    2026-09-30以降、まとめメールの対象日(target_day)自体はtoday(当日)を
    直接使うため、この関数はもう対象日の算出には使わない。_NEXT_DAY_LAG_RULES
    に該当するruleが「target_dayの前営業日」に検出された分も許容するための、
    _collect_digest_messages内部でのみ引き続き使う。
    """
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


def _event_datetime_from_key(key: str) -> dt.datetime | None:
    """_event_date_from_keyの時刻付き版。時刻情報が無いキー（J-Quants由来の
    rule等、元々日付単位の情報しか無い）は00:00:00として扱う。TDnet由来の
    ruleの1日遅れ許容判定(_is_within_allowed_lag参照)で、実際の開示時刻を
    見るために使う。
    """
    date_part = key.rsplit("|", 1)[-1]
    try:
        if "T" in date_part:
            return dt.datetime.fromisoformat(date_part)
        return dt.datetime.combine(dt.date.fromisoformat(date_part), dt.time())
    except ValueError:
        return None


# ipo_listed（上場前日のお知らせ）だけは、キーに埋め込まれる日付が
# 「対象の事象が起きた日」ではなく「翌営業日の上場予定日」（送信日の1日後）
# になる設計のため（scripts/watch_and_notify.py _ipo_candidates参照）、
# 他のruleと同じ基準で_event_date_from_keyと比較すると常に不一致になり、
# 前日に届いたはずのリマインダーが翌朝のまとめメールから毎回漏れてしまう
# （2026-09-29のCodexレビューで指摘・修正）。この情報源はそもそも意図的に
# watermarkによるcatch-upを行わない（detect_listings_tomorrowのdocstring
# 参照）ため、catch-upによる古い日付混入の心配が無く、sent_atの日付だけで
# 絞れば十分。
_NO_CATCH_UP_RULES = {"ipo_listed"}

# profit_growth_major（経常利益急増）は財務情報(/fins/summary)が対象で、
# CLAUDE.mdの通り18:00と24:30(=翌0:30)に更新される。watch_and_notify.pyの
# 実行は平日10:00/13:00/16:10 JSTで、いずれも18:00より前のため、18:00〜
# 24:30の間に確定した分は当日中には検出できず、翌営業日の10:00の実行で
# 初めて検出・送信される。この場合sent_atは翌営業日、キーの日付（開示日）は
# 前営業日になり、他のruleと同じ「target_dayに完全一致」の条件を課すと、
# 一時的な障害由来のcatch-upと区別できずこの正常な1日遅れの通知が
# どのまとめメールにも一度も載らなくなってしまう（2026-09-29のCodex
# レビューで指摘・修正）。このruleに限り、キーの日付がtarget_day自体か
# その前営業日のどちらでも許容する。
#
# stock_split・stock_consolidation（TDnet由来）も同じ構造的リスクを持つ。
# watch_and_notify.pyの最終実行は16:10 JSTのため、それより後（例:
# 16:30）に出た開示は当日中には検出できず、翌営業日の10:00の実行で初めて
# 検出・送信される（tests/test_watch_and_notify.pyのTestPruneState等で
# 16:30の開示時刻が実際にモデル化されている＝起こりうる想定として既に
# テスト済みの状況）。profit_growth_majorだけを許容してこれらを対象外の
#ままにすると、同じ理由で正常な1日遅れの分がまとめメールから漏れて
# しまうため、同じ緩和を適用する（2026-09-29のCodexレビューで指摘・修正）。
#
# ただしTDnet由来のruleは、前営業日「全体」を許容すると本来防ぎたかった
# 古いcatch-upまで再び許してしまう。例えば前営業日の朝10:00の開示が
# TDnetミラーAPIの障害で取りこぼされ、今日になって初めてcatch-upで
# 検出された場合、それは16:10枠より後に出た正常な開示ではなく一時的な
# 障害由来の取りこぼしそのものであり、まとめメールに含めるべきではない
# （2026-09-30のCodexレビューで指摘・修正）。TDnet由来のruleはキーに
# 実際の開示時刻を持つ(_event_datetime_from_key参照)ため、前営業日の
# 最終実行枠(16:10 JST)以降に限って許容する。profit_growth_majorは
# 財務情報が元々日付単位の情報しか無く、開示"時刻"という概念自体が
# 無いため、この時刻による絞り込みは適用できず従来通り前営業日全体を
# 許容する。
_TDNET_NEXT_DAY_LAG_RULES = {"stock_split", "stock_consolidation"}
_DATE_ONLY_NEXT_DAY_LAG_RULES = {"profit_growth_major"}
_NEXT_DAY_LAG_RULES = _TDNET_NEXT_DAY_LAG_RULES | _DATE_ONLY_NEXT_DAY_LAG_RULES
_FINAL_NOTIFIER_SLOT_TIME = dt.time(16, 10)


def _is_within_allowed_lag(key: str, rule: str, target_day: dt.date) -> bool:
    """このkeyの対象日付・時刻が、target_day分のまとめメールに含めてよい
    範囲内かを判定する（_NEXT_DAY_LAG_RULES群の詳細はその定義部分の
    コメント参照）。
    """
    event_date = _event_date_from_key(key)
    if event_date == target_day:
        return True
    if rule not in _NEXT_DAY_LAG_RULES:
        return False
    if event_date != _previous_business_day(target_day):
        return False
    if rule in _TDNET_NEXT_DAY_LAG_RULES:
        event_dt = _event_datetime_from_key(key)
        return event_dt is not None and event_dt.time() >= _FINAL_NOTIFIER_SLOT_TIME
    return True  # _DATE_ONLY_NEXT_DAY_LAG_RULES


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

    ただし全ruleに単純な完全一致を課すと、一時的な障害由来のcatch-upとは
    別に、データ源の構造上ほぼ毎回1日遅れが起きるruleまで拾えなくなる
    （_NEXT_DAY_LAG_RULES/_NO_CATCH_UP_RULES参照。2026-09-29のCodexレビューで
    指摘・修正）。

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
        rule = key.split("|", 1)[0]
        if rule not in _NO_CATCH_UP_RULES and not _is_within_allowed_lag(key, rule, target_day):
            continue
        entries.append((sent_at_jst, rule, message))
    entries.sort(key=lambda e: (_rule_sort_key(e[1]), e[0]))
    return [message for _, _, message in entries]


def _build_email_body(
    target_day: dt.date, messages: list[str], had_error: bool = False, *, is_today: bool = True
) -> str:
    # target_dayは常に「今日」とは限らない。JST深夜を跨ぐ遅延やFORCE_SEND
    # による手動リカバリでは、target_dayが実行日より前の日になりうる
    # （_resolve_todays_final_notifier_run docstring参照）。それでも
    # 「（本日）」と表示すると、件名・本文の対象日は過去の日付なのに
    # 本文だけ「今日のことだ」と読めてしまい紛らわしいため、target_dayが
    # 実際に今日である場合だけこの表記を付ける（2026-09-30のCodexレビューで
    # 指摘・修正）。
    label = "（本日）" if is_today else ""
    lines = [f"{target_day:%Y-%m-%d}{label}にDiscordへ通知した内容のまとめです。", ""]
    # watch_and_notify.py側でいずれかの情報源の取得に失敗した日は、0件の
    # ときだけでなく、他の情報源が検出・送信できた分がある場合でも
    # その旨を明記する。「一部の情報源が失敗しつつ別の情報源は成功した」
    # 日に、成功分だけを見せて何も欠けていないかのような完全な日次
    # まとめだと誤解させてはならない（CLAUDE.md「データの正確性を最優先」
    # 参照。2026-09-30のCodexレビューで指摘・修正。以前は messages が
    # 空の場合にしかこの注記を出していなかった）。
    if had_error:
        lines.append(
            "⚠️ 一部の情報源でチェックが完了できなかった可能性があります。"
            "Discordに送信されたエラー通知をご確認ください。"
        )
        lines.append("")
    if not messages:
        if not had_error:
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


def _parse_iso_date(value: object) -> dt.date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    today = today_jst()

    # workflow_dispatch（手動実行）の場合はマーカーガード・休日ガードの
    # 両方を無効化し、いつでも強制送信できるようにする（.github/workflows/
    # daily_email_digest.yml参照。動作確認や手動リカバリ用）。
    force_send = os.environ.get("FORCE_SEND", "").lower() == "true"
    state = _load_state()

    if force_send:
        # target_dayにtodayを固定していたため、翌日以降にworkflow_dispatch
        # で前日分の手動リカバリを試みても「今日」分（まだ何も無い）を
        # 対象にしてしまい、本来確認したかった前日分ではなく空の「今日分」
        # を送ってしまっていた。last_run_dateがあればそれを使う
        # （2026-09-30のCodexレビューで指摘・修正）。
        target_day = _parse_iso_date(state.get("last_run_date")) or today
    else:
        # TRIGGERING_RUN_ID: workflow_run経由の起動時、自身を起動した
        # watch-and-notifyの実行ID(github.event.workflow_run.id)。
        # state["last_run_id"]と突き合わせることで、実行順序の入れ替わりに
        # よる重複送信を防ぐ（_resolve_todays_final_notifier_run参照）。
        triggering_run_id = os.environ.get("TRIGGERING_RUN_ID") or None
        resolved_target_day = _resolve_todays_final_notifier_run(state, today, triggering_run_id)
        if resolved_target_day is None:
            logger.info(
                "watch_and_notifyの最終実行枠(16:10 JST)が当日分としてまだ完了して"
                "いない、または既にこの実行分を送信済みのためスキップします。"
            )
            return 0
        target_day = resolved_target_day

        # 休日チェックはtoday_jst()ではなく解決済みのtarget_dayに対して
        # 行う。通常watch_and_notify.py自体が休日はスキップするため
        # マーカーが更新されず、上のNone判定で既にスキップされているはず
        # だが、念のための防御。today_jst()で判定すると、例えば金曜深夜
        # 近くまで遅延した最終実行枠の完了をきっかけに土曜になってから
        # このスクリプトが起動された場合、_resolve_todays_final_notifier_run
        # 側の前後1日許容は正しく機能していても、この休日チェック自体が
        # 誤って土曜日（today）を見てスキップしてしまう
        # （2026-09-30のCodexレビューで指摘・修正）。
        if is_market_holiday(target_day):
            logger.info("%s は休日のためスキップします。", target_day)
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

    messages = _collect_digest_messages(state, target_day)
    had_error = bool(state.get("last_run_had_error"))

    subject = f"📈 株式スクリーニング日次まとめ（{target_day:%Y-%m-%d}分）"
    body = _build_email_body(target_day, messages, had_error, is_today=(target_day == today))

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
