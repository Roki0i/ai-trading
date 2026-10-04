# Phase 9: Portfolio Decision Support

実保有Portfolio、MarketSnapshot、ユーザーが設定したルールをまとめ、現在の条件到達とデータ品質をAssessmentとして返します。売買推奨度ではなく、検証可能な値・比較演算・閾値を返す機能です。自動売買、broker注文、LLM判断、Raphael接続はありません。

## 構成

```text
Portfolio SQLite → CLI → transactions / RuleConfig
                            ↓
                      既存replay / status ← MarketSnapshot
                            ↓                    ↑
                      Portfolio State      明示選択したProvider
                            ↓                    ↓
                      既存alerts           collect_snapshots
                            ↓              （企業行動の停止を分離）
                      assess(report, generated_at, blocked)
                            ↓
                      JSON / 人間向け表示
```

`assessment.py`は純粋なdomain層で、SQLite・Provider・CLI・現在時計へ依存しません。Phase 7のstatus出力とそのrulesを入力し、既存alertsの結果を利用します。新しい損益計算や閾値判定は作りません。呼出し元が生成時刻を明示し、同じ入力から同じ出力を得ます。入力reportは変更しません。

`assessment_market.py`は取得境界です。各保有銘柄のsnapshotを取得して固定し、既知の企業行動停止コードだけを銘柄別に収集します。認証・通信・不正レスポンス・銘柄不明などのエラーは伝播してCLI全体がexit 2になります。価格や調整係数を推測しません。

J-Quantsの`prepare_provider`には既定trueの`prefetch`を追加しています。assessだけはfalseで取得境界へ渡します。通信前の全銘柄・通貨検証、認証、明示fixture選択、品質検証は維持します。status/alertsは従来どおり事前取得し、企業行動時にはエラーで停止します。

取得完了後に評価時刻を決めます。`--as-of`指定時はその時刻を維持し、後日取得したデータを過去に使えるようにはしません。

## AssessmentとJSON契約

assess成功時は次のtop-level構造です。既存status/alertsの`data` envelopeは変更していません。assessには`data`を重複させず、`portfolio`と`assessments`を直接置きます。失敗時は従来どおりschema_version/generated_at/ok=false/command/error.codeです。

```json
{
  "schema_version": 1,
  "generated_at": "2025-01-08T09:00:00.000000Z",
  "as_of": "2025-01-08T09:00:00.000000Z",
  "ok": true,
  "command": "assess",
  "portfolio": {
    "weight_basis": "same_currency_open_positions",
    "currencies": []
  },
  "assessments": []
}
```

- generated_at: 出力生成時刻（UTC）。各assessmentにも同じ値を保持。
- as_of: 評価時刻（UTC）。価格の時点と区別。
- assessments: 数量が正の保有だけをsymbol/currency順で返す。全売却済み銘柄の実現損益は通貨集計に残すが、価格欠落として数えない。
- 各assessment: symbol, currency, metrics, market_data_status, market_snapshot, triggered_rules, assessment_flags, severity, reasons, generated_at。
- metrics: quantity, average_cost, total_cost, current_price, market_value, unrealized_pnl, unrealized_pnl_pct, realized_pnl, daily_move_pct, portfolio_weight。
- 金額・数量・比率はPhase 7同様のDecimal文字列。比率は割合で、`"0.2"`は20%。設定の`20`とは単位が異なる。整数件数はJSON number、評価不能はnull。
- market_snapshot: as_of, source, market, data_date, ingested_at, evaluated_at, max_snapshot_age_seconds。旧形式の手動snapshotにないmetadataはnullで、データ日を捏造しない。価格はmetricsだけに置く。
- triggered_rules: 発火した既存ルールのtype名。assessment_flags: 下表の正規化名。複数発火はすべて保持。
- reasons: 設定された全ルールの評価根拠。未到達false、評価不能null、到達trueを区別。無効ルールの根拠は作らない。

ルール根拠の例:

