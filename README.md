# AI Trading Research

日本株の売買戦略を、同じ期間・同じ執行条件で比較するためのPythonツールです。保有株を記録したい場合は、別のCLIで取引を登録し、平均取得原価や通貨別の損益を確認できます。

## サンプルデータで試す

Python 3.9以上を使います。リポジトリのルートで次を実行すると、架空の2銘柄から日足データと品質レポートを生成します。APIキーは不要です。

```sh
export PYTHONPATH=src
python3 -m ai_trading.cli --config config/research.json
```

Windows PowerShellでは `$env:PYTHONPATH="src"` と `$env:PYTHONUTF8="1"` を設定し、`python3` を `py -3.12` に読み替えてください。

## 売買戦略を比較する

[バックテスト](docs/backtest.md)では売買手数料や約定条件をそろえて戦略を比較します。[機械学習の実験](docs/ml-experiments.md)では、価格から作った特徴量を使い、ロジスティック回帰と基準戦略を比較できます。

```sh
python3 -m ai_trading.experiment --help
python3 -m ai_trading.ml_fixture --store experiments/ml-demo
```

サンプルで得られる収益率は動作確認用です。過去の株価を使う場合は、その時点で取得できたデータと銘柄を使ってください。[研究条件](docs/research-spec.md)、[データの取得時点と品質チェック](docs/data-architecture.md)、[実験の再現手順](docs/experiments.md)に条件をまとめています。

株式分割・配当を含む検証は[市場データの扱い](docs/phase5-market-validation.md)、条件を固定して以後の判断を記録する手順は[フォワードテスト](docs/phase6-forward-test.md)を参照してください。

## 保有株を記録する

次はローカルの取引台帳を作り、購入を1件記録する例です。台帳の保存先は `data/user-portfolio/portfolio.sqlite3` です。

```sh
python3 -m ai_trading.portfolio init --json
python3 -m ai_trading.portfolio add --symbol 7203 --side buy --quantity 100 --price 2500 --currency JPY --executed-at 2025-01-01 --json
python3 -m ai_trading.portfolio status
```

評価額や含み損益を出すには価格データが必要です。手元の価格データを使う方法と、J-Quantsの日足終値を使う方法があります。J-Quantsへの接続には環境変数 `JQUANTS_API_KEY` が必要です。実APIでの疎通は未検証です。

- [取引の登録と損益計算](docs/portfolio-assistant.md)
- [価格の指定とJ-Quantsの設定](docs/market-data-integration.md)
- [損益や保有比率が設定値に達したか確認する](docs/portfolio-decision-support.md)
- [決算・配当予定](docs/corporate-events.md)と[ニュース](docs/news-integration.md)をサンプルJSONから読み込む

## テスト

```sh
python3 -m unittest discover -s tests -v
```
