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
