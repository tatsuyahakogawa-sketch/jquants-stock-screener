# CLAUDE.md

このファイルはClaude Codeがこのプロジェクトで作業する際のガイドです。

## プロジェクト概要

株式投資向けの銘柄スクリーニングアプリを開発するプロジェクト。
ユーザーが条件（PER・PBR・配当利回り・移動平均など）を入力すると、
その条件に合致する銘柄を検索・一覧表示する機能を想定している。

このうち「須田忠雄事務所ルール」による8項目スクリーニング（ストップ高、
売上高の大幅/爆発的増加、本決算が会社予想を上回った、株式分割、
東証移籍、プライム市場への市場変更、新工場・新店舗の設置）を
`app.py` / `src/` 以下に実装中。詳細は README.md を参照。

## 想定データソース: J-Quants API

日本取引所グループ（JPX）が個人投資家向けに提供する金融データAPI。
機関投資家向けと同等の株価・財務データを取得できる。

### 提供データ
- 株価四本値・出来高（ヒストリカル）
- 財務情報・決算情報
- 上場銘柄マスターデータ、取引カレンダー
- 業種別空売り比率、指数四本値
- （分足・Tickデータも提供開始済み）

### 料金プラン（月額・税込）
| プラン | 月額料金 | データ遡及期間 | 備考 |
|---|---|---|---|
| Free | ¥0 | 直近12週間を除く過去2年分（12週間遅延配信） | CSV取得不可、APIコール制限5件/分 |
| Light | ¥1,650 | 過去5年（遅延なし） | 日経225・信用取引・空売りデータ等は対象外、APIコール制限60件/分 |
| Standard | ¥3,300 | 過去10年 | APIコール制限120件/分 |
| Premium | ¥16,500 | 過去20年（2008年5月7日〜） | 財務情報は随時更新、先物・オプション等も対象、APIコール制限500件/分 |

現在 **Lightプランを契約中**（2026-07-28に無料プランからアップグレード。理由: 直近12週間の遅延配信ではリアルタイムに近い運用ができないため）。

- プラン変更: 上位への変更は回数制限なし。下位への変更（ダウングレード）は月1回まで
- 解約: いつでも可能。退会するとユーザー情報は全サービスから削除される

### 更新頻度（主なもの）
- 株価四本値: 営業日の大引け後、当日分を更新
- 財務情報: 18:00と24:30に更新（Premiumプランは随時更新）
- 銘柄情報: 翌営業日17:30以降に取得可能

### データ対象範囲の制約（東証系のみ、地方取引所は対象外）

J-QuantsはJPX（東証グループ）提供のため、カバー範囲は**東証(TSE)とTOKYO PRO Market**のみ。
福岡証券取引所（福証）・札幌証券取引所（札証）・名古屋証券取引所の独自市場
（Q-Board、アンビシャス等）は運営会社が異なり、対象外。

該当銘柄は`/equities/master`から消え、`/equities/bars/daily`も全期間null
になるため、時価総額・PER・PBR・配当利回り・株価が計算不能になる（実行表・
企業詳細では該当欄が空欄のまま）。株式分割等と違い、これは恒久的な制約で
自作パイプライン側では解決できない。

2026-08-06に9388（パパネッツ）で実機確認: TOKYO PRO Marketを2025-03-20に
上場廃止し、翌3/21に福証Q-Boardへスライド上場（公開価格700円、初値830円）。
移籍後はJ-Quants側で完全に空欄になったが、実際は現役で取引されている
（yfinanceでは`9388.T`ではなく`9388.F`サフィックスで現在値が取得できることを確認）。

2026-08-13に判明: TDnet開示には`markets_string`列（東/福/名/札）があり、
J-Quantsのマスタとは無関係に地方単独上場企業を特定できる。これを使って
地方単独上場企業の新規上場・東証関連イベント・大型イベントを検出する
「地方株」ページを`src/regional_stocks.py`として実装済み（README参照）。
上記の福証`.F`サフィックスで取得できるのは株価（現在値）のみで、発行済
株式数が無いため時価総額は算出できない。名証・札証はyfinanceでも実機
確認の上、株価自体が取得不可だった。

