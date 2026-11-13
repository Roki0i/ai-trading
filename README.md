# AI Trading Research

日本株の売買戦略を同じ期間・執行条件で比較し、保有株の取引台帳をローカル管理するPythonツールです。

## 試す

Python 3.9以上。以下は**架空のデータ**で動くサンプルです。

```bash
export PYTHONPATH=src
python3 -m ai_trading.cli --config config/research.json
python3 -m ai_trading.experiment --help
python3 -m ai_trading.ml_fixture --store experiments/ml-demo
```

Windows PowerShellでは `$env:PYTHONPATH="src"` を設定し、`python3` を `py -3.12` に読み替えてください。

## 保有株の記録

```bash
python3 -m ai_trading.portfolio init --json
python3 -m ai_trading.portfolio add --symbol 7203 --side buy --quantity 100 --price 2500 --currency JPY --executed-at 2025-01-01 --json
python3 -m ai_trading.portfolio status
```

台帳は `data/user-portfolio/portfolio.sqlite3` に保存されます。評価額の取得には別途価格データが必要です。J-Quants連携には `JQUANTS_API_KEY` が必要で、実API疎通は未検証です。

## 少額資金のバックテスト（架空データ）

`config/small-capital-demo.json` は元本20万円、1銘柄最大20%、初期資金の40%を現金として残す検証例です。
`initial_cash` を `100000` / `200000` / `300000` に変更すると、同じ入力条件で資金規模を比較できます。

```bash
export PYTHONPATH=src
python3 - <<'PY'
import json
from pathlib import Path
from ai_trading.backtest import BacktestConfig, run_backtest
from ai_trading.providers import normalize_daily
pages = json.loads(Path("tests/fixtures/backtest_pages.json").read_text())
cfg = BacktestConfig(**json.loads(Path("config/small-capital-demo.json").read_text()))
result = run_backtest(normalize_daily(pages["pages"], source="fixture"), cfg)
print(result["metrics"])
print(result["snapshots"][-1])
PY
```

100株単位で予算に収まる銘柄がなければ売買しません。`lot_size=1` は計算上の単元未満株を模擬するだけで、
実際の証券会社の約定方式・スプレッドを保証しません。実市場データ・利益検証・実発注には接続していません。
詳細は [バックテスト仕様](docs/backtest.md) を参照してください。

## テスト

```bash
python3 -m unittest discover -s tests -v
```

## 詳細・注意事項

- [バックテストと検証条件](docs/backtest.md) / [研究仕様](docs/research-spec.md) / [ML実験](docs/ml-experiments.md)
- [取引・損益管理](docs/portfolio-assistant.md) / [市場データ連携](docs/market-data-integration.md)
- [株式分割・配当の扱い](docs/phase5-market-validation.md) / [フォワードテスト](docs/phase6-forward-test.md)
- サンプル成績は実市場での利益を保証しません。価格の取得時点・生存者バイアス・手数料・将来情報の混入に注意してください。
