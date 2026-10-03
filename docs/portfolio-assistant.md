# Phase 7: Portfolio Assistant Core

実保有株を手入力で記録し、平均取得原価、評価損益、通貨別構成比、設定した閾値への到達状況を取得する独立したCLIです。投資判断の自動化、売買推奨、broker接続・注文送信、shell実行、LLM、Raphael依存はありません。

## 研究基盤からの分離

既存Phase 0〜6はPIT・holdout保護・ExperimentStore・設定freeze・研究用paper portfolioを扱います。新規 `ai_trading.portfolio` パッケージは研究モジュールをimportせず、研究DB、frozen strategy、simulated fillを使いません。既存J-Quants Providerも変更していません。

```text
CLI / 将来のJSON client
  ├─ init / add / transactions / config
  │    └─ PortfolioStore → 専用SQLite取引台帳
  └─ status / alerts
       ├─ 台帳を約定時刻順にreplay → Position
       ├─ SnapshotProvider → MarketSnapshot（手動JSON / fake）
       └─ 評価 / 通貨別集計 / rule判定 → JSON
```

既定DBは `data/user-portfolio/portfolio.sqlite3`。研究用forward ledgerと別のapplication IDを使い、既存ファイルへのinit、異種DBの利用、自動migration、自動修復を拒否します。CLIのDBパスは現在の作業ディレクトリ基準です。新規ユーザーDBを作成するまでadd/status等は失敗します。

## 起動

Python 3.9以上、実行時の追加依存なし。プロジェクトルートで実行します。以下のsnapshot価格は架空です。

Mac / Linux:

```sh
export PYTHONPATH=src
export PYTHONUTF8=1
python3 -m ai_trading.portfolio init --json
python3 -m ai_trading.portfolio add --id trade-001 --symbol 7203 --side buy --quantity 100 --price 2500 --fee 100 --currency JPY --executed-at 2025-01-01 --note "手入力" --json
python3 -m ai_trading.portfolio config --file config/portfolio.example.json --json
python3 -m ai_trading.portfolio transactions --json
python3 -m ai_trading.portfolio status --json
```

Windows PowerShell:

```powershell
$env:PYTHONPATH = "src"
$env:PYTHONUTF8 = "1"
py -3.12 -m ai_trading.portfolio init --json
py -3.12 -m ai_trading.portfolio add --id trade-001 --symbol 7203 --side buy --quantity 100 --price 2500 --fee 100 --currency JPY --executed-at 2025-01-01 --note "手入力" --json
py -3.12 -m ai_trading.portfolio config --file config/portfolio.example.json --json
py -3.12 -m ai_trading.portfolio status --json
```

既にinit済みなら繰り返しません。別の台帳を作るときは全コマンドへ `--db <別の新規パス>` を渡します。`--db` / `--json` はサブコマンドの前後どちらでも指定できます。既存の `research-data` CLIは変更していません。

| コマンド | 内容 |
| --- | --- |
| init | 新規DBを排他的に作成 |
| add | 取引を検証し、1件追加。ID省略時はUUIDを生成 |
| transactions | 台帳追加順に履歴を取得 |
| config | 現在設定を表示 |
| config --file PATH | 設定を全置換。省略した閾値は無効に戻す |
| status | 保有、評価、通貨別集計 |
| alerts | 人間向けには評価集計とルール判定。JSONではstatusと共通の完全なreport。注文は生成しない |

日時はoffset付きISO 8601。取引の日付だけを指定した場合はUTC 00:00として扱います。実際の約定時刻が分かる場合は `2025-01-01T09:10:00+09:00` のように指定してください。未来取引は拒否します。created_atはCLIがUTCで付与し、約定日時以降であることを確認します。

## 台帳・モデル・SQLite schema v1

| テーブル | 主な内容 |
| --- | --- |
| instruments | symbol + currencyの複合主キー、asset_type=equity |
| transactions | sequence、id UNIQUE、symbol/currency外部キー、side、quantity/price/fee、executed_at、note、created_at、economic_hash UNIQUE、previous_hash、entry_hash |
| metadata | 連番件数とチェーン末尾hash |
| rules | 現在の設定JSONとSHA-256 |

Positionは保存せず毎回取引から再構成します。quantity、average_cost、total_cost、realized_pnl、symbol、currencyを持ちます。

