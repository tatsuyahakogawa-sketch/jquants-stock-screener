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

- `app.py`: Streamlit UI（絞り込み条件選択 → スクリーニング実行 → 銘柄集計テーブル/CSV）
- `src/jquants_client.py`: J-Quants認証・レート制限（429時は65秒待って自動リトライ）・ページネーション
- `src/endpoints.py`: 日付単位バルク取得 + ローカルキャッシュ（`data/cache/`, parquet）
- `src/cache.py`: 上記ローカルキャッシュの実体。`SUPABASE_URL`/`SUPABASE_KEY`設定時はSupabase（PostgREST）にも保存し、Streamlit Cloudの再デプロイでローカルキャッシュが消えても再利用できるようにする（任意設定、未設定でも動作、README参照）
- `src/tdnet_client.py`: TDnet開示情報の非公式ミラーAPIクライアント（新工場・新店舗、東証移籍検出、実行表の「最近のトピック」欄用）
- `src/edinet_client.py`: EDINET(金融庁)から有価証券報告書の事業概要・大株主・潜在株式・事業等のリスク・経営方針をテキストブロック単位で取得（`EDINET_API_KEY`必須、README参照）。要約はせず開示テキストをそのまま使う
- `src/rules.py`: 各条件の判定ロジック（J-Quants数値データ、市場区分履歴、TDnetタイトル検出）
- `src/pipeline.py`: 取得〜判定〜銘柄単位集計(build_summary)〜市場データ付与(enrich_with_market_data)のオーケストレーション。時価総額/PER/PBR/配当利回り計算(compute_market_metrics)と上場日近似(estimate_listing_date)はexcel_export.pyと共用
- `src/excel_export.py`: 銘柄コード→Excel自動生成。企業詳細・実行表とも、元のExcel（個人情報・実データを除いた汎用テンプレート化: `templates/company_detail_template.xlsx`, `templates/execution_table_template.xlsx`）のレイアウト・書式・数式をそのまま使い値だけを埋める方式。定性コメント欄はtdnet_client/edinet_clientの実データで埋め、市況解釈が必要な項目（価格が上下した理由）は空欄のまま
- `scripts/inspect_schema.py`: J-QuantsレスポンスのカラムをAPI実物で確認する検証用スクリプト

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
2. `gh pr create`でPRを作成する（GitHub CLIはこの環境で認証済み）
3. Codexの自動レビューを待つ（有効化されていない場合はPRに`@codex review`とコメントして起動する）
4. `gh api repos/tatsuyahakogawa-sketch/jquants-stock-screener/pulls/<PR番号>/comments`や`gh pr view <PR番号> --comments`でレビュー内容を取得する（ユーザーがコピペで貼り直す必要はない）
5. 各指摘を上記「Codexの指摘は無条件採用しない」の基準で判断し、必要な修正のみ実装してpush
6. 問題なければ`develop`（→動作確認後`main`）へマージする

軽微な修正（typo、ドキュメントのみ等）はPRを介さず直接ブランチにpushしてよい。

## 未決事項（今後詰める内容）

- 「オーナー経営」「取引先」は未実装（EDINET有報の自由記述テキスト解析が必要。README.md参照）
- TDnetの非公式ミラーAPI（やのしん氏運営）が停止した場合の切り替え先（公式スクレイピング or JPX有料API）

## 注意事項

- APIキーや認証情報を絶対にコード・リポジトリに直接書き込まない
- 個人情報（氏名・住所・電話番号等）を含むファイルをコードベースに含めない

## 複数PCでの開発（デスクトップ・ノートPC）

このプロジェクトフォルダ自体をOneDrive等のクラウド同期フォルダに置くことは禁止
（`.git`をファイル同期ツールと併用すると、同時操作でリポジトリが壊れたり
「ファイル名 (1).py」のような重複ファイルが生じたりする実績多数のため）。

代わりにGitHub（private repo: `tatsuyahakogawa-sketch/jquants-stock-screener`）
を経由する。各PCでの作業ルール:

- 作業を始める前に必ず `git pull origin main`
- 作業が終わったら `git add` → `git commit` → `git push origin main`
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
