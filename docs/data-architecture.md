# データ基盤 v1 — Phase 1

## プロジェクト構成と実行環境

`src/ai_trading` に設定、PITモデル、保存、取得、品質検査、CLI、再生を分離する。
Python 3.9以上、実行時は標準ライブラリのみ。検証環境はPython 3.9.6。
ビルド用setuptoolsはpyprojectで版を固定し、データmanifestには実行Python版とパッケージ内全ソースのハッシュを記録する。
学習ライブラリ等の追加時は依存ロックと対応Python版を改めて固定する。

初期の保存形式はJSON/JSONLとする。原本の監査と依存なしの動作を優先し、Parquet・DuckDBは量が増えた段階で追加する。
形式変更時も原本・PITフィールド・データセット版の意味は維持する。

## 設定

`config/research.json` が実行設定。未定義のキーや研究範囲の変更を拒否する。

| セクション | 内容 |
|---|---|
| schema_version | 設定スキーマ版。現在1 |
| research | 日本株、日足、現物買い、週次、レバレッジなし |
| storage | raw / processed / experiments の保存先 |
| provider | jquants_v2、fixture/live、fixtureパス、認証環境変数名 |
| pit | observed、strict_before。取得CLIでは変更不可 |
| dataset | daily_bars と取得日（YYYY-MM-DD） |

相対パスは作業ディレクトリではなく設定ファイルの親から解決する。
APIキーそのものは設定・manifest・ログに保存せず、`JQUANTS_API_KEY` のみから読み取る。
liveを使う際は設定を複製し、`provider.mode` を明示的に変更する。デフォルトのfixture実行は環境変数が存在しても通信しない。

## 保存レイアウト

```text
data/
  raw/jquants_v2/daily_bars/
    bodies/<sha256>.json        # HTTP応答の元バイト列（不正JSONでも原本を残す）
    receipts/<sha256>.json      # 問い合わせ条件・取得時刻・bodyハッシュ
  processed/<source>/daily_bars/v1/
    <sha256>.jsonl              # 全ページ取得・品質チェック後の観測データ
experiments/
  quality/<sha256>.json         # エラーと警告
  manifests/<sha256>.json       # データ取得実験の設定・来歴・出力参照
tests/fixtures/
  daily_pages.json             # バージョン管理する架空のAPI応答
```

rawのパスはAPI形式を表す。fixtureの原本も同じ形式で保存するが、manifestの `synthetic=true` と
processedの `source=fixture_jquants_v2` で実データから区別する。
fixtureのraw receipt単独では実データ判定を行わず、必ずmanifestと一緒に扱う。
data/ と experiments/ はGit対象外。docs、設定、実装、fixture、テストはGit対象。

保存は内容ハッシュをファイル名とした新規作成のみ。同じ内容の再実行は同じファイルを参照する。
取得時刻や応答内容が変われば別のreceiptとなり、訂正前の原本を上書きしない。
検証付き読み込みで改変を検出する。OSレベルの書換禁止ではなく、破損検出の仕組みである。
書き込み中の中断で破損したファイルが残った場合は検証が停止するため、バックアップから復元する。

## Point-in-timeモデル

`Observation` は変更不可のdataclass。payloadも文字列で保持し、辞書の後書換えを避ける。

| フィールド | 意味 |
|---|---|
| dataset / entity_id | データ種別と提供元スコープの銘柄ID |
| event_at | 情報の対象日時。日足ではセッション日をJST午前0時で表したラベル |
| published_at | 公表日時。不明ならnull。日付から時刻を捏造しない |
| available_at | この版の情報が利用可能になった日時 |
| ingested_at | 自システムが応答全体を取得完了した日時 |
| revision_id | 版識別子。日足では元行の内容ハッシュ。大小関係は持たない |
| source | 提供元。fixtureには別の名前空間を使う |
| payload_json | 非調整OHLCV、調整係数、対象日・コード |
| availability_basis | observed / documented / estimated |
| availability_evidence | 原本ハッシュ、開示ID、または推定ルールの根拠 |

全datetimeはタイムゾーン必須、内部UTCへ正規化。`available_at >= published_at` を必須とする。
observedでは `available_at >= ingested_at` を必須とし、日足アダプタでは取得完了時刻を使う。
当日セッションの未確定データの可能性は品質警告とし、営業日・更新時刻の厳密検証は今後追加する。

`as_of(rows, decision_at)` は `available_at < decision_at` かつ `ingested_at < decision_at` の版だけを使う。
同時刻は除外する。対象キー `(dataset, source, entity_id, event_at)` ごとに利用可能時刻が最新の版を選ぶ。
同じ利用可能時刻に異なる版があれば曖昧な順序を推測せずエラー。結果は入力順に依存しない。
財務の対象期末や、すでに発表済みの将来配当日で情報の可否を判断しない。
日足がセッション開始前に利用可能となっている矛盾は日足品質検査が検出する。