### 利用開始手順
1. J-Quantsサイトでアカウント作成・サインイン
2. ダッシュボードの「設定 > APIキー」からAPIキーを発行
3. リクエストヘッダーに `x-api-key: <APIキー>` を付与して呼び出す

  ※ 2026-07-28に実機で確認済み。旧V1方式（メールアドレス/パスワード→
  refreshToken→idToken、`Authorization: Bearer`）で実装していたが、
  実際には2026年1月のV2移行で旧エンドポイントが410 Goneで廃止されており、
  上記のAPIキー方式（`x-api-key`ヘッダー、有効期限なし）が正となることを確認した。
  `src/jquants_client.py` は現在この方式で実装済み。
  詳細: https://jpx-jquants.com/ja/spec/migration-v1-v2
  （エンドポイントパス・レスポンス列名もV2で変更されている。例:
  `/listed/info`→`/equities/master`, `/prices/daily_quotes`→`/equities/bars/daily`,
  `/fins/statements`→`/fins/summary`、`Close`→`C`, `AdjustmentFactor`→`AdjFactor` 等）

### 参考リンク
- 公式サイト: https://jpx-jquants.com/ja
- APIリファレンス（データ仕様）: https://jpx-jquants.com/ja/spec/data-spec
- サービス概要: https://jpx-jquants.com/ja/help/about
- プラン詳細: https://jpx-jquants.com/ja/help/plan

料金・仕様はサービス側の変更が入りうるため、実装前に上記公式ページで最新情報を再確認すること。

## 技術スタック

Python + Streamlit に決定（2026-07-28）。個人利用のダッシュボードとして
最速で構築でき、J-Quants呼び出し・スクリーニング処理もPythonで一本化できるため。

