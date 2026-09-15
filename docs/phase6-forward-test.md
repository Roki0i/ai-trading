# Phase 6: Real-market validation and frozen forward test

## 到達点

Phase 6の検証・freeze・paper ledger基盤を実装した。実API資格情報は環境に存在せず、実市場検証は未実施。実データ完全性を確認済みとする承認や、実市場Forward Testの開始は行っていない。holdoutは未開封。broker、発注、実資金、LLM、ニュース、強化学習は含まない。

### 実J-Quants検証

`validation.smoke` は `JQUANTS_API_KEY` がある場合だけread-only実APIを選択する。ない場合は2ページ・2架空銘柄のfixtureへfallbackする。credentialがある場合のHTTP障害をfixture成功へ置き換えない。日付指定日足・日付指定master・期間指定calendarの原本本文とreceiptはcontent-addressed/create-onlyで保存する。日足bundleのマージは古い原本を残す。

parserはDate/Code・有限数値を検査し、missing/nullを補完しない。OHLCの正負・高安関係は既存quality層で検査する。公表時刻を推定せず、取得時刻をavailable_at/ingested_atとして記録する。HTTP 401/403/429/500・network/timeoutは取得停止。自動再試行は行わず、運用側で間隔を空けて再取得する。ページカーソルの循環・上限超過は停止する。

認証情報は環境変数からリクエストheaderへ渡し、receiptやExperimentへ含めない。HTTP例外の本文・headerをログへ出さず、credentialを反射した成功本文も保存前に拒否する。テスト用の偽credential以外の値をコードへ追加していない。

### Coverageと実市場仕様の注意点

銘柄・指定期間ごとにprice/volume coverage、missing sessions、duplicate rows、revision、unavailable fields、quality issues、dated listing snapshot coverage、adjustmentイベントを記録する。`acquired` と `research_complete` は別項目。日足とmasterだけでは企業行動の完全性・ライフサイクル・過去の観測時刻を証明できないため、この監査の `research_complete` はfalseのままになる。営業日列は呼出側が公式calendarに基づいて指定する。

今回のfixture: 2025-01-06、90001/90002ともprice 1/1、volume 1/1、missing 0、duplicate 0、revision 0。AdjC、公表時刻、listing/delisting lifecycle、企業行動の完全性は未確認。実市場のcoverageを示す値ではない。

