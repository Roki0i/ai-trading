# Phase 3: 再現可能な実験基盤

Phase 2の `run_backtest` を呼ぶオフライン研究基盤。機械学習、LLM、ニュース、実売買APIは含まない。追加依存なし。

## アーキテクチャ

`study.json`（固定期間・固定universe）→ `ExperimentStore.run`（アクセス判定・入力制限）→ Phase 2バックテスト → 実験別成果物とSHA-256 manifest → `read` / `verify` / `compare`。

1 store = 1 study。`init` は既存studyと異なる定義を拒否する。development < validation < holdout の重複しない期間を必須とする。sessionsは選択した期間内だけを許可し、分割をまたぐポートフォリオ・学習状態は持ち越さない。各期間は同じ初期資金から開始する。比較ではsessionsも完全一致を要求する。

universeは開発開始前に既知だった固定集合と、その `known_at`・`evidence` を指定する。将来の銘柄マスターを照会する処理はない。動的構成銘柄や上場廃止対応は対象外。fixtureのevidenceは合成データの宣言であり実市場の在籍証明ではない。

## Experimentモデル

`src/ai_trading/experiment.py` のfrozen dataclassに次を記録する。

| フィールド | 内容 |
|---|---|
| experiment_id / created_at | 実行ごとに異なるUUID / UTC作成時刻 |
| strategy / strategy_parameters | 戦略名 / lookback・top_n |
| universe_definition | static_point_in_time、symbols、known_at、evidence |
| evaluation_period | split名、開始日、終了日。実際のsessionsはconfigに保存 |
| initial_capital | 初期資金 |
| fee_settings / slippage_settings | bps手数料・固定手数料 / bpsスリッページ |
| input_data_hash / config_hash | 実行用入力 / study・バックテスト設定・seedのSHA-256 |
| git_commit | 実行環境のHEAD。Git不在ならnull |
| random_seed | 明示的整数。現在の3戦略は乱数未使用 |
| metrics / result_hash | 6指標 / 成否・失敗理由を含む結果全体のSHA-256 |
| status / failure_reason | completedまたはfailed / 例外型と理由 |

lot_size、PIT設定、全sessionsなども `config.json` に保存する。UUIDと時刻は結果hashには含めず、同一実行を再実行しても結果hashは同じになる。再実行も新しい実験として残す。

## 保存と再検証

通常実験は `<store>/research/<experiment_id>/`、holdoutは `<store>/holdout/<experiment_id>/` に保存。

- `experiment.json`: 上記モデル
- `config.json`: 全実行設定、study、split、seed
- `inputs.json`: 実際に利用可能な期間・universeに制限したObservation。元値と時刻・revision・sourceを保持
- `daily_equity.json`: 日次資金、保有数、評価価格、欠損・古い評価価格、資産額
- `orders.json` / `fills.json`: 注文判断時刻、数量、約定・拒否・取消状態、価格、費用、約定後資金
- `trade_history.json`: 各約定と元注文を結ぶ取引台帳（FIFO損益計算や往復取引集計ではない）
- `metrics.json` / `outcome.json`: 指標 / バックテスト全結果または失敗理由
- `environment.json`: Python・実装・OS・architecture、Git HEAD/status、パッケージ全Pythonソースとそのhash、実行方針
- `<sha256>.manifest.json`: 上記全成果物のhash一覧。manifest自体もファイル名hashで検証

`show ID` だけで入力、設定、コード、取引、資産推移を取得できる。取引台帳から注文判断日と利用可能価格へ遡り、保存された戦略ソースと設定で判断・数量・費用を再計算できる。`verify ID` は全ファイル、内部hash、モデル整合性、コード・実行環境を検証し、保存入力で再実行して全結果と派生成果物を比較する。失敗実験も同じ例外型・理由になるか検証する。

Gitの作業ツリーに未コミット変更がある場合もHEADだけに依存せず、実行したソースを保存する。コード変更後のverifyは拒否するため、保存ソースを別環境に復元し、記録したPython・OS・architectureを合わせて実行する。保存ソースを自動実行する機能はない。

実行設定・PIT・価格品質・バックテストの失敗はfailed実験として保存する。CLIは保存済みモデルを出力して終了コード1を返す。引数解析、入力ファイル自体のhash破損やJSON解析、保存先I/O障害、プロセス強制終了は実験開始前/保存不能なエラーであり、完了レコードの保存保証外。未完成ディレクトリは有効manifestがないためreadが拒否する。

## Leakage対策と保証範囲

- Phase 3はobserved PIT限定。historical復元と推定availabilityを拒否する。
- 別split、評価末尾以後のevent/availability/ingestion、別universeのデータは実行前に除外。失敗実験への入力保存でも別splitは除外する。
- 各判断時点でPhase 2のstrict-before PIT選択を再実行。訂正は利用可能になった時点以後だけに反映し、過去に遡及適用しない。評価末尾以後の訂正・未来行追加は入力hashも結果hashも変えない。
- 日付とevent時刻の不一致を拒否。翌session約定、過去履歴のみのMomentum、同点時の銘柄順、固定Decimal contextを継承。
- 全期間標準化・学習・任意プラグインの実行口はない。未来価格を評価期間内に追加・変更しても、それより前の資産推移・約定は変わらないことをテスト。
- 通常のrunはholdout指定不可。listはresearchだけ、show/verify/compareはholdout IDを拒否。holdoutは明示的な `holdout-run` / `holdout-verify` だけで扱う。通常の比較にholdoutを含めるオプションはない。

