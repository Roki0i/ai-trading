# Phase 11: Real Data Validation & Provider Integration

既存のread-only J-Quants経路を再利用し、HTTP境界からPortfolio Assessmentまでの解析・失敗・provenanceを検証します。日足終値を扱い、リアルタイム株価とは表現しません。News・broker・注文・自動売買・Raphaelは追加しません。

## 今回の実API検証結果

実行プロセスの`JQUANTS_API_KEY`は未設定でした。値を表示・保存せず存在だけ確認しました。**credentialなしのため実API smokeは未実施、実J-Quants疎通は未検証**です。fixtureをlive成功の代わりにしません。ユーザーの実Portfolio DBも変更していません。

通常検証はmock HTTPと明示fixtureだけです。公式公開ドキュメントを調査しましたが、APIへの認証要求・キー取得・新規契約は行っていません。

## 既存Providerと構成

```text
portfolio status / alerts / assess --market-provider jquants
  → prepare_provider: 通貨・code事前検証 / credential必須
  → JQuantsSnapshotProvider
      → 既存JQuantsTransport: GET限定 / endpoint allowlist / timeout 30秒
      → calendar / 日付指定master / code・期間指定daily
      → 既存coverage / validate_daily_row / inspect_daily
  → MarketSnapshot
  → Portfolio Engine → Assessment → 既存events拡張
```

HTTP処理をPortfolioへ重複実装しません。`providers.py`は通信、`portfolio/jquants.py`は既存市場検証と変換、`portfolio/market.py`はsnapshot境界です。`validation.py`のcoverageは原本hash、master日付/code、日足を検証します。`portfolio/events.py`は引き続きfixture専用です。

研究用`validation.smoke`には従来のcredential不在時fixture動作があります。Phase 0〜6回帰を維持するため変更せず、Phase 11のlive確認には使用しません。Portfolio CLIのjquants指定ではcredential必須で、silent fallbackはありません。

## 対応市場データと時点

- JPY普通株式。4桁codeは普通株式5桁codeへ変換し、台帳symbolは維持。
- 指定lookback範囲で取得可能な最新daily closeを採用。直前営業日の行が存在するときだけprevious_closeを取得。
- 最新行の価格欠落を古い価格で補完しない。空の日足はmissing、存在しないmasterは明示error。
- masterのcode/日付、calendarの日付網羅、OHLC/出来高/調整係数を既存検証へ通す。
- corporate action未解決の評価停止は維持。日足調整係数からイベントを生成しない。

MarketSnapshotの契約・JSON schema_version=1は変更しません。

| 値 | 意味 |
| --- | --- |
| data_date | Providerの日足データ日 |
| as_of | 同日のJST 00:00という既存の日付代表時刻。実際の引け・公表時刻ではない |
| ingested_at | 必要な各レスポンスを受信した最終時刻 |
| source | jquants_v2:daily_close:原本bundle hash、または明示fixtureの接頭辞 |
| 利用可能時刻 | 取得によって観測できた時刻が根拠。過去の公表時刻を推測しない |
| 公表時刻 | 日足APIから確定できない。研究Observationではpublished_at=null、available_at=ingested_at |

Portfolioでは原本・header・キーをDBへ保存しません。source hashは取得内容の識別であり、原本を保存しないため後日の完全再生保証ではありません。

staleは評価時刻からsnapshot.as_ofまでの経過時間が設定閾値を超えた場合です。新しい取得時刻で古い日足をfreshにしません。future ingestionやbefore_transactionも従来どおり評価停止します。価格依存値はnull、対応ルールは抑止。休日の自動補正や契約遅延の自動回避はしません。

## Error policy

共有Transportに`JQuantsError(RuntimeError)`を追加しました。既存研究側のRuntimeError捕捉と既存メッセージを維持し、Portfolioは例外文字列の解析ではなくkind/http_statusで判別します。HTTP失敗時も応答を閉じます。

| ケース | Portfolio error.code |
| --- | --- |
| credential未設定/空白 | market_auth_missing（通信なし） |
| 401 | market_auth_failed |
| 403 | market_forbidden |
| 429 | market_rate_limited |
| その他HTTP status | market_http_error |
| timeout、URLError内のTimeoutError | market_timeout |
| network/reset/不完全read | market_network_error |
| その他Provider例外・credential反射 | market_provider_error |
| 不正JSON/schema | market_response_invalid |
| master日付/code不整合・必須日付欠落 | market_master_invalid |
| masterに銘柄なし | market_symbol_not_found |
| calendar不完全 | market_calendar_invalid |
| 日足なし | 成功応答内でmissing。実価格成功とは扱わない |
| 古い日足 | 成功応答内でstale、評価抑止 |

エラーはexit 2、ok=falseの既存envelopeで返します。header・body・例外の生文字列はCLI JSONへ流しません。全失敗で自動retryなし。特に429は即停止です。

既存の各endpoint最大4ページ、各ページ4MiB（Transport受信後の検査）、10000行、保有100銘柄の上限は維持します。取得全体のdeadlineやダウンロード中のメモリ上限は今回追加していません。

## Corporate Eventsの公式仕様調査