- `app.py`: Streamlit UI。単一ページ構成（2026-08-19に`pages/`のマルチページ構成から統合）。「絞り込み条件」内の「地方単独上場企業のみを検索する」チェックボックスで、通常のスクリーニング（絞り込み条件選択 → スクリーニング実行 → 銘柄集計テーブル/CSV）と地方株スキャン結果（下記`src/regional_stocks.py`のUI）を切り替える（2026-08-19にページ切り替えのst.radioから変更。地方株はPBR等の財務条件を持たないため、切り替えではなく条件の一つとして選べる方が自然というフィードバックのため）。地方株モードではJ-Quantsベースの財務条件カード（PBR等）は非表示になる代わりに、TDnet決算短信ベースの「💰 財務条件で絞り込む」（売上高増加・自己資本比率等、下記`src/tdnet_xbrl.py`参照）が表示される
- `src/jquants_client.py`: J-Quants認証・レート制限（429時は65秒待って自動リトライ）・ページネーション
- `src/endpoints.py`: 日付単位バルク取得 + ローカルキャッシュ（`data/cache/`, parquet）
- `src/cache.py`: 上記ローカルキャッシュの実体。`SUPABASE_URL`/`SUPABASE_KEY`設定時はSupabase（PostgREST）にも保存し、Streamlit Cloudの再デプロイでローカルキャッシュが消えても再利用できるようにする（任意設定、未設定でも動作、README参照）
- `src/tdnet_client.py`: TDnet開示情報の非公式ミラーAPIクライアント（新工場・新店舗、東証移籍検出、実行表の「最近のトピック」欄用）
- `src/edinet_client.py`: EDINET(金融庁)から有価証券報告書の事業概要・大株主・潜在株式・事業等のリスク・経営方針をテキストブロック単位で取得（`EDINET_API_KEY`必須、README参照）。要約はせず開示テキストをそのまま使う
- `src/rules.py`: 各条件の判定ロジック（J-Quants数値データ、市場区分履歴、TDnetタイトル検出）
- `src/pipeline.py`: 取得〜判定〜銘柄単位集計(build_summary)〜市場データ付与(enrich_with_market_data)のオーケストレーション。時価総額/PER/PBR/配当利回り計算(compute_market_metrics)と上場日近似(estimate_listing_date)はexcel_export.pyと共用
- `src/excel_export.py`: 銘柄コード→Excel自動生成。企業詳細・実行表とも、元のExcel（個人情報・実データを除いた汎用テンプレート化: `templates/company_detail_template.xlsx`, `templates/execution_table_template.xlsx`）のレイアウト・書式・数式をそのまま使い値だけを埋める方式。定性コメント欄はtdnet_client/edinet_clientの実データで埋め、市況解釈が必要な項目（価格が上下した理由）は空欄のまま
- `scripts/inspect_schema.py`: J-QuantsレスポンスのカラムをAPI実物で確認する検証用スクリプト
- `src/regional_stocks.py`: 「地方株」モード用。地方取引所（札幌・福岡・名古屋）単独上場企業をTDnet開示の`markets_string`列（東/福/名/札）で特定し、新規上場・東証関連イベント・M&A等大型イベントを検出する。J-Quantsは地方取引所単独上場企業を対象としないため使用しない。株価（現在値のみ、時価総額は算出不可）は福証単独上場企業のみyfinanceの`.F`サフィックスで取得可（名証・札証・新規英数字コード銘柄は取得不可）。前回スキャン済み日付をキャッシュに保存し、次回はその翌日からだけ追加取得する。`fetch_regional_statements()`で決算短信からsrc/tdnet_xbrl.py経由の財務データを蓄積し、`screen_regional()`でsrc/rules.pyの各`detect_*`（売上高増加・自己資本比率等、株価不要の条件のみ）をそのまま適用する（`REGIONAL_STATEMENT_RULES`/`REGIONAL_TITLE_RULES`参照。PBR・ストップ高は株価データが必要なため未対応）
- `src/tdnet_xbrl.py`: TDnet決算短信の「サマリー情報」XBRL(`url_xbrl`)を、J-Quants(`/fins/summary`)互換の列名(Sales/OP/OdP/NP/EqAR等)にパースする。東証以外の取引所単独上場企業の決算短信も同一の標準タクソノミ(`tse-ed-t:...`)を使うことを実機確認済み。iXBRLの`scale`/`sign`属性を正しく反映しないと単位・符号を誤る（2026-08-19に前年同期の赤字をsign属性なしで正の値と誤読するバグを実機データで発見・修正した経緯あり）。TDnetの開示添付ファイルは公開から約1〜1.5ヶ月で取得できなくなり過去分をバックフィルできないため、決算短信本文に埋め込まれている前年同期実績も1行として一緒に抽出し、初回スキャンの時点から前年同期比較を可能にしている
- `src/auth.py`: パスワード認証（元はapp.py内にあったものを切り出し）
- `scripts/watch_and_notify.py`: ストップ高・株式分割/併合・経常利益急増（前年同期比+50%以上）・東証新規上場（承認発表・当日上場）を平日10:00/13:00 JSTに定期チェックしDiscordへ通知するバッチ（2026-08-27にユーザー指定、README「Discord通知」参照）。GitHub Actions(`.github/workflows/watch_and_notify.yml`)から実行され、`app.py`のUI安全弁（TDnet開示件数上限）を経由せずrules.py/endpoints.pyを直接呼ぶ。通知済み状態・各データ源のウォーターマーク（前回成功した走査開始日。長期休場明けの取りこぼし防止用）は`data/notify_state.json`にJSONで保存し、ワークフロー側で`notify-state`という専用ブランチにコミットして実行間（使い捨てコンテナ）をまたいで永続化する（`main`にコミットするとStreamlit Cloudの不要な再デプロイを招くため専用ブランチに分離。2026-08-27のCodexレビューで指摘・修正）
- `src/jpx_new_listings.py`: JPX公式サイトの「新規上場会社情報」ページ（東証本体のみ。UTF-8）をスクレイピングし、銘柄コード単位で上場日・上場承認日・市場区分を取得する。上場前の会社はTDnetアカウントを持たないため、TDnet開示だけでは東証新規上場の「上場承認」を網羅的に検出できないことを実データで確認した上で採用したデータ源（watch_and_notify.py専用）
- `src/discord_notify.py`: Discord Webhookへのメッセージ送信（2000文字上限を超える場合は分割送信）
- `src/market_calendar.py`: 土日・日本の祝日（`jpholiday`パッケージ）・東証の年末年始休場(12/31・1/2・1/3。国民の祝日ではないためjpholidayだけでは判定できない)判定。watch_and_notify.py専用

## 開発体制（Claude Code + Codexレビュー）

2026-08-13にユーザーが明示した体制。

- **Claude Code**: 実装担当（新機能実装、バグ修正、Streamlit画面開発、API連携、Excel出力処理、データ取得処理、コード整理・リファクタリング、GitHub上のコード管理）
- **OpenAI Codex**: 第三者コードレビュー担当（バグ、ロジックの誤り、計算処理の正しさ、API取得処理の妥当性、欠損データ処理の適切さ、Streamlitでのエラー可能性、無駄な処理、処理速度、セキュリティ、APIキー等の秘密情報の直書き、より安全・シンプルな実装の有無）
- 基本フロー: Claude Codeが実装 → PR作成 → Codexが自動レビュー → 指摘を確認 → Claude Codeが修正 → 動作確認 → マージ

