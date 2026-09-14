# Phase 4: AI/ML比較実験

## 実行

Python 3.9以上、標準ライブラリのみ。APIキー・ネットワーク不要。

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m ai_trading.ml_fixture --store experiments/phase4-fixture
PYTHONPATH=src python3 -m ai_trading.experiment compare --store experiments/phase4-fixture
PYTHONPATH=src python3 -m ai_trading.experiment verify --store experiments/phase4-fixture EXPERIMENT_ID
```

fixtureコマンドは180営業日相当の平日・架空2銘柄を生成し、4件のPhase 3 Experimentを保存、再実行検証する。暦は日本の取引所営業日ではない。研究用の実データ・独立holdoutに対する性能評価は未実施。

通常のExperiment CLIもBacktestConfigの`ml`設定を受け付ける。`strategy="ml"`にする。比較する非AI戦略にも同じ`ml`設定とseedを渡すことで、共通の助走期間を除いた評価期間となる。設定例は保存されたconfig.jsonに全項目を記録する。mlなしの従来Phase 2/3の動作は変わらない。

## 特徴量とPIT

固定定義は5/20/60セッションリターン、直近20日リターンの標本標準偏差、close/20日平均close−1。volume=Trueを明示した場合のみvolume[t]/volume[t−5]−1を追加。入力全期間の欠損状況から特徴選択しない。61連続セッション分のcloseがない銘柄は除外、volumeを使用するとき分母欠損・ゼロも除外。補完はしない。

各セッション18:00 JSTのdecisionに対し既存のobserved PIT・品質検査・未対応corporate action拒否を再利用。available_atとingested_atがdecisionより厳密に前の観測版だけを読む。各特徴量に、その参照観測の利用可能時刻の最大値、セッション、revision_id、観測全体のSHA-256を記録する。原観測はExperiment inputsから辿れる。過去特徴を後日の最新版で再計算しない。観測のevent_atが未来の場合も除外する。

## ラベルとwalk-forward

教師ラベルはclose[t+5]/close[t]−1 > 0。t+5の18:00に見えていた価格版から生成・固定し、その時刻をラベルの利用可能時刻とする。ラベルは特徴辞書と分離され、モデル入力は固定の特徴名だけから構成される。最後の5セッションの予測はラベル未確定として予測指標から除外する。欠損で評価できない予測数も記録する。

既定値は60日助走、40日train、5日purge、20日validation、5日purge、次20日予測。以降20日ずつrollingで進む。営業日インデックス単位で区切り、同日銘柄は同じ区間に配置する。random shuffleはない。trainラベルはvalidation開始前、validationラベルは予測開始前に確定していることをさらに確認する。

各trainだけで平均・母標準偏差をfitし、ゼロ分散のscaleは1。同じscalerをvalidationと予測に適用。seed付き初期値から全件batch gradient descentでロジスティック回帰を120epoch学習（学習率0.1）。L2候補0.01/0.1をvalidation log lossで比較し、同点なら小さい係数を採用。interceptは正則化しない。train+validationでの再fitはしない。単一クラスtrainも最適化可能だが性能上の妥当性を意味しない。

各foldはその時点までに完結した区間のみを使う。以前の予測期間が将来foldのtrainに入ることは許容する。Phase 3のdevelopmentまたはvalidation内でのみrollingを実施し、外側splitを跨いだ学習データ読取はしない。holdoutは明示アクセスフラグを付けてもML選択処理を拒否する。holdoutへの固定済みモデル適用は未実装。研究者が別storeを作って期間を改名することを防ぐセキュリティ境界ではない。

## シグナルと比較

p>=0.5の適格銘柄を均等配分、該当なしなら現金。閾値の最適化はしない。予測は毎日生成するが執行は既存の週次判断、次セッション終値約定をそのまま使用。現金、売却先行、ロット、手数料、slippage、未約定処理も既存実装を通る。

Buy & Hold / Equal Weight / Momentum / MLは同一入力hash、universe、cost、初期資金、評価開始・終了で比較する。Momentumは同じ助走期間の過去価格を使用可能。共通のML研究情報を各runに保存するが、非AI戦略は予測を注文に使用しない。投資指標は既存の累積収益率、CAGR、最大DD、年率volatility、Sharpe（無リスク0）、turnover（売買notional総額/平均equity）。

予測指標は全OOS銘柄日についてaccuracy、precision、recall、ROC-AUC（tie=0.5）、Brier、log loss、10固定binのcalibration、ECE。比較参照として固定p=0.5も保存。分母なしはnull。銘柄日をpoolし、ラベル期間も重複するため独立標本の有意差を主張できない。2銘柄のfixtureでICを主要指標として扱うのは適切でないため未実装。

ラベルはdecision終値からの方向、投資損益は翌セッション終値の実行と週次再配分による。その差・コスト・選択率によってaccuracy向上と収益向上は一致しない。

## 保存と再現

Phase 3 ExperimentStore、入力/config/result hash、source snapshot、runtime、manifest、再生照合を共用。strategy_parameters.mlにhorizon、各window、step、epoch、学習率、正則化候補、volumeフラグ、seedを保存。Experiment seedとの不一致は拒否する。

outcome.json内のresult.ml_researchに以下を保存する。

- feature_definitionと全featuresの値・利用可能時刻・観測版lineage
- target_definition、実際の投資evaluation_period（外側study期間はExperiment本体に保持）
- preprocessing config、shuffle=false
- foldごとのtrain/validation/prediction期間、train/validation対象キー、ラベル最大利用可能時刻
- model type、特徴順序、scaler平均/scale、係数、intercept、seed、正則化係数、モデルartifact hash
- 各候補validation log loss、全OOS予測、prediction metrics、未評価予測数

ラベルは凍結inputsと明示target定義から再生成する。モデルはJSONでありpickle実行不要。全結果は既存outcome hashとmanifestで検証され、verifyは再学習・再バックテストで一致確認する。fixture-report.jsonは便宜的な比較レポートで、正本は検証可能な各Experiment。

## 制約と次Phase前の課題

fixture収益は機械学習の有効性の証拠ではない。取引所カレンダー、当時のuniverseと配信・取得時刻、上場廃止、企業行動、欠損・停止、流動性を検証した十分な実観測履歴が必要。既存のcorporate action非対応制約も継続。

純Python実装は小規模・監査可能性優先で大規模データには遅い。学習の収束保証や独立ライブラリとの数値照合、銘柄別/期間別評価、ラベル重複を考慮した統計的検定、複数探索の履歴管理、コスト感応度、固定モデルを独立holdoutへ一度だけ適用する経路が次の課題。正則化以外の設計変更も外側validation/holdoutから独立に管理する必要がある。

LLM予測、ニュース、EDINET、sentiment、deep learning、RL、broker API、実売買は実装していない。

## 検証結果（fixtureのみ）

100 tests passed（既存80件＋Phase 4追加20件）。4件のExperimentすべてで保存入力からの再学習・再バックテスト一致を確認。結果は`experiments/phase4-verified/fixture-report.json`、各正本は同storeの`research/<experiment_id>/`にある（experimentsは既存.gitignoreの対象）。

共通評価期間2024-07-02〜2024-09-09、50架空セッション、初期資金1,000,000、手数料10bps、slippage 5bps、ロット1。

| 戦略 | 累積収益率 | CAGR | 最大DD | 年率volatility | Sharpe | turnover |
|---|---:|---:|---:|---:|---:|---:|
| Buy & Hold | 1.53% | 8.38% | 8.16% | 11.16% | 0.74 | 1.01 |
| Equal Weight | 2.07% | 11.46% | 8.13% | 11.22% | 0.98 | 1.27 |
| Momentum | −5.49% | −25.84% | 10.38% | 11.93% | −2.33 | 4.03 |
| ML | 9.60% | 62.46% | 3.30% | 9.51% | 4.91 | 6.88 |

3 folds、予測100銘柄日、確定ラベル90件（末尾10予測は未確定）。accuracy 71.11%、precision/recallともに74.00%、ROC-AUC 0.805、Brier 0.1812、log loss 0.5325、10bin ECE 0.1623。固定p=0.5参照はaccuracy 55.56%、ROC-AUC 0.5、Brier 0.25、log loss 0.6931、ECE 0.0556。MLのECEは参照より大きく、確率校正の改善は示されていない。

fixtureは規則的な合成価格で、学習しやすい構造を持つ。高いCAGR/Sharpeも短期間の架空結果の年率換算であり、実市場での有効性の根拠にはならない。