これは同じ研究アプリケーション内の誤使用を防ぐ境界であり、OSの所有者に対する秘密保護ではない。ファイル直接参照、別storeでの期間再定義、ソース改変、manifestごとの悪意ある書換えは防げない。時点情報・universe evidenceの真実性も入力提供者に依存する。本番のholdout統制には別OSユーザー/保存先と権限管理、事前確定した戦略の評価・開封記録が必要。

## CLI例（リポジトリルート）

ソース配置のまま使う場合は `PYTHONPATH=src` を指定する。

```sh
export PYTHONPATH=src
python3 -m ai_trading.experiment init --store experiments/phase3-demo --study config/experiment-study.json
```

既存fixtureをネットワークなしで正規化する。

```sh
python3 - <<'PY'
import json
from pathlib import Path
from ai_trading.providers import normalize_daily
from ai_trading.storage import save_observations
fixture = json.loads(Path('tests/fixtures/backtest_pages.json').read_text())
print(save_observations(Path('data/phase3-fixture'), normalize_daily(fixture['pages'], source='fixture')))
PY
```

出力された入力パスを使う。

```sh
python3 -m ai_trading.experiment run --store experiments/phase3-demo --config config/backtest.json --data data/phase3-fixture/cb9fbf0610986661427222ad88e7159025e8d9b192840d1f97f88011d6df7fed.jsonl --split development --seed 0
python3 -m ai_trading.experiment baseline --store experiments/phase3-demo --config config/backtest.json --data data/phase3-fixture/cb9fbf0610986661427222ad88e7159025e8d9b192840d1f97f88011d6df7fed.jsonl
python3 -m ai_trading.experiment list --store experiments/phase3-demo
python3 -m ai_trading.experiment compare --store experiments/phase3-demo > experiments/phase3-demo/comparison.json
python3 -m ai_trading.experiment show --store experiments/phase3-demo EXPERIMENT_ID
python3 -m ai_trading.experiment verify --store experiments/phase3-demo EXPERIMENT_ID
```

compareはIDの列挙も可能。ID省略時は全research実験を含める。期間・入力・費用・初期資金・lot・seed・コード・環境が異なる組合せは明示的に拒否するため、条件が複数あるstoreでは同条件のIDを列挙する。戦略名・lookback・top_nのみ比較差分として許可。結果の良否では除外せず、失敗実験も理由つきで残す。

validationには対応期間のsessionsとデータを用意し `run --split validation` を使う。holdoutは同様に対応した設定・データを用意し `holdout-run` を使用する。holdout-runではsplitは強制的にholdoutとなる。

## 3戦略比較例

`config/backtest.json` と合成fixtureによる2025-01-06〜01-21、11 sessions。A/B、初期100万円、100株単位、手数料10bps、slippage 5bps、Momentum lookback=2/top_n=1。Buy & Holdは初回の等金額配分を維持、Equal Weightは週次で再配分する。

| 戦略 | 累積収益 | CAGR | 最大DD | 年率volatility | Sharpe | turnover |
|---|---:|---:|---:|---:|---:|---:|
| Buy & Hold | 9.0314% | 721.0756% | 0.1486% | 6.8141% | 29.2153 | 0.952247 |
| Equal Weight | 8.1629% | 575.7769% | 0.1486% | 6.4830% | 27.8568 | 1.137439 |
| Momentum | 9.9313% | 903.0033% | 0.1487% | 19.6836% | 11.1559 | 0.970127 |

これは動作確認用の短期間の合成価格であり、戦略の投資能力を示さない。CAGRは暦日365.25換算、volatility/Sharpeは252 sessions・標本標準偏差・無リスク金利0。turnoverは売買両側の約定総額÷平均日次資産（年率化なし）。0分散のSharpeと1日だけのCAGRはnull。

## テストとPhase 4前の課題

`PYTHONPATH=src python3 -m unittest discover -s tests -v`：80件成功（Phase 3追加25件）。実サービスや認証情報は不要。再実行、データ/設定/seed hash、Git/source保存、全成果物改変、成功/失敗再検証、holdout拒否、未来価格/訂正/銘柄、分割、CLI、比較条件を検証する。

Phase 4へ進む前に、実市場の時点別universe証拠・銘柄変更/上場廃止/株式分割、観測履歴の信頼性、OSで隔離したholdout権限と開封手順、長期のdevelopment/validation設計と取引可能性を整備する。現状はPhase 2同様の現金・買いのみ・週次・翌session終値モデルであり、出来高制約、税金、配当、実際の市場インパクトは未対応。Momentumは各split内で履歴を貯めるため初期warm-upがある。複数マシン間のビット一致を保証するコンテナ固定、署名/外部監査ログ、障害時の原子的保存も今後の課題。