- long-only株式のbuy/sellのみ。asset_typeはequityのみ受理します。short、margin、option、future、crypto専用モデルや執行機能はありません。
- 銘柄コードは大文字英数字、ピリオド、ハイフンの1〜32文字。銘柄マスター照合は未実装で、株式であることは入力者の申告に依存します。
- quantity/priceは正、feeは0以上。数値入力は値は1e18未満・小数部12桁以内、float/bool/NaN/無限大は拒否します。
- 通貨はJPY/USD/EUR/GBP/CHF/CAD/AUD/NZD/HKD/SGD/CNY/SEK/NOK/DKK/INR/KRW/TWD。通貨別の小数丸め、税務、為替換算はしません。同じ銘柄でも通貨が異なれば別positionです。
- ID重複と、銘柄・通貨・売買・数量・価格・手数料・約定時刻・asset_typeが同じ取引の重複を拒否します。note/created_atを変えても重複です。同時刻・同値の実際の別約定を区別する外部約定ID設計は将来の課題です。
- 再生順は約定時刻順、同時刻は台帳追加順。過去日の追加時も全履歴で保有超過売却がないか検証します。
- 書込みはBEGIN IMMEDIATEによる1操作1transaction。foreign_keysを有効化し、DB接続はfinallyでcloseします。同時売却は直列化され、超過した側が失敗します。
- transactions/instrumentsのUPDATE/DELETEはtriggerで拒否。台帳hash chain・連番・末尾件数・設定hash・schema・SQLite integrity/FKを読書き前に検証します。異常は固定コードのエラーで停止します。
- SQLite自体を編集できる管理者による、hashを含む全体書換えへの認証・WORM保証はありません。
- 約定の訂正・取消・インポート・配当・分割/併合は未実装です。実際に売買していない取引を訂正目的で作らず、このMVPの外で台帳を管理してください。
- 毎回全履歴を読む小規模台帳向けの実装です。DBは平文で、認証情報を保存する列や読取処理はありません。noteは自由入力なので認証情報を書かないでください。実保有DBはGitへ追加しません。

## 平均取得原価と損益

数量q、取得原価C、買付数量b、買値p、手数料f:

- 買付後数量 = q + b
- 買付後原価 = C + b × p + f
- 平均取得原価 = 原価 / 数量

数量sを売却すると、配分原価 = C × s / q、実現損益増分 = s × 売値 − 売却手数料 − 配分原価。残存原価はCから配分原価を引きます。全売却時は原価全額を配分し残存数量・原価を厳密に0にします。実現損益は全売却・再購入後も累積します。closed positionも履歴確認のため残します。

評価額 = 数量 × 現在価格、未実現損益 = 評価額 − 残存原価、未実現損益率 = 未実現損益 / 残存原価。日次騰落率 = (現在価格 − 前日終値) / 前日終値。将来の売却手数料は推測しません。

計算は独立したDecimalコンテキスト（精度60桁、ROUND_HALF_EVEN）を使い、外側のDecimal設定に依存しません。除算の循環小数は60有効桁へ丸めます。入力・DB・JSONは10進文字列を使用し、JSONの数値入力もDecimalで読みます。

構成比は同一通貨の保有評価額に対する比率です。現金、為替、他通貨を分母へ混ぜません。異なる通貨の総合計はありません。閉じたpositionのaverage_costはnullです。

## 価格入力と時点

SnapshotProviderは `snapshot(symbol, currency) -> MarketSnapshot | None` の境界だけを持ちます。engineはJ-QuantsやHTTPを認識しません。現MVPは手動JSONとfakeのみで、自動の現在値取得はありません。

`data/user-portfolio/snapshot.json` の例:

```json
{
  "schema_version": 1,
  "snapshots": [
    {
      "symbol": "7203",
      "price": "3000",
      "previous_close": "2900",
      "currency": "JPY",
      "as_of": "2025-01-03T06:00:00Z",
      "source": "manual-example"
    }
  ]
}
```

```sh
python3 -m ai_trading.portfolio status --snapshot data/user-portfolio/snapshot.json --as-of 2025-01-03T06:01:00Z --json
python3 -m ai_trading.portfolio alerts --snapshot data/user-portfolio/snapshot.json --as-of 2025-01-03T06:01:00Z --json
```

