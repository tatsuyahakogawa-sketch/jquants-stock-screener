"""定期監視スクリプト(scripts/watch_and_notify.py)向けの営業日判定。

土日・日本の祝日（国民の祝日・振替休日等）は市場が休みで新しい開示・株価データが
出ないため、この日は監視をスキップしてよい。祝日判定はjpholidayパッケージ
（内閣府の祝日データに基づく）に委ねる。
"""
from __future__ import annotations

import datetime as dt

import jpholiday


def is_market_holiday(date: dt.date) -> bool:
    """土曜・日曜、または日本の祝日ならTrue。"""
    return date.weekday() >= 5 or jpholiday.is_holiday(date)
