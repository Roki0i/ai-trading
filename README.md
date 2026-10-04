# AI Trading Research

日本株の研究・分析基盤。Phase 0〜1（研究仕様・データ取得・PIT・品質検査）とPhase 2（非AIバックテスト）を実装。
Phase 4では固定価格特徴量によるロジスティック回帰の比較実験を実装。LLM、ニュース分析、実売買は未実装。

- [固定した研究仕様](docs/research-spec.md)
- [設定・データ構造・PIT・取得層の設計と制約](docs/data-architecture.md)
- [Phase 2の戦略・執行・指標・再現手順と制約](docs/backtest.md)
- [既定設定](config/research.json)

## APIキーなしで実行

Python 3.9以上。プロジェクトルートで実行する。実行時の外部依存なし。

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m ai_trading.cli --config config/research.json
```

2ページ・2銘柄の架空fixtureから、raw原本、processed JSONL、品質レポート、manifestを生成する。
`coverage_unverified` 警告は、過去の銘柄集合・営業日との突合が未実施であることを示す。
同じ設定・fixture・コード・Python環境で再実行すると同じ成果物を参照する。

原本から再生成するには、取得CLIが表示したmanifestのパスを指定する。

```sh
PYTHONPATH=src python3 -m ai_trading.replay experiments/manifests/<hash>.json --output data/replayed
```

`<hash>` は実際のファイル名に置き換える。原本改変やコード変更がある場合は再生を停止する。
必要なら `python3 -m pip install -e .` でインストール後、`research-data` コマンドも利用できる（ビルド依存の取得が必要な場合がある）。

## 実データ取得の準備

設定ファイルを複製し、`provider.mode` を `live` に変更して対象日を設定する。
APIキーは `JQUANTS_API_KEY` 環境変数で渡す。設定ファイルやGitにキーを書かない。
日足の読取専用アダプタを実装済みだが、実API疎通・データ契約・履歴完全性の確認は未実施。

取得時点が不明な過去データを、過去に利用可能だったと推測して扱わない。
公表時刻不明の日足は `published_at=null`、`available_at=ingested_at`。
過去のバックテスト用に使うには別途、当時の版・配信時刻と銘柄履歴の根拠が必要。

## Phase 3: 再現可能な実験基盤

固定したdevelopment / validation / holdoutと時点別universeの下で、3基準戦略の実験・保存・比較・再検証を行います。通常の開発コマンドはholdout結果へのアクセスを拒否します。

```sh
PYTHONPATH=src python3 -m ai_trading.experiment --help
```

[実験モデル、CLI、fixture比較例、保証範囲と制約](docs/experiments.md)を参照してください。

## Phase 4: AI/ML比較実験

PIT特徴量、train限定scaler、purge付きwalk-forward、4戦略の同条件比較を外部依存なしで実行できます。

```sh
PYTHONPATH=src python3 -m ai_trading.ml_fixture --store experiments/phase4-fixture
```

これは架空データによる動作検証です。収益率はAI性能の証拠にはなりません。
[設計、保存内容、リーク対策と残る課題](docs/ml-experiments.md)を参照してください。

## Phase 5: 市場データ・バックテスト堅牢化

実API/fixture共通Provider、raw再生成、明示営業日カレンダー、PIT銘柄履歴、分割・併合・配当、上場廃止時の停止、厳格な欠損方針、コスト比較、block bootstrapを追加しました。実市場の履歴完全性・実API疎通は未検証です。holdoutは開封しません。

```sh
PYTHONPATH=src python3 -m ai_trading.market_fixture --store experiments/phase5-verified
```

[設計・provenance・欠損/企業行動の処理・実市場で残る制約](docs/phase5-market-validation.md)

## Phase 6: Real-market validation and frozen forward test

出来高参加率、実データcoverage監査、設定freeze、append-only判断台帳、paper portfolio、再起動後の再生検証を追加しました。credentialのない環境ではfixtureを使用します。実API・実銘柄の完全性検証は未実施、holdoutは未開封です。

```sh
PYTHONPATH=src python3 -m ai_trading.validation --raw-root experiments/phase6-validation --sessions 2025-01-06 --symbols 90001 90002
PYTHONPATH=src python3 -m ai_trading.forward_fixture --store experiments/phase6-demo
PYTHONPATH=src python3 -m ai_trading.forward verify --root experiments/phase6-demo/fixture-forward
```

最初のコマンドの銘柄・日付はfixture専用です。credentialがある環境では実APIへ接続するため、契約範囲内の検証対象へ置き換えてください。demoは架空developmentデータの再生であり、未観測期間のForward成績ではありません。既存のfreeze先への再作成は拒否します。

[Phase 6の実装・検証結果・運用手順・残る制約](docs/phase6-forward-test.md)

## Phase 7: Portfolio Assistant Core

研究用paper portfolioとは独立した、実保有株の取引台帳・平均取得原価・損益・通貨別構成比・設定済み閾値の通知を追加しました。自動売買、broker注文、LLM、Raphael依存はありません。価格は手動JSONで入力し、未取得・古い価格は評価不能として明示します。

```sh
export PYTHONPATH=src
export PYTHONUTF8=1
python3 -m ai_trading.portfolio init --json
python3 -m ai_trading.portfolio add --symbol 7203 --side buy --quantity 100 --price 2500 --currency JPY --executed-at 2025-01-01 --json
python3 -m ai_trading.portfolio config --file config/portfolio.example.json --json
python3 -m ai_trading.portfolio status --json
python3 -m ai_trading.portfolio alerts --json
```

専用DBは`data/user-portfolio/portfolio.sqlite3`。既存ファイルへのinitは拒否します。実価格の自動取得はなく、snapshotなしでは評価額・損益率がnullになります。閾値は既定で無効で、例示設定を明示適用します。`--json`はschema_version / generated_at付きの機械可読出力、省略時は人間向けの表示です。

Windows PowerShellでは`$env:PYTHONPATH="src"`、`$env:PYTHONUTF8="1"`を設定し、`python3`を`py -3.12`へ置き換えます。

[Windows/Macの手順・台帳とJSON契約・計算仕様・制約](docs/portfolio-assistant.md)

## Phase 8: Market Data Integration

Portfolioから既存read-only J-Quants基盤の日足終値を取得できます。リアルタイム株価ではありません。JPYの普通株式に限定し、日付付きmaster・calendar・日足品質を検証します。価格のデータ日と取得時刻を分離し、古い価格や取得時刻より過去の評価は明示的に評価不能とします。

```sh
PYTHONUTF8=1 PYTHONPATH=src python3 -m ai_trading.portfolio status --market-provider jquants --json
PYTHONUTF8=1 PYTHONPATH=src python3 -m ai_trading.portfolio alerts --market-provider jquants --json
```

liveはユーザーが環境へ設定した`JQUANTS_API_KEY`が必要です。未設定時はエラーで停止し、fixtureへ自動fallbackしません。既定のmanualは通信せず、credential不要の`jquants-fixture`も明示選択できます。実API疎通は未検証です。

[fixture起動手順・adapter・JSON互換性・stale/企業行動の方針・制約](docs/market-data-integration.md)

## Phase 9: Portfolio Decision Support

実保有Portfolio・MarketSnapshot・設定済みalertsから、条件到達を構造化Assessmentで返します。利益・損失・日次変動・集中の条件をすべて保持し、根拠となる値・閾値・比較演算・価格時点を返します。stale/missing/blockedは評価不能を明示し、通貨を合算しません。売買推奨、broker注文、自動売買、LLM判断、Raphael接続はありません。

```sh
PYTHONUTF8=1 PYTHONPATH=src python3 -m ai_trading.portfolio assess
PYTHONUTF8=1 PYTHONPATH=src python3 -m ai_trading.portfolio assess --json
```

既定は通信しないmanualで、snapshotなしでは価格をmissingと表示します。Phase 8の明示fixture/live選択と`--snapshot`を利用できます。既存DBやstatus/alerts JSONは変更しません。

[Assessmentモデル・severity・CLI・JSON契約・制約](docs/portfolio-decision-support.md)

## Phase 10 MVP: Earnings / Corporate Events

決算・配当・株式分割の予定を、明示したローカルfixtureからAssessmentへ追加します。予定日までの日数、鮮度、設定閾値によるevent flagsと構造化根拠を返します。stale/unknownのflagは抑止し、売買判断や数量補正は行いません。新規外部API・News・Raphaelには未接続です。

```sh
PYTHONUTF8=1 PYTHONPATH=src python3 -m ai_trading.portfolio assess --events-fixture tests/fixtures/portfolio_events.json --as-of 2025-01-08T09:00:00Z --json
```

[イベント契約・設定・fixture・鮮度・CLI・制約](docs/corporate-events.md)