[公式日足仕様](https://jpx-jquants.com/ja/spec/eq-bars-daily)では、無取引日のOHLCVがnullになり、一部プランでは前後場のキー自体が欠落する。AdjFactorは分割・併合のほかライツイシューにも関連するため、非1の係数だけからsplitを推定しない。これらは仕様確認であり、今回実APIで観測した障害ではない。

## Historical universe / corporate action / delisting

`historical_checks` はPhase 5のobserved-PIT lifecycleを複数日で選択する。上場日を含み、上場廃止日を含まない。現在masterを過去へ適用しない。dated master取得成功だけでlisting_date/delisting_dateや当時の利用可能性を作らない。実銘柄のlifecycle根拠が未入手なので、実銘柄universe検証は保留。fixtureでは上場前・上場中・廃止後を検査した。

`validate_split` は独立したaction evidence、effective_date、ratioと当日AdjFactorの逆数関係を要求する。raw close、vendor adjusted close、数量前後、旧markの逆比率を保存する。全期間調整済価格を執行に使わず、端株・未知の調整は停止する。engineの企業行動ledgerにも数量前後・raw closeを追加した。実銘柄splitの検証は根拠未入手のため未実施。

`execution_statuses` はsymbol、status、effective_date、available_at、ingested_at、evidenceを持つ観測履歴。通常売却可能、最終取引日、halt、delisting、cash_out、merger、value_unknownを区別する。haltは約定不可。delisting/cash_out/merger/value_unknownの保有は停止する。買収価格や支払日が判明しても、精算会計の実装がない段階では強制売却・0円化しない。volume=0や欠損から正式なhalt/delistingを推定しない。

## Liquidity-aware execution

`BacktestConfig.volume_participation` は (0,1]。未指定時はPhase 0〜5の挙動を維持し、forward freezeでは指定必須。

1. 当日未調整volume × 参加率を整数株へ切り下げる。
2. 同一symbolの同日約定済数量を控除し、売買単位へ切り下げる。
3. 現金・保有株制約と合わせて約定数量を決める。
4. 部分約定の残数量は取消し、`unfilled_quantity` を記録する。
5. capacityが0なら約定を繰り延べ、`last_no_fill` を記録する。週次注文更新やuniverse退出で取消され得る。

売買双方を合計した日次capacityであり、架空order bookは作らない。判断は18時、執行は翌営業日以降の終値シミュレーション。日足の出来高が判明した後のpaper fillであり、引け注文受付・価格優先・ストップ高安の約定可能性を再現したものではない。

## Freezeと候補選定

manifestは設定全体、universe policy、特徴量定義、model type、hyperparameters、retraining cadence、decision time、execution rule、fee/slippage、参加率、欠損方針、seed、統計設定、development selection、git commit、コード全文とhash、runtimeを含む。canonical JSONのSHA-256をファイル名と `freeze.json` に保存し、SQLiteにも系列hashを固定する。

既存rootへの再freezeやmanifest差替えを拒否する。コード/runtime変更時も同系列への追記・再計算を拒否し、別root/新manifestが必要。MLのpenalty候補はfreeze前に1個へ固定する。seedはML・bootstrapと一致させる。

`select_development` は共通期間・同一データ・コスト・流動性条件でBuy & Hold、Equal Weight、Momentum、MLをExperimentStoreへ保存する。選定規則はnet cumulative return最大、同点は戦略名順。holdoutへの経路はない。fixtureの候補選定条件はfee 10bps、slippage 5bps、参加率1%、penalty 0.01、seed 7。

| 候補 | development累積収益率 |
|---|---:|
| Buy & Hold | -0.2921% |
| Equal Weight | +3.8516% |
| Momentum | -11.2940% |
| ML | -0.3791% |

Equal Weightを選定した。これは架空developmentデータ上の選定であり、実市場での優位性ではない。全候補でPhase 4のML warmup後の共通評価期間を使う。非ML戦略に付いたML設定は比較期間を揃えるための研究計算で、売買判断には使わない。forwardの非ML予測指標はnot_applicableとする。

## Forward ledger / paper portfolio

`forward.freeze` → `Ledger.step` → `Ledger.verify` / `Ledger.report`。

- SQLiteは1decisionを1transactionで追記。sessionは一意、sequenceは連続、同時追記は件数の競合検査で拒否。UPDATE/DELETEをtriggerで拒否する。
- 各entryは前entryのSHA-256を保持し、最初はfreeze hashへ接続する。
- generated_at、decision_at、data_available_at、raw/feature/model/result/reference hash、prediction、target weights、proposed orders、orders、simulated fills、costs、cash/positions/marks/equityを保存する。turnoverは参照先resultのmetricsに保存する。
- 毎営業日のentryを作り、週次rebalanceしない日はtarget_weights=null、proposed_orders=[]とする。これは新規判断のない口座評価entry。
- raw bundleとprocessed observationsを両方保存し、calendar・universe・corporate actions・status履歴を辿れる。観測された新しい参照履歴は `reference_updates` で追記できる。
- 原本履歴の削除・差替え・過去へ遡った新規ingestionを拒否。future revisionはavailableになるまで判断入力にできない。後日利用可能になったrevisionは後続判断にのみ使う。
- 既存engineで保存時点のprefixを再生し、過去の口座状態・predictionが変われば停止する。過去entryを上書きしない。現在の情報で作る研究上の再構築は別Experimentに置く。
- `verify` は全entryのhash・raw再生成・保存時点のconfigによるengine再実行を検査する。再起動は台帳を読み直して次の固定sessionから進める。
- paper modeは最初の評価sessionより前のfreeze、実provider provenance、4候補のdevelopment比較を要求する。時刻上書きはfixture modeだけ。過去日のpaper追記は拒否する（当該UTC日内の実行窓）。sessionを飛ばせないため欠測日は停止し、新系列での再開判断が必要。

これはローカルアプリケーションの不変性・改変検出であり、OS管理者によるDB/原本の全差替え・末尾の削除を外部から証明できるWORMではない。外部timestamp/署名/checkpoint保管は未実装。

## Reporting

累積収益、同一universe/コスト/参加率/期間のBuy & Holdとの差、drawdown、volatility、Sharpe（risk-free=0）、turnover、cost、ML予測指標、block bootstrap CIを出力する。CIは平均日次収益と日次benchmark差。MLは既存の同日銘柄cluster付き予測CIを使う。短い期間ではCIの上下限をnullにし、`insufficient_forward_history` を明示する。60営業日以上でも記述統計であり、モデル選択の不確実性や多重比較を含む優位性の確定判定はしない。

## 再現・運用手順

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m ai_trading.validation --raw-root experiments/phase6-validation --sessions 2025-01-06 --symbols 90001 90002
PYTHONPATH=src python3 -m ai_trading.forward_fixture --store experiments/phase6-release
PYTHONPATH=src python3 -m ai_trading.forward verify --root experiments/phase6-release/fixture-forward
PYTHONPATH=src python3 -m ai_trading.forward report --root experiments/phase6-release/fixture-forward
```

validationの日付・銘柄はfixture専用。環境にcredentialがある場合は実APIへ接続するので契約範囲の対象を指定する。今回credentialは存在しない。fixture-forwardは既存development期間の5営業日を再生する結合検証。開始前に得られていない実未来の収益を表すForward Testではない。

実paper開始時は、既存studyの境界を維持したconfigと、検証済み参照履歴・公式calendar・development selection artifactを用意する。

```sh
PYTHONPATH=src python3 -m ai_trading.forward freeze --root experiments/paper-series --config config/forward.json --study config/study.json --selection path/to/HASH.json --seed 7
PYTHONPATH=src python3 -m ai_trading.forward step --root experiments/paper-series --session YYYY-MM-DD --raw-snapshot path/to/HASH.json
```

この例のconfig/selection/日付は準備する入力のプレースホルダー。CLIは実paperの時刻を捏造しない。J-Quants日足snapshotは前回のrawへ自動マージされる。新しいuniverse/action/status evidenceはcontent-addressed JSONを `--reference-updates` で渡す。published/ingested履歴を証明できない過去データでMLのwarmupを作り直さない。

## 変更ファイル

- `providers.py`: schema、secret反射拒否、timeout、incremental rawマージ。
- `validation.py`: smoke、coverage、historical sanity check、split関係監査。
- `execution.py`: 参加率上限と根拠付きmarket status方針。
- `backtest.py`: 出来高制約、部分約定、判断履歴、企業行動記録、status適用。
- `forward.py`: freeze、append-only ledger、paper実行、再生検証、report/CLI。
- `forward_fixture.py`: development4候補比較と5日間fixture台帳。
- `tests/test_forward.py`: Phase 6追加テスト。
- `README.md`、本書: 手順・結果・制約。

## 残る制約

実API疎通、実銘柄ライフサイクル、実split根拠、取得履歴、契約上の欠落範囲は未確認。実市場で確認できた問題は今回0件（未接続のため）であり、問題がないとの意味ではない。実データ整備が完了するまで実市場Forward開始を保留する。精算・端株・複合企業行動、板/価格制限の約定、外部監査署名、長期間の計算効率化は未対応。prefix再生は長期運用で計算量・保存量が増える。holdout開封には既存の専用経路とauditが必要で、Phase 6には開封コマンドを追加していない。

## 最終検証結果（2026-09-15）

- 181テスト成功（既存135＋Phase 6追加46）。credentialなしで実行。
- 4候補のdevelopment Experimentはすべてcompleted。最終成果物は `experiments/phase6-release`。
- 5営業日のfixture ledgerを作成。後日revisionによる過去ML予測の不変性、restart、raw再生成、台帳の更新・削除拒否を検査。
- 最終paper fixture残高: cash 749,532.876813544825円、A 1,120株、B 1,130株、equity 992,798.835983544825円。総cost 375.262836455175円、turnover 0.2509277342320041。初期資金1,000,000円。
- 5日間の累積収益 -0.7201164%、benchmarkとの差0、CI上下限null、`insufficient_forward_history`。これらは結合テストの値であり実市場Forwardの成績ではない。
- 参加率による部分売却後に固定手数料が売却代金を上回る境界ケースを修正し、回帰テストを追加。
- `git diff --check` 成功。Phase 6 storeにholdoutディレクトリは作成していない。

Freeze SHA-256:

```text
5cbae2b21229eb4c715da58f0c1ce414355328238df9eca3de4f099c36da9bb3
```

manifestは `experiments/phase6-release/fixture-forward/artifacts/<上記hash>.json`。
reportは `experiments/phase6-release/reports/afcb05f16294c90a7c9aaa385055f2f6d0d1239f6e9c5eafe93f9e4d8a5fdf42.json`。
raw coverage reportは `experiments/phase6-final-validation/validation/9d6587ee7a3460954d24f9807b114d86042f98bc00f7b6b406c6d4d23685e0e2.json`。
これらはGit対象外のローカル成果物。freezeの時刻・Experiment IDは再作成時に変わる。
