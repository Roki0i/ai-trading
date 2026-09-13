# AI Trading Research

日本株の研究・分析基盤。現在はPhase 0〜1（研究仕様・データ取得・PIT・品質検査）のみ。
予測モデル、LLM、バックテスト、実売買は未実装。

- [固定した研究仕様](docs/research-spec.md)
- [設定・データ構造・PIT・取得層の設計と制約](docs/data-architecture.md)
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
