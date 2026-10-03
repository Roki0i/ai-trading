# Phase 8: Market Data Integration

Phase 7のPortfolioへ、J-Quants v2の最新取得可能な日足終値を渡します。リアルタイム価格、broker接続、注文、FX、Raphael連携はありません。

## 構成と既存基盤の再利用

```text
portfolio status / alerts
  → Providerを明示選択
  → JQuantsSnapshotProvider（銘柄/通貨を通信前に検証）
      → 既存JQuantsTransport / FixtureTransport
      → calendar / dated master / daily bars
      → 既存の原本hash・master・calendar・日足品質検証
      → MarketSnapshot
  → 既存Portfolio Engine
  → 通貨別評価・alerts・JSON / 人間向け表示
```

新規adapterは `src/ai_trading/portfolio/jquants.py`。EngineはJ-Quantsをimportせず、従来の `snapshot(symbol, currency)` 境界だけを使用します。

再利用する既存処理:

- providers.JQuantsTransport: GET限定・endpoint allowlist・JQUANTS_API_KEY・HTTP timeout。
- providers.FixtureTransport: ページ単位で決定的なローカルレスポンスを返す。
- providers.calendar_from_raw / market.TradingCalendar: 原本hash、日付範囲、休日区分、calendar coverage。
- validation.coverage: 日付指定masterのcode/Date/原本hash検証。日足を空にしたbundleでmaster検証だけを実施し、その結果を研究完全性の根拠にはしない。
- providers.validate_daily_row / normalize_daily、quality.inspect_daily: 日足の型、原本由来のmetadata、OHLC・出来高・調整係数などの検証。

研究用の取得・保存処理は変更しません。adapterでは原本とreceiptをメモリ上で扱い、全bundleのSHA-256をsource末尾へ付けます。これは1回の取得の同一性を示すもので、rawを永続保存しないため後日の原本再生保証ではありません。Portfolio SQLiteへ価格、レスポンス、API keyは保存しません。

## ProviderとCLI

既定は従来どおりmanualです。キーが環境に存在しても、manual指定では通信しません。

```sh
export PYTHONPATH=src
export PYTHONUTF8=1
python3 -m ai_trading.portfolio status --market-provider jquants --json
python3 -m ai_trading.portfolio alerts --market-provider jquants --json
```

liveには、ユーザーがプロセス環境へ設定した `JQUANTS_API_KEY` のみを使います。値をコマンド引数・設定ファイル・DBへ渡す方式はありません。キー未設定なら `market_auth_missing` で終了し、fixtureへfallbackしません。今回の検証でキー設定・実API接続はしていません。

PowerShellは以下を設定し、python3をpy -3.12へ置き換えます。

```powershell
$env:PYTHONPATH = "src"
$env:PYTHONUTF8 = "1"
py -3.12 -m ai_trading.portfolio status --market-provider jquants --json
```

credential不要の明示fixture例（空の新規DBを使用）:

```sh
python3 -m ai_trading.portfolio init --db data/phase8-demo.sqlite3 --json
python3 -m ai_trading.portfolio add --db data/phase8-demo.sqlite3 --symbol 7203 --side buy --quantity 100 --price 2500 --currency JPY --executed-at 2025-01-01 --json
python3 -m ai_trading.portfolio config --db data/phase8-demo.sqlite3 --file config/portfolio.example.json --json
python3 -m ai_trading.portfolio status --db data/phase8-demo.sqlite3 --market-provider jquants-fixture --market-fixture tests/fixtures/portfolio_market.json --market-lookback-days 7 --as-of 2025-01-08T09:00:00Z --json
python3 -m ai_trading.portfolio alerts --db data/phase8-demo.sqlite3 --market-provider jquants-fixture --market-fixture tests/fixtures/portfolio_market.json --market-lookback-days 7 --as-of 2025-01-08T09:00:00Z --json
```

fixtureは架空値であり、sourceは常に `fixture_jquants_v2:daily_close:<sha256>`。liveのsourceは `jquants_v2:daily_close:<sha256>` です。