**Codexの指摘は無条件採用しない。** 指摘ごとに「本当に修正が必要か／既存仕様を壊さないか／他の機能に影響しないか／Codex側の判断ミスではないか」をClaude Code側で確認してから反映する。Claude CodeとCodexの意見が分かれた場合は理由を比較し、より安全で正しい方法を採用する。

**ブランチ運用（可能な範囲で）**: `main`=安定版（GitHubのmainへのpushでStreamlit Cloudが自動デプロイされる）、`develop`=開発・テスト用。大きめの変更は`develop`で作業してから動作確認後に`main`へマージする（`main`への直接pushは即デプロイに直結するため、動作未確認のまま行わない）。細かい修正は従来通り直接mainで作業してもよい。

**データの正確性を最優先**: 見た目のUIより、(1)数値が正しい (2)データ取得元が正しい (3)計算式が正しい (4)欠損データを誤った数字で埋めない (5)取得できない場合は「取得不可」と判断できる (6)同じ処理を何度実行しても結果が安定する、ことを優先する。特にPER・PBR・時価総額・営業利益率・自己資本比率・配当利回り・決算情報・業績予想は、誤った推測値を出さない。

**Codexレビューの自動化（2026-08-13に構成）**: ユーザーがChatGPT側でGitHub連携を設定済み（`tatsuyahakogawa-sketch/jquants-stock-screener`へのアクセスを許可、PR作成時にCodexが自動レビューする設定）。これにより、Claude Codeは以下を自分で完結できる:

1. まとまった変更（新機能・ロジック変更等）は`develop`からfeatureブランチを切って実装・テストする
2. `gh pr create --base develop`でdevelop向けにPRを作成する（`--base`を省略するとリポジトリの既定ブランチ=`main`向けのPRになってしまうため必須。GitHub CLIはこの環境で認証済み）
3. Codexの自動レビューを待つ（有効化されていない場合はPRに`@codex review`とコメントして起動する）
4. `gh api repos/tatsuyahakogawa-sketch/jquants-stock-screener/pulls/<PR番号>/comments`や`gh pr view <PR番号> --comments`でレビュー内容を取得する（ユーザーがコピペで貼り直す必要はない）
5. 各指摘を上記「Codexの指摘は無条件採用しない」の基準で判断し、必要な修正のみ実装してpush
6. 問題なければ`develop`（→動作確認後`main`）へマージする

軽微な修正（typo、ドキュメントのみ等）はPRを介さず直接ブランチにpushしてよい。

## 未決事項（今後詰める内容）

- 「オーナー経営」「取引先」は未実装（EDINET有報の自由記述テキスト解析が必要。README.md参照）
- TDnetの非公式ミラーAPI（やのしん氏運営）が停止した場合の切り替え先（公式スクレイピング or JPX有料API）
- 複数年遡及が必要なルール（sales_growth_doubling・profit_doubling）は
  `src/endpoints.get_statements_range`が1日ごとに個別リクエストするバルク取得+キャッシュ方式のため、
  初回・キャッシュが冷えている場合に遡り日数分（sales_growth_doublingで約790日≒13分、
  profit_doublingで約1520日≒25分、Lightプランの60件/分制限下）の待ち時間が発生する
  （2026-08-25の8巡目のCodexレビューで指摘。2回目以降は当日分を除きキャッシュ済みになるため
  高速。根本解決には`src/regional_stocks.py`の地方株スキャンのような「前回スキャン日以降だけ
  追加取得」方式の専用ストアへの作り直しが必要だが、他の複数年遡及ルールにも影響する
  アーキテクチャ変更のため別タスクとする）。sales_growth_major/explosive・two_quarter_growth・
  profit_growth_majorは前年同期比較(約425日)で足りるため、これらのうちprofit_doublingを
  含まない組み合わせを選んだ場合は約425日分の遡りで済む（2026-08-27のCodexレビューで、
  以前は選択内容に関わらず一律でprofit_doubling相当の約4年分を遡っていたため
  profit_growth_major単独選択時にも無駄な待ち時間が発生する不具合を指摘され、選択中の
  ルールが実際に必要とする日数の最大値だけ遡るよう修正した）