historicalモードはライブラリでのみ明示指定可能。`knowledge_at` を必須として取り込み済み情報の範囲も固定する。
過去の利用可能時刻を持たせる場合は公表時刻と根拠が必要。estimatedは既定で除外し、許可には明示オプションが必要。
過去データを今日取得しても、observedデータが当時使えたことにはならない。
本APIの日足から、当時の配信時刻や訂正前の値を自動復元する機能はない。

同一内容の再取得は別snapshotとして保管する。複数snapshotをまとめる際は同一版の重複を整理する必要があり、
未整理の重複は品質エラーにする。再取得時刻を新たな公表時刻とは扱わない。

## J-Quants取得層

第一候補はJ-Quants v2。2026-09-14に公式クライアントでAPIキー方式、基底URL、日足取得、ページングを確認。
仕様参照:

- https://github.com/J-Quants/jquants-api-client-python
- https://github.com/J-Quants/jquants-api-client-python/blob/main/jquantsapi/client_v2.py

実装対象はGET `https://api.jquants.com/v2/equities/bars/daily`。
`date` と `pagination_key` を使い、認証ヘッダは `x-api-key`。
`data` 配列のDate、Code、O/H/L/C、Vo、AdjFactorを正規化する。調整済み価格は原本に残すが正規化OHLCに混ぜない。
各ページの元応答を先に保存し、全ページ完了後にprocessedを作る。循環カーソルと想定外の日付を拒否する。
タイムアウト30秒。HTTPエラー、429、通信エラーは停止し、部分取得を完了扱いにしない。
現段階は自動再試行・レート制御・差分スケジューラなし。再実行は新しい取得記録になる。
異常応答・中断時にはrawが残り、正常なデータセットmanifestは作られない。品質エラーの場合は失敗manifestを残す。

Transportプロトコルで通信を注入でき、FixtureTransportで同じページング・正規化経路を検証する。
実APIを使った疎通は未実施。プラン、履歴期間、遅延、上場廃止カバレッジ、契約条件は採用前の確認事項。

## 後続データのスキーマ方針（取得は未実装）

| dataset | 対象キー・payload候補 |
|---|---|
| instrument_master | 恒久ID、コードと有効期間、市場、上場・廃止日。現在の一覧を過去に適用しない |
| trading_calendar | 取引所、営業日、取引時間。改定時の公開・取得日時を保持 |
| corporate_actions | イベントID、発表・権利・効力・支払日、分割比率・配当額、取消版 |
| financials | 開示ID、対象期間、項目、単位、会計基準、訂正版 |

同じPIT包絡モデルを使い、dataset固有の品質検査を追加する。
現在のentity_idは提供元コードによる暫定IDであり、コード変更や再利用を解決した恒久IDではない。
この移行と企業イベントの完全性確認を終えるまで、市場全体のバックテスト用データと宣言しない。

## 品質ゲートと再現性

エラー: 非正値・非有限価格、不正OHLC範囲、負の出来高、不正調整係数、部分欠損、重複版・矛盾版、空集合、未来日足。
警告: 全OHLC欠損、出来高不明・ゼロ、調整係数不明、日中取得、銘柄・営業日カバレッジ未検証。
欠損は補完せず、売買停止と取得失敗を区別できなければ未分類のままにする。
`expected_keys` を渡した検査では銘柄・営業日単位の欠落と余分な行をエラーにする。
CLIでは当時のマスターがまだないため必ず `coverage_unverified` 警告を残す。passedは完全性や投資可能性の保証ではない。

manifestは設定、原本receipt、出力・品質レポートのハッシュ、Python版、コードハッシュ、可能ならGitコミットを保持。
未コミット変更もコードハッシュで識別するが、ハッシュだけではコードは復元できない。再現対象ソースはGitやアーカイブで別途保存する。
データsnapshotはmanifestに列挙したreceipt集合で固定し、フォルダの全ファイルを暗黙に読み込まない。
`ai_trading.replay` はネットワークを使わず原本を検証し、同じコードで正規化と品質レポートのハッシュ一致を確認する。
manifestのパスは実行機における絶対パス。別環境への移送・相対URIへの変換は未実装であり、現段階の再生は同じ配置で行う。

基礎テストは未来訂正追加の不変性、公表前除外、取得前除外、同時刻境界、タイムゾーン、証拠のないバックデート拒否、
推定データの既定除外、原本改変、再生、HTTP障害・ページング障害・品質エラー時の公開阻止をカバーする。

## 無料・登録不要の日本株データ候補（2026-11-13調査）