| 引数 | 意味 |
| --- | --- |
| --market-provider manual | 従来の手動snapshot。既定 |
| --market-provider jquants | 明示live |
| --market-provider jquants-fixture | 明示fixture。--market-fixture必須 |
| --market-fixture PATH | FixtureTransport形式のローカルファイル |
| --market-lookback-days N | 取得対象の暦日数。既定90、2〜366 |
| --max-price-age-seconds N | 今回の評価だけstale閾値を上書き。DB設定は不変 |
| --as-of ISO_TIMESTAMP | 明示評価時点。省略時は取得後の現在時刻 |
| --snapshot PATH | manual限定。J-Quantsとの混用は拒否 |

永続的なstale設定は既存configのmax_snapshot_age_secondsを使います。CLIの一時上書きも同じ1〜31536000秒の範囲で検証します。

## 日足取得とsymbol

1. open positionの全currency/codeを先に検証。JPYのみ対応し、USD等が混在する場合は通信前に明示エラーです。手動snapshotでの複数通貨評価は従来どおりです。
2. 評価予定日のJST日付まで、指定lookback範囲のcalendarを1回取得。
3. calendar上の直近営業日を明示してmasterを銘柄別に取得。休日に暗黙の将来masterを採用しません。
4. 同じ範囲の銘柄別日足を取得し、最新の日付の行を選択。順序が逆のレスポンスでも日付で選びます。
5. previous_closeはその行の直前営業日にある未調整終値C。前営業日の行や終値がなければnull。さらに古い終値で埋めません。

4桁コード7203は取得時だけ72030へ変換し、台帳symbolは7203を維持します。130Aなど英字を含む日本の証券コードも扱います。5桁は末尾0の普通株式コードに限定します。会社名による検索はありません。同じ銘柄を7203と72030の両方で台帳へ登録すると別positionになるため、表記を統一してください。

日付masterにコードが存在しなければ `market_symbol_not_found`。別code・別Date・複数行は不正レスポンスとして拒否します。ProdCatは内国株券の011を必須とし、欠落やETF/REIT等の区分は拒否します。011だけでは優先株式等を区別できないため、対象codeも4桁または末尾0の5桁へ限定します。上場・廃止・コード変更の完全なライフサイクルは推定しません。

「最新」は指定lookbackと契約のデータ提供範囲で取得できた最新日です。全市場に対する即時性・完全性を保証しません。取得できた最新行のCが欠落していればmissingで、古い価格を最新として返しません。OHLCの不整合、重複日付、異なるcode、calendar外の行はエラーです。

## MarketSnapshotとJSON契約

既存6フィールドとschema_version=1を維持し、日足metadataを任意追加します。

| field | 内容 |
| --- | --- |
| symbol / currency | 台帳の銘柄表記 / JPY |
| price | 最新取得可能な未調整終値C、Decimal文字列 |
| previous_close | 直前営業日の未調整終値、またはnull |
| as_of | データ日を表すJST 00:00のUTC表現。終値の正確な発生・公表時刻ではない |
| source | live/fixture + daily_close + 原本bundle群のSHA-256 |
| market | TSE |
| data_date | YYYY-MM-DDのデータ日 |
| ingested_at | calendar/master/dailyの最終取得時刻、UTC |

as_ofの「日付代表時刻」は既存normalize_dailyのevent時刻規約と同じです。取得時刻を株価時刻として使わず、APIが提供しない公表時刻・取引終了時刻も捏造しません。ingested_atは別項目です。

追加metadataはmarket/data_date/ingested_atの3項目を揃えて扱います。従来の6項目だけの手動JSONも受理し、従来snapshotのto_dictは従来の6項目だけを返します。metadata付き手動JSONも読取り可能です。

envelopeのschema_version/generated_at/ok/command、dataのportfolio_summary/currency_summaries/positions/alerts/rules/as_ofは維持。positionsのsnapshotにmetadata、positionsとalertsにstale、alertsにvaluation_statusを加える追加互換変更です。generated_atは出力生成時刻、data.as_ofは評価時点、snapshot.as_of/data_dateは価格のデータ時点です。

人間向け表示にも「日足終値（リアルタイムではありません）」、data_date、as_of、sourceを表示します。

## Stale・PIT・企業行動