- `.github/workflows/watch_and_notify.yml`のschedule実行（平日10:00・13:00 JST）が
  2026-08-31に2回連続（10:00・13:00 JSTの両方）で発火しなかった。切り分けのため以下を確認済み:
  (1) `workflow_dispatch`（手動実行）は同日中に2回とも正常に成功しており、Secrets
  （`JQUANTS_API_KEY`・`DISCORD_WEBHOOK_URL`）・Actions権限（`contents: write`）・
  Discord Webhook自体（実URLへ直接テストPOSTしdiscordが204を返すことを実機確認）は
  いずれも問題なし。(2) ワークフローの状態は`gh api .../actions/workflows`で`state: "active"`、
  cron構文（`0 1 * * 1-5` / `0 4 * * 1-5`、UTC）・ワークフローファイルが`main`（デフォルト
  ブランチ）上に存在することも確認済み。(3) https://www.githubstatus.com/ 確認時点で、
  この完全な未発火を説明できるような進行中の障害は見当たらなかった（直近のActions関連
  インシデントは8/18・8/24・8/26でいずれも「遅延」であり「完全に発火しない」ものではなく、
  かつ全て解決済み）。原因はGitHub Actions側でこのワークフローのschedule登録が反映されて
  いない（またはできていない）可能性が高いと推測し、暫定対処としてワークフローファイルに
  変更を加えて`main`へpush（2a3054d）し、cron再登録を試みた。翌営業日以降の自動発火有無を
  要継続確認。恒久対策が必要な場合の候補: (a) scheduleが一定時間内に発火したかを検知する
  監視の追加（例: 別ジョブが`gh api .../actions/runs`を見て`event: schedule`の最終実行時刻を
  チェックし、閾値超過でDiscordに警告する）、(b) cron式を毎時ちょうど(`:00`)から数分ずらす
  （GitHub公式が「毎時ちょうどは高負荷になりやすく遅延しやすい」と明言しているため）。

## 注意事項

- APIキーや認証情報を絶対にコード・リポジトリに直接書き込まない
- 個人情報（氏名・住所・電話番号等）を含むファイルをコードベースに含めない

## 複数PCでの開発（デスクトップ・ノートPC）

このプロジェクトフォルダ自体をOneDrive等のクラウド同期フォルダに置くことは禁止
（`.git`をファイル同期ツールと併用すると、同時操作でリポジトリが壊れたり
「ファイル名 (1).py」のような重複ファイルが生じたりする実績多数のため）。

代わりにGitHub（private repo: `tatsuyahakogawa-sketch/jquants-stock-screener`）
を経由する。各PCでの作業ルール:

- `main`で直接作業する場合は、始める前に必ず `git pull origin main`、終わったら `git add` → `git commit` → `git push origin main`
- `develop`やfeatureブランチで作業する場合、上記の`main`固定コマンドはそのブランチの変更を同期しない（`git pull origin main`は現在のブランチにmainの更新を取り込むだけ、`git push origin main`はローカルのmainブランチを送るだけで、今の作業ブランチはpushされない）。今のブランチ名を明示して `git pull origin <ブランチ名>` / `git push -u origin <ブランチ名>` を使うこと
- `src/`以下のコードを変更した後、ローカルでStreamlitを動かして確認する場合は
  プロセスを完全に再起動すること（`streamlit run app.py`のホットリロードは
  app.py本体は再実行するが、`pipeline.py`や`excel_export.py`等のサブモジュールの
  変更はプロセスを再起動しないと反映されないことがあると2026-07-31に実機で確認済み）

利用者向けのアプリ自体はStreamlit Community Cloudにデプロイ済み
（GitHubのmainブランチにプッシュすると自動再デプロイされる）。アプリ設定の
「Secrets」に`app_password`・`JQUANTS_API_KEY`・`EDINET_API_KEY`
（・任意で`SUPABASE_URL`・`SUPABASE_KEY`、README参照）を設定し、
「Sharing」を「このアプリは公開されており、検索可能です」にしておくことで、
URLとアプリ内のパスワードさえ分かればどのPC・ブラウザからでも同じ最新版に
アクセスできる（Streamlit Cloud自体の非公開設定と、アプリ内のパスワード認証は
別レイヤーなので両方確認すること）。
