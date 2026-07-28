"""スクリーニングの閾値・定数設定。

須田忠雄事務所ルールの8項目のうち、J-Quantsの数値データだけで判定できる
以下4項目をこのバージョンで実装している。

  1. ストップ高が出た                 -> rules.detect_stop_high
  2. 増収率が大幅 / 爆発的            -> rules.detect_sales_growth
  3. 四半期(本決算)が会社予想を上回った -> rules.detect_earnings_beat
  4. 株式分割があった                 -> rules.detect_stock_split

残り4項目（札幌・福岡・名古屋からの東証移籍、スタンダード/グロースから
プライムへの市場変更、新工場・新店舗の設置、上記に該当しない定性的な
「爆発的増収」の事例）は、市場区分の履歴管理やTDnet開示テキストの解析が
必要になるため未実装（README参照）。
"""

# 増収率のしきい値（前年同期比、対象期間の累計売上高で比較）
SALES_GROWTH_MAJOR_THRESHOLD = 0.20   # 「大幅に増えた」の下限 (+20%)
SALES_GROWTH_EXPLOSIVE_THRESHOLD = 0.50  # 「爆発的に伸びた」の下限 (+50%)

# 決算が会社予想を上回ったと判定する上振れ率のしきい値
EARNINGS_BEAT_THRESHOLD = 0.0  # 0 = 予想を1円でも上回れば対象。必要に応じて調整可

# 自己資本率のしきい値（EqAR は 0.6 = 60% のような比率で返ってくる）
EQUITY_RATIO_THRESHOLD = 0.6

# 経常利益が約N年で一定倍数以上に増えたかの判定
PROFIT_DOUBLING_YEARS = 4
PROFIT_DOUBLING_MULTIPLE = 2.0

# 業績予想の下方修正を検知するしきい値（前回予想からどれだけ下がったら「下方修正」とみなすか）
DOWNWARD_REVISION_THRESHOLD = 0.0  # 0 = 1円でも下がったら対象

# 「上場5年以内」の近似判定。J-Quantsには上場日そのものが無いため、
# 契約プランで取得できる株価履歴の最も古い日付を「推定初値観測日」として使う。
# 株価データがこの年数分の期間の開始日付近から始まっている場合は、それより前から
# 取引されていた可能性が高く「不明」と判定する（実際の上場日は分からない）。
LISTING_LOOKBACK_YEARS = 5
LISTING_DATE_BOUNDARY_TOLERANCE_DAYS = 14

# J-Quants Lightプラン（2026-07-28に契約）の制約
# https://jpx-jquants.com/ja/help/plan によれば日経225・信用取引・空売りデータ等は対象外だが、
# 本アプリが使う株価四本値・財務情報は取得可能。データ遡及期間は過去5年、遅延配信の制約はない。
JQUANTS_API_CALLS_PER_MINUTE = 60
JQUANTS_FREE_PLAN_DELAY_WEEKS = 0  # Lightプランは遅延なし

# ローカルキャッシュ保存先
CACHE_DIR = "data/cache"