- age = 評価時刻 − snapshot.as_of。ageがmax_snapshot_age_secondsを超える場合はstale、境界ちょうどは有効。
- 日付代表時刻を使うため、日足では保守的な経過時間になります。休日を免除する自動補正はありません。週末を許容するならユーザーが閾値を明示設定します。
- 古いdata_dateの価格を今取得してもstale判定は変わりません。
- ingested_atが評価時刻より後ならfutureで評価しません。後日取得したデータを過去に既知だった価格として扱いません。
- Phase 7のbefore_transaction規則を維持します。同日の日中に買ったpositionでは、その日の日足代表時刻が約定前になり評価不能になる場合があります。正確な終値時刻がないため、このMVPでは条件を緩めません。
- stale/missing/future/before_transactionはcurrent_price、評価額、未実現損益をnull。該当通貨の評価合計と構成比も不完全として明示します。
- staleの場合、alertはstale=true、triggered=null。無効ルールのalertを新しく生成することはありません。条件未到達falseとは区別します。
- 調整済AdjCを原価・数量へ無断で適用しません。取得範囲内にAdjFactor != 1があれば `market_corporate_action_requires_review`、係数不明なら `market_adjustment_unknown`。
- この停止は保守的で、購入前の分割でも範囲内なら停止します。一方、lookbackより前の企業行動の完全性は検証できません。ユーザー台帳への企業行動反映は今回追加していません。

## エラー・通信・credential

認証不足、対象外通貨、存在しない銘柄、不正schema、calendar不完全、Providerエラーは固定codeのCLIエラー（exit 2、ok=false）です。部分的な成功reportやmanual/fixtureへのsilent fallbackはありません。

主なcode: market_auth_missing / market_currency_unsupported / market_symbol_invalid / market_symbol_not_found / market_provider_error / market_response_invalid / market_calendar_invalid / market_master_invalid / market_daily_invalid / market_response_too_large / market_pagination_limit / market_corporate_action_requires_review / market_adjustment_unknown。

既存TransportはHTTP要求ごとtimeout 30秒。adapterは各endpoint最大4ページ、1ページ4MiB、合計10000行、100 open positionsに制限します。レスポンスサイズ検査は既存Transport受信後なので、ダウンロード中のメモリ上限や全要求を通した総deadlineではありません。自動retryはありません。

APIキーは既存Transportのメモリと認証headerだけで使用します。adapterは例外本文、header、rawレスポンスをJSON・ログ・DB・configへ渡しません。今回キーの入力・保存・live smoke testはしていません。

## 検証と制約

```sh
PYTHONUTF8=1 PYTHONPATH=src python3 -m unittest discover -s tests -p test_portfolio_market.py -v
PYTHONUTF8=1 PYTHONPATH=src python3 -m unittest discover -s tests -v
```

テストはfake/fixtureだけで、live選択のテストもTransportをfakeへ差し替えます。CLI子プロセスではキーを環境から除去します。原本のDB非保存・設定不変もDBバイト比較で検証します。

2026-10-03、Windows / Python 3.12 / UTF-8で専用33件、全261件が成功（Phase 7の45件、既存181件、接続終了2件を含む）。ML・market・forward fixture、forward verify（5 entry）/reportも成功し、SQLite cleanup errorは0件でした。研究コード・SQLite schema・台帳データへの変更はありません。

実API疎通、契約ごとの履歴・遅延・rate limit・最新master提供範囲は未検証です。実APIでは日付範囲や権限不足によるProviderエラーが起こり得ます。呼出し間の永続cache・原本保存・通知・リアルタイム配信はありません。J-Quants対応範囲はJPY普通株式のみです。

## 参照仕様

- [公式J-Quants master仕様](https://jpx-jquants.com/ja/spec/eq-master): 日付指定、休日のmaster、4桁/5桁コード、資産種別。
- [公式商品区分仕様](https://jpx-jquants.com/ja/spec/eq-master/product-category): ProdCatの定義と収録範囲。
- [公式Python client v2](https://github.com/J-Quants/jquants-api-client-python/blob/main/jquantsapi/client_v2.py): /equities/bars/dailyのcode/from/to、v2認証方式。

仕様確認だけに公開ドキュメントを使用し、開発中にJ-Quants APIへ接続していません。