```json
{
  "kind": "rule",
  "rule": "take_profit_threshold",
  "flag": "take_profit_threshold",
  "value": "0.2",
  "threshold": "0.2",
  "comparison": ">=",
  "triggered": true,
  "evaluation": "evaluated",
  "code": "configured_threshold_reached",
  "data_date": "2025-01-08",
  "source": "fixture",
  "as_of": "2025-01-08T09:00:00.000000Z"
}
```

構成比の根拠は同一通貨の評価が完全な場合にだけ生成可能です。`portfolio.weight_basis`と全銘柄のmetrics/market_snapshotから分母と価格時点を確認できます。構造化値により外部clientが説明を生成でき、自然言語のreasonだけには依存しません。

## ルールとseverity

閾値は既存RuleConfig/configコマンドを使用します。既定はすべて無効。コードに投資判断用の閾値は固定しません。

| 設定 | 既存type | flag | 比較 | 発火時severity |
| --- | --- | --- | --- | --- |
| take_profit_pct | take_profit_threshold | take_profit_threshold | 含み損益率 >= 閾値 | info |
| loss_warning_pct | loss_warning_threshold | loss_warning | 含み損益率 <= 負の閾値 | warning |
| daily_move_pct | daily_move_threshold | daily_move_warning | abs(日次変動率) >= 閾値 | warning |
| max_position_weight_pct | concentration_threshold | concentration_warning | 同一通貨内構成比 > 閾値 | warning |

利益・損失・日次変動は閾値ちょうどで発火し、構成比はちょうどでは発火しません。既存alertsと一致します。日次変動は正負両方を評価し、valueの符号を保持します。

severityは最大の重要度を使用しますが、flagsを一つに潰しません。

- info: データ有効・警戒条件なし、または利益閾値だけ到達。
- warning: 損失・日次変動・集中条件が到達、stale/missing、または有効化済みルールが評価不能。
- critical: blocked。企業行動または時点整合性により評価を停止。

criticalは損失額の大きさや売買推奨ではありません。domainから推奨文・注文・BUY/SELLシグナルは生成しません。

## stale / missing / blocked

Phase 8の鮮度・PIT規則を維持します。

| 状態 | 条件 | 価格依存の値 |
| --- | --- | --- |
| fresh | 既存valuation_status=ok | 計算可能なものを返す |
| stale | 評価時刻 − snapshot.as_of が設定秒数を超過 | null |
| missing | snapshotなし | null |
| blocked | corporate action停止、係数不明、future、before_transaction | null |

staleの境界ちょうどは有効。新しいingested_atで古い価格をfreshにしません。data_date、as_of、ingested_at、評価時刻、閾値を保持します。日足はリアルタイムではなく、休日を除く自動補正もありません。

データ問題のreasonsはkind=market_data、status、code、各時点metadataを持ちます。企業行動では`market_corporate_action_requires_review`または`market_adjustment_unknown`を返します。snapshot自体が成立しないため、その価格・出所・日付はnullです。推測補正しません。

freshでもprevious_closeがない場合はdaily_move_pct=nullで、日次変動ルールのcodeは`previous_close_unavailable`。利益ルールなど評価できる項目は残します。同一通貨に評価不能銘柄がある場合は全構成比がnullで、codeは`currency_valuation_incomplete`。その他の価格依存ルールは`market_data_unavailable`として発火を抑止します。0やfalseへ補完しません。

## 通貨別summary

portfolio.currenciesの各要素はcurrency, number_of_positions, total_cost, market_value, unrealized_pnl, realized_pnl, stale_position_count, missing_price_count, blocked_position_count, concentration_flags。

number_of_positionsは保有中の銘柄数です。データ品質の3件数は排他的に数えます。concentration_flagsはその通貨で集中閾値を超えたsymbolの配列です。価格不足の通貨ではmarket_valueとunrealized_pnlをnullにします。原価・実現損益は台帳で確定しているため残します。JPYとUSDの合算やFX換算はありません。

## CLI

PowerShell例（既存のPortfolio DBを使用）:

```powershell
$env:PYTHONPATH = "src"
$env:PYTHONUTF8 = "1"
py -3.12 -m ai_trading.portfolio assess
py -3.12 -m ai_trading.portfolio assess --json
py -3.12 -m ai_trading.portfolio assess --snapshot prices.json --json
```

既定manualでは通信しません。snapshotなしではmissingを明示します。従来のinit/add/transactions/config/status/alertsは継続して使用できます。`--db`はコマンド前後どちらでも使用可能です。人間向け表示は価格・含み損益率・条件・鮮度・通貨集計を表示し、機械向けは必ず`--json`を使用します。

既存Phase 8の架空fixtureを利用する例（2025-01-01以前の7203/JPY取引があるテストDBを指定）:

```powershell
py -3.12 -m ai_trading.portfolio assess --db <テストDBのパス> --market-provider jquants-fixture --market-fixture tests/fixtures/portfolio_market.json --market-lookback-days 7 --as-of 2025-01-08T09:00:00Z --json
```

明示liveの起動例:

```powershell
py -3.12 -m ai_trading.portfolio assess --market-provider jquants --json
```

liveは既存の`JQUANTS_API_KEY`だけを使用します。未設定なら通信せずエラーになり、fixtureへfallbackしません。キーを引数・ログ・JSON・DB・Gitへ保存しません。本開発の検証はfake/fixtureだけで実APIに接続しません。

`--max-price-age-seconds`は評価時だけの上書きで、保存済み設定を変更しません。通常設定は`portfolio config --file config/portfolio.example.json`等で明示設定します。

## 永続化・互換性・制約

Assessmentは保存せず都度計算し、DB schema/version/transaction ledgerは変更しません。status/alerts JSON、MarketSnapshot形式、設定形式、Windowsの接続終了処理を維持します。Phase 0〜6研究コード、PIT、freeze、append-only、holdout保護を変更しません。

J-QuantsはPhase 8と同じJPY普通株式の日足限定です。手動snapshotは複数通貨を使用できます。企業行動の停止は取得範囲内に限られ、購入前の分割でも停止する保守的仕様です。Corporate actionを台帳へ反映する機能や新しい価格鮮度推定はありません。live API疎通や市場履歴の完全性は未検証です。

News / Sentiment / Earnings / Fundamentals / broker / 自動売買 / 通知 / Web UI / FX / 税 / 最適化 / ranking / Raphael / LLMは対象外です。

## 検証

```powershell
$env:PYTHONPATH = "src"
py -3.12 -X utf8 -m unittest discover -s tests -p test_portfolio_assessment.py -v
py -3.12 -X utf8 -m unittest discover -s tests -v
```

専用テストは閾値境界、単独・複数flag、severity、構造化根拠、欠測/古い値/企業行動/時点不整合、複数銘柄・通貨、DB再オープンと読取り不変、CLI/JSON、明示fixture、fake live Transport、既存status/alerts互換を対象とします。通常テストから実APIを呼びません。


2026-10-04、Windows / Python 3.12で検証:

- Phase 9専用41件成功。
- Python全302件成功（Phase 7の45件、Phase 8の33件、既存183件を含む）。SQLite cleanup errorなし。
- Phase 7の45件、Phase 8の33件は別実行でも成功。
- market fixture、ML fixture、forward fixture、forward verify（5エントリー）、forward report成功。reportはfixture / 5 sessions / insufficient_forward_historyで、実市場成績として扱わない。
- 別プロセスのCLI init/add/config/assessをTemp DBで実行。人間向け表示・JSON・評価前後のDBバイト不変を確認。
- 初回の全体実行では親だけの`-X utf8`指定により、子プロセスがcp932となり既存Experiment CLIテスト1件が失敗。README指定の`PYTHONUTF8=1`を子プロセスにも継承して全体成功。これに関する本番コード変更は行っていない。
- fixture生成物と検証ログはTemp配下のみ。実API・holdout artifactは使用しない。commit/pushは未実施。