PR #2の資金制約、#3の実データPIT照合、#4のJ-Quants原本収集を維持したまま、
yfinance、Stooq、JPX公開資料を同じ基準で調べた。以下の「未確認」は実測や利用許諾の
根拠が得られていないことを意味し、取得不可の断定ではない。

| 項目 | yfinance / Yahoo Finance | Stooq | JPX公開資料 |
|---|---|---|---|
| 無料・登録不要 | ライブラリは無償。Yahooの公開APIを使うが、データ利用権は別 | サイトの閲覧入口は公開。CSV利用条件は未確認 | 公開Excelを登録なしで1件取得 |
| 日本株銘柄数 | `.T` を対象にする設計。実取得数は未確認 | `.jp` 2銘柄のCSV要求が404。対応数未確認 | 2026-10-31一覧は4,444コード（内国株式3,693行） |
| 日足OHLCV・期間 | ライブラリは日足と期間指定に対応。日本株の実範囲は未確認 | CSVが取得できず未確認 | 今回の一覧にはOHLCVなし。別の公開日報はPDF。機械可読の日足連続系列は未確認 |
| 配当・分割 | `actions=True` で取得するAPIがあるが、日本株の完全性・当時の訂正履歴は未確認 | 未確認 | 今回の一覧にはない。別資料との照合が必要 |
| 上場廃止銘柄 | 全候補と過去時点の網羅性は未確認 | 未確認 | 月末の現存一覧だけでは過去の廃止銘柄を網羅できない |
| 更新・遅延・制限 | 日本株で未計測。Yahooの利用条件を先に解決する | 未計測。サイトがブラウザ検証を要求し、CSVは404 | 上場一覧は月次更新。日足の取得時刻・訂正履歴は未確認 |
| ローカル保存・研究利用 | [yfinance README](https://github.com/ranaroussi/yfinance)はYahooの条件を参照させる。[Yahoo規約](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html)は許可のない自動収集を禁じるため、今回の自動取得は実施しない | 公式利用条件を確認できず、継続取得・再配布の可否は未確認 | [JPXサイト規約](https://www.jpx.co.jp/term-of-use/index.html)は二次利用・再配信を制限。研究用の継続保存・変換の許諾は未確認 |
| 品質・安定性 | 実行していないため未評価 | 7203.jp、9432.jp、対照のaapl.usが各1回404。日本株非対応との断定は不可 | 取得したExcelは日付・コードに欠損なし、コード重複なし。ただし1時点のみ |

### 小規模な実取得と照合

- yfinance: 実リクエスト0件。`yfinance`/`pandas`は環境未導入。許諾が確認できた場合に限り
  `7203.T` 等8銘柄を `auto_adjust=False, actions=True, keepna=True, repair=False`
  で取得し、OHLCVと `Adj Close`、配当・分割を別系列にする。
  [公式引数仕様](https://ranaroussi.github.io/yfinance/reference/api/yfinance.download.html)の
  `auto_adjust=True` 既定を使わない。`False` でも元配信値が完全な分割前価格であるとは
  証明できないので、原本との照合なしに執行価格へ入れない。
- Stooq: 2026-11-02〜13の `7203.jp`、`9432.jp`、対照 `aapl.us` の日足CSVを
  それぞれ1回だけ通常のGETで要求し、いずれもHTTP 404。再試行・ブラウザ検証の回避は
  行っていない。よって日本株対応、OHLCVの範囲、価格調整、企業行動は未確認。
- JPX: [東証上場銘柄一覧](https://www.jpx.co.jp/markets/statistics-equities/misc/01.html)の
  公開Excelを1件取得。取得完了は `2026-11-13T04:38:28.292967+00:00`、
  SHA-256は `97a28fdb7f605d5c84d20e5ce9354cf4f366c23f494a15f4578986f48e2a0023`。
  4,444行はすべて `20261031`、コードは4,444種類で重複・空欄なし。
  日付、コード、銘柄名、市場・商品区分、業種、規模の10列を確認した。
  代表の8コード `1301, 1332, 6501, 6758, 7203, 8306, 9432, 9984` はすべて掲載。
  原本と取得記録はGit対象外の `data/free-source-probe/` にのみ置いた。
  [公開日報](https://www.jpx.co.jp/markets/statistics-equities/daily/index.html)は別資料で、
  今回の日付別OHLCV検証には使っていない。

日足OHLCVを複数元から実取得できなかったため、同一日・同一銘柄の価格/出来高差、
日足欠損、配当・分割の差、10万・20万・30万円の実データ購入可能銘柄数は**算出不可**。
Excelの4,444コードを投資可能数や過去時点のuniverseへ転用しない。
新しいアダプタ・依存ライブラリ・データセットは追加しない。利用許諾、安定した日足原本、
未調整価格、企業行動と上場廃止履歴、営業日、各取得時刻が揃うまで、
いずれも研究バックテスト・フォワードの採用元としない。