Windowsではpython3をpy -3.12へ置き換えます。status/alertsのas-of省略時は実行時刻。全取引がas-of以前である必要があり、履歴を黙って部分的に除外しません。

価格ファイルは最大1MiB。unknown field、重複JSONキー、重複symbol/currency、不正数値、無効な日時を拒否します。previous_closeはnull可。sourceは入力元ラベルで、価格の真正性保証ではありません。

| valuation_status | 扱い |
| --- | --- |
| ok | 価格を評価に使用 |
| missing | 価格なし |
| stale | age > max_snapshot_age_seconds |
| future | snapshotの時刻が評価時刻より未来 |
| before_transaction | snapshotがそのpositionの最新約定より古い |
| closed | 数量0、価格照会不要 |

未来・古すぎる・約定前の価格はsnapshot metadataとして表示しますが、current_price、評価額、未実現損益はnullです。古さは営業日ではなく経過秒数で判定し、境界ちょうどは有効です。

価格欠落がある通貨はcomplete=false、合計market_value/unrealized_pnlとその通貨の全構成比がnull。known_market_valueは評価できた分だけであり総資産ではありません。原価と実現損益は価格がなくても取得できます。別通貨の完全な集計は利用できます。

価格snapshot・source・rawレスポンスはDBへ保存しません。status/alertsはDB読取り専用です。

## Alert仕様

新規DBでは全閾値がnull（無効）。例示設定をユーザーが明示適用する方式です。`config/portfolio.example.json` は以下の例であり推奨設定ではありません。

| 設定（百分率） | 比較 | type | severity |
| --- | --- | --- | --- |
| take_profit_pct=20 | 未実現損益率 >= 0.20 | take_profit_threshold | info |
| loss_warning_pct=-10 | 未実現損益率 <= -0.10 | loss_warning_threshold | warning |
| daily_move_pct=7 | 日次騰落率の絶対値 >= 0.07 | daily_move_threshold | warning |
| max_position_weight_pct=30 | 同一通貨の構成比 > 0.30 | concentration_threshold | warning |

正の閾値を指定し、loss_warning_pctのみ-100以上0未満。構成比上限は100以下。max_snapshot_age_secondsは1〜31536000、既定86400。欠測時はtriggered=null/evaluation=not_evaluableで、falseと区別します。previous_closeがなければ日次ルールだけ評価不能。closed positionは対象外です。通知は有効なルールをtrue/false/nullすべて返し、BUY/SELL命令や外部通知を返しません。

## JSON契約 v1

`--json` はUTF-8 JSONをstdoutへ1個出し、成功exit 0、失敗exit 2。成功のenvelope:

```json
{"schema_version":1,"ok":true,"command":"status","generated_at":"2025-01-03T12:00:01.000000Z","data":{"as_of":"2025-01-03T12:00:00.000000Z","weight_basis":"same_currency_open_positions","portfolio_summary":{"transaction_count":0,"open_position_count":0,"closed_position_count":0,"currencies":[],"valuation_complete":true},"positions":[],"currency_summaries":[],"alerts":[],"rules":{"take_profit_pct":null,"loss_warning_pct":null,"daily_move_pct":null,"max_position_weight_pct":null,"max_snapshot_age_seconds":86400}}}
```

status.data.positionsの各要素:

| フィールド | JSON型 |
| --- | --- |
| symbol/currency/valuation_status | string |
| quantity/total_cost/realized_pnl | decimal string |
| average_cost/current_price/market_value/unrealized_pnl | decimal string または null |
| unrealized_pnl_pct/portfolio_weight/daily_move | decimal string または null（0.2 = 20%） |
| snapshot | MarketSnapshot object または null |

status.data.currency_summariesはcurrency別配列で、currency/total_cost/realized_pnl/known_market_value、market_value/unrealized_pnl（null可）、complete（boolean）、unpriced_symbols（string配列）を持ちます。空台帳はpositions/currency_summariesとも空配列。

status/alertsのJSON dataは共通です。portfolio_summaryはtransaction_count、open_position_count、closed_position_count、currencies、valuation_completeを持ち、通貨をまたいだ金額合計は作りません。generated_atは生成時刻、data.as_ofは評価基準時刻で、過去のsnapshot評価時にも区別します。data.alertsの各要素の例:

```json
{"type":"take_profit_threshold","symbol":"7203","currency":"JPY","severity":"info","triggered":true,"value":"0.217","threshold":"0.2","evaluation":"evaluated","reason":"configured_threshold_reached"}
```

value/thresholdは10進文字列。reasonはconfigured_threshold_reached / configured_threshold_not_reached / price_or_weight_unavailable。日次値のvalueは符号付きです。

init.dataはinitialized、add.dataはtransaction、transactions.dataはtransactions配列、config.dataはrules。transactionはid/symbol/side/quantity/price/fee/currency/executed_at/note/created_at/asset_typeを持ちます。

失敗例:

```json
{"schema_version":1,"ok":false,"command":"add","generated_at":"2025-01-03T12:00:01.000000Z","error":{"code":"oversell"}}
```

引数解析前の失敗ではcommand=null。代表コードはinvalid_arguments、invalid_decimal、invalid_timestamp、unsupported_transaction、duplicate_transaction、oversell、database_not_initialized、database_already_exists、unsupported_database、schema_integrity_error、ledger_integrity_error、config_integrity_error、storage_error。API key・入力本文・例外tracebackをエラーへ反射しません。clientはok/codeを読み、未知のcodeも失敗として扱います。`--help` は通常の人間向けヘルプです。

`--json`なしは日本語の人間向け表示で、この表示の文字列解析はclient契約に含めません。

このJSON契約を将来Raphael等から利用できますが、今回の実装に接続・プロセス起動・TTS・UIはありません。

## 検証

専用テスト:

```sh
PYTHONUTF8=1 PYTHONPATH=src python3 -m unittest discover -s tests -p test_portfolio.py -v
PYTHONUTF8=1 PYTHONPATH=src python3 -m unittest discover -s tests -v
```

fake価格と一時DBで、取得原価、手数料、再購入、超過売却、Decimal精度、閾値境界、複数通貨、時点、欠測、CLI JSON、同時書込み、rollback、重複、再読込み、hash/schema破損を検証します。実APIは呼びません。

Windowsでは既存研究コードがUTF-8を既定と仮定しているためPYTHONUTF8=1を指定します。

Phase 6のLedger.connect()はSQLite connectionを返し、with終了でcommit/rollbackしてもcloseしていませんでした。これによりGCまでWindowsのファイルロックが残り、一時DB cleanupでWinError 32が発生していました。明示的なcontextmanagerで従来のcommit/rollback後にfinallyでcloseする修正だけを行いました。保持されたcursor/connectionが終了後に利用できず、正常時commit・例外時rollbackと即時ファイル削除が成立することを専用テストで確認しています。sleep/retry、GCによる回避、テストの成功扱い変更はありません。ExperimentStore/storageはSQLiteを使用しません。

forward.pyの内容変更により研究側code_hashは変わります。既存freezeに対するコード一致検査を緩めず、新しいfixtureは新規ディレクトリにfreezeしました。古いfreezeを今回のコードで継続せず、元のコード・環境で扱うか、新規seriesとして明示的に開始してください。

2026-10-03 / Windows / Python 3.12 / UTF-8で、再開時のPortfolio 43件が成功。JSON契約・人間向け表示の2件追加後はPortfolio 45件、resource closeの2件を含む全228件が成功（既存181件を含む）、SQLite cleanup errorは0件でした。

既存ML fixture、market fixture、forward fixture、forward verify（5 entry）、forward reportも成功しました。検証時は子プロセス環境からJQUANTS_API_KEYを除き、架空developmentデータだけを使い、holdoutを開いていません。生成物はGit除外のexperiments配下、ログと手動CLIのDBはTemp配下です。

手動CLI確認では7203を100株・単価2500円・手数料0円で登録し、架空価格3000円に対して評価額300000円、未実現損益50000円、+20%閾値到達を確認しました。価格なしの場合はmissing/nullとなります。

## 残る制約とPhase 8候補

- 実価格adapterは未実装。手動snapshotのsource・価格は信頼できる元データと照合してください。
- 株式の実在・資産種別はマスター未照合。取引訂正・同一条件の別約定・分割/配当は未対応です。
- 税計算、FX、入出金/現金残高、口座別管理、broker、通知、自動売買はありません。
- 次段階は銘柄マスター検証、明示的な訂正/取消イベント、重複を識別できるCSV import、価格adapter、企業行動を順に検討できます。研究用forward ledgerとの統合は行いません。
