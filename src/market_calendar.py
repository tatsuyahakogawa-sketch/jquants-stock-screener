"""定期監視スクリプト(scripts/watch_and_notify.py)向けの営業日判定。

土日・日本の祝日（国民の祝日・振替休日等）は市場が休みで新しい開示・株価データが
出ないため、この日は監視をスキップしてよい。祝日判定はjpholidayパッケージ
（内閣府の祝日データに基づく）に委ねる。

東証は12/31・1/2・1/3（年末年始休場。1/1は国民の祝日として既にjpholidayが
判定する）も、国民の祝日とは無関係に休場する。これらがjpholidayの
祝日判定に含まれないため、平日に当たる年（例: 2026年1月2日は金曜日）に
チェックを省略できず、無駄なAPI呼び出しや実行のたびの「該当なし」通知が
発生する（2026-08-28の8巡目のCodexレビューで指摘・修正）。
"""
from __future__ import annotations

import datetime as dt

import jpholiday

_YEAR_END_NEW_YEAR_CLOSURE_MONTH_DAYS = {(12, 31), (1, 1), (1, 2), (1, 3)}


def is_market_holiday(date: dt.date) -> bool:
    """土曜・日曜、日本の祝日、または東証の年末年始休場(12/31・1/1〜1/3)ならTrue。"""
    if date.weekday() >= 5:
        return True
    if (date.month, date.day) in _YEAR_END_NEW_YEAR_CLOSURE_MONTH_DAYS:
        return True
    return jpholiday.is_holiday(date)