以下は公開仕様上の可否です。現在の利用者の契約・権限・実レスポンスはcredential不在により確認できません。**新しいイベントendpointを既存allowlistへ追加せず、実イベントadapterは今回未実装**です。Phase 10のfixture契約はそのまま維持します。

| 種別 | 公式の選択肢と不足点 | 今回 |
| --- | --- | --- |
| earnings | /equities/earnings-calendarは3・9月期会社の限定データ。発表日時fieldがなく、取得時刻をannounced_atに流用できない。/fins/earnings-dateには予定変更・未定の履歴がある | 契約範囲と履歴・未定への変換仕様を確認してからadapter化。予定が取れないことを「予定なし」と断定しない |
| dividend | /fins/dividendは通知日時、基準日・権利落日・支払予定日、訂正/削除の参照関係を提供。Premium対象 | 権限未確認。event_dateがどの日を指すか、訂正/削除の解決が必要。単純変換は未実装 |
| stock_split | 既存dailyのAdjFactorは予定・発表履歴を持つ独立イベントではない | 価格変化や係数から推測しない。独立した公式イベントProviderが必要 |

決算・配当は「J-Quantsにデータがない」という結論ではありません。利用可能な公式APIは存在しますが、現在の契約で安全に利用できることは未確認です。3・9月期のAPIでannounced_at不明なら将来の変換でもnull/unknownとし、取得時刻で鮮度を偽装しません。分割等の独立データを別サービスから取得する契約は行いません。

参照した公式仕様:

- [決算予定（3・9月期限定）](https://jpx-jquants.com/ja/spec/eq-earnings-cal)
- [決算予定の公表履歴](https://jpx-jquants.com/ja/spec/fin-earnings-date)
- [配当通知](https://jpx-jquants.com/ja/spec/fin-dividend)
- [プラン別取得範囲](https://jpx-jquants.com/ja/spec/data-spec)
- [日足の項目・欠測・調整係数](https://jpx-jquants.com/ja/spec/eq-bars-daily)

## CLIとcredential

```powershell
$env:PYTHONPATH = "src"
$env:PYTHONUTF8 = "1"
py -3.12 -m ai_trading.portfolio assess --market-provider jquants --json
```

既存status/alertsも同じmarket-provider引数を使います。イベントは`--events-fixture`のままです。`--events-provider jquants`は追加していません。キーはユーザーが環境へ手動設定し、値をコマンドログ・docs・Git・JSON・DBへ書きません。

credentialなしでも通常testと明示fixtureは動作します。既定manualから勝手に通信しません。

```powershell
py -3.12 -m ai_trading.portfolio assess --market-provider jquants-fixture --market-fixture tests/fixtures/portfolio_market.json --market-lookback-days 7 --as-of 2025-01-08T09:00:00Z --json
```

上記fixtureは過去の架空データです。対応日時以前のテスト取引がある専用DBを`--db`で指定してください。fixture/liveでdomain JSONの形は同一、sourceと取得内容は異なります。

## credential設定後の少数live smoke手順（今回は未実施）

1. 環境にキーがあることを値を出さず確認。不在なら停止。
2. Temp配下へ専用DBを作り、1銘柄（例7203/JPY）の架空取引を登録。実保有DBは使わない。
3. 既存assessに`--db <Temp DB> --market-provider jquants --market-lookback-days 7 --json`を指定。評価時刻は現在時刻を使用し、過去のas_ofへ取得時刻を繰り上げない。
4. 通常3要求（calendar/master/daily）。各endpoint最大4ページで1銘柄の上限12要求。別銘柄への連続試行や失敗時のretryは行わない。
5. 401/403/429/timeout/不正データ等なら即停止。契約遅延を避けるために日付やstale閾値を自動変更しない。
6. symbol/date/endpoint category/successまたはfailure/freshnessだけを報告。rawや認証headerは報告・保存しない。

Freeの提供遅延や参照期間制限により現在日付のmaster/calendar取得が拒否される可能性があります。これは取得成功と偽装せず、契約条件を確認してからユーザーが取得期間を選ぶ必要があります。

## Testsと残る制約

通常テストは実Transportのurlopenをmockし、架空credentialでHTTP→既存adapter→Engine→Assessment→CLIを検証します。API未接続のmock成功をlive smoke成功とは呼びません。DBはTemporaryDirectory内のみで、読取り前後のバイト一致も確認します。

実市場での認証、契約遅延、最新master・calendar・日足の整合性、rate limit、企業行動の網羅性は未検証です。既存の研究PIT/holdout/forward/append-onlyとPortfolio台帳・SQLite schemaは変更しません。


最終検証（Windows / Python 3.12 / PYTHONUTF8=1）:

- Phase 11専用25件成功。
- Python全352件成功。SQLite cleanup errorなし。
- 個別回帰: Phase 7=45件、Phase 8=33件、Phase 9=41件、Phase 10=25件、すべて成功。
- market / ML / forward fixture成功。forward verify=5 entry、report=fixture / 5 sessions / insufficient_forward_history。
- git diff --check成功。workflow成果物とログはTemp配下。secret・DB・experiment/holdout artifactの差分混入なし。
- live APIはcredentialなしで未実施。キーの値の入力・表示・保存なし。既存Portfolio DBの変更なし。
