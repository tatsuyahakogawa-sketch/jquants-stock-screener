"""JST(日本時間)基準の「今日」を返すユーティリティ。

Streamlit CloudなどサーバーがUTCで動く環境では、`datetime.date.today()`は
JSTより最大9時間遅れる（JST 0:00〜8:59の間はUTCではまだ前日のまま）。
J-Quants等の日本市場向けAPIへの日付範囲リクエスト（特に契約プランの
取得可能期間の起点となる日付）がこの1日のズレで境界を超えてしまうことが
実機で確認されている（2026-08-19、Lightプランの5年分取得可能期間の
起点をUTC基準で計算すると1日早くなり、J-Quants側に400エラーで拒否された）。
日本市場のデータを扱う日付範囲計算には、必ずこちらを使う。
"""
from __future__ import annotations

import datetime as dt

JST = dt.timezone(dt.timedelta(hours=9))


def today_jst() -> dt.date:
    return dt.datetime.now(JST).date()
