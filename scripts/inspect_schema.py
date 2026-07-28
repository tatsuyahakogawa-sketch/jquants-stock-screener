"""J-Quants の実際のレスポンス列名を確認するための検証スクリプト。

rules.py の列名定数（QUOTES_*, STMT_*）は記憶ベースで書いたものなので、
本番投入前に必ずこれを実行し、実際の列名と食い違いがないか確認すること。

使い方:
    python scripts/inspect_schema.py
"""
from __future__ import annotations

import datetime as dt

from dotenv import load_dotenv

from src import endpoints
from src.config import JQUANTS_FREE_PLAN_DELAY_WEEKS
from src.jquants_client import JQuantsClient

load_dotenv()


def main() -> None:
    client = JQuantsClient()
    # Freeプランで確実に取れる日付（直近の遅延期間より前）を使う
    sample_date = dt.date.today() - dt.timedelta(weeks=JQUANTS_FREE_PLAN_DELAY_WEEKS + 2)
    # 土日祝日を避けるため、数日分試す
    for offset in range(7):
        d = sample_date - dt.timedelta(days=offset)
        quotes = endpoints.get_daily_quotes_by_date(client, d)
        if not quotes.empty:
            print(f"[daily_quotes] date={d} columns=\n{list(quotes.columns)}\n")
            print(quotes.head(3))
            break
    else:
        print("daily_quotes: サンプル日で1件もデータが取得できませんでした。")

    for offset in range(30):
        d = sample_date - dt.timedelta(days=offset)
        statements = endpoints.get_statements_by_date(client, d)
        if not statements.empty:
            print(f"\n[statements] date={d} columns=\n{list(statements.columns)}\n")
            print(statements.head(3))
            break
    else:
        print("statements: サンプル日で1件もデータが取得できませんでした。")


if __name__ == "__main__":
    main()
