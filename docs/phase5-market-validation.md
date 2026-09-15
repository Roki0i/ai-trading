# Phase 5: 実市場データ検証・バックテスト堅牢化

Phase 0〜4の既存経路を残し、`BacktestConfig.market` 指定時に厳格な市場コンテキストを適用する。既存設定に黙って新しい営業日やuniverseを適用しない。LLM、ニュース、broker、live trading、自動発注、強化学習は含まない。実APIの疎通や実銘柄による収益検証は未実施。

## 再現手順

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m ai_trading.market_fixture --store experiments/phase5-verified
```

後者は架空2銘柄・210営業日のdevelopmentだけを使い、equal_weight / momentum / ML × 3コストを保存・再検証する。比較期間を揃えるため全戦略に同一のML研究設定を付け、Phase 4同様にwarmup後の共通期間で評価する。予測CIも同じ研究出力に保存する。これはAIの有効性や実市場収益の証拠ではない。実験IDは実行ごとに変わり、同じコード・データ・設定では結果hashが一致する。

## Provider / raw / processed

`Transport.get(path, params) -> Response(body, ingested_at)` は実APIとfixture共通。`MarketDataProvider.acquire` は日足のページを既存content-addressed rawストレージへ保存し、receiptと原本本文を含む可搬bundleを生成する。既存ファイルへの上書きは行わず、hash不一致は停止する。`regenerate` は本文hashを検証し、ネットワークなしで既存 `normalize_daily` によりprocessed Observationを再生成する。

`acquire_reference` はカレンダーおよび日付指定銘柄一覧を同じTransportで取得・保存する。現在一覧へのデフォルトアクセスを禁止する。取得したmasterの欠落から上場日・上場廃止日を推測しない。銘柄のライフサイクルと企業行動は、根拠を持つObservationを別途提供する必要がある。

本番 `market.data_provider='jquants_v2'` の `raw_data` にはbundle全体を格納し、`raw_data_hash` にはそのcanonical SHA-256を格納する。架空fixtureだけは `synthetic_jquants_v2` として生成Observationの原本配列を使う。Experimentはrawから再生成したObservationとprocessed入力を完全一致で突合する。原本に対応しない入力を含む実験は失敗として記録する。raw内に別splitの日付や知識境界以後のデータがある場合は、保存前に拒否する。API障害は取得失敗として停止し、成功応答内の欠落は研究結果で `api_gap` として扱う。

## Trading Calendar

`TradingCalendar` は期間中の全暦日について `open` または閉場理由を保持する。version、source、available_atを必須とし、日付の欠落・範囲外・非ISO日付を拒否する。土日と年末年始の明白な矛盾も拒否する。祝日・臨時休場は明示データに従い、平日だけの推定はしない。

`calendar_from_raw` はJ-Quantsの営業日・半日立会日をopen、非営業日と先物祝日取引日をclosedとする。応答が要求範囲を覆わない場合は停止。カレンダーのavailable_atが最初の判断時刻より後なら利用できない。fixtureの休日一覧はテスト対象範囲専用であり、将来年へ外挿してはならない。

判断・執行・特徴量・target horizon・train/validation/purge/prediction windowが参照する `config.sessions` を、Calendarの完全な営業日列と一致させる。これにより途中の営業日を抜いてtarget期間を短縮することも拒否する。約定方式はPhase 2と同じ「判断の次営業日以降の終値」。日足を18時時点で観測できたとする研究上の終値シミュレーションであり、実際の引け注文の再現ではない。

## Historical Universe / PIT

`historical_universe` はlisting_date、delisting_date、source、published_at、available_at、ingested_at、revision_idを持つObservationの履歴。listing_dateを安定したevent identityとし、delisting_dateの判明・訂正を同じidentityの新しい版として記録する。上場日を含み、上場廃止日は含まない半開区間。その日のPIT選択後にmembershipを計算し、保有銘柄を現在一覧から過去へ逆算しない。

studyのsymbolsは凍結した候補ID集合であり、その日の投資可能集合ではない。対象日の投資可能集合は履歴から求める。将来上場銘柄が候補に含まれていても、上場前に発注・特徴量対象へ入らない。上場廃止済みの負け銘柄を残した場合と、現在銘柄だけに絞った偏った計算との差をテストする。

Phase 5はobserved PITのみ。available_atとingested_atは判断より厳密に前、published_atはObservationの検証によりavailable_at以下。曖昧な同時revisionを拒否する。後日取得した過去の日足に当時のavailable_atを捏造しない。後日訂正データは利用可能になる前の判断に影響しない。Experimentは入力・市場参照履歴・コード・runtime・結果を凍結し、原本hash・設定hash・結果hashを検証して再実行する。

## Corporate Actions / Delisting

執行・時価評価には未調整OHLCを使い、全期間調整済み価格は使用しない。

- 分割・併合: effective_dateの売買より前の保有数量にratioを適用し、旧markを逆比率で変更する。未約定注文は取消。rawの当日adjustment_factorとratioの積が1でない場合は停止する。端株が生じる場合は根拠あるcash-in-lieuを実装するまで停止する。
- 配当: effective_dateを権利落ち日として、その直前保有数量から受取債権を計上する。支払日以降の最初の評価セッションで現金化し、それまで債権をequityに含める。税・再投資・外貨・株式配当は未対応。
- 特徴量とラベル: 分割・併合・配当をまたぐ期間は除外する。total-returnラベルやPIT調整系列は未実装。将来の企業行動で過去の価格を変更しない。
- 未知の行動、当日価格との不整合、処理済み行動の訂正、期間途中に遅れて届いた行動、曖昧な同日複数行動は停止する。同一銘柄・同一日の複数権利は現モデルでは扱えない。
- 上場廃止: 注文を取消し、ポジションが存在すれば明示エラーで実験を停止する。最後の価格での強制売却やゼロ円償却を捏造しない。実際の精算・買取価格・支払日をモデル化することは今後の課題。

## Missing Data Policy

`strict_v1` を固定し、結果の `missing_data_events` と失敗理由をExperimentから追跡できる。

| 状況 | 処理 |
|---|---|
| 非保有銘柄の価格/API欠落 | 欠落理由を記録し、その日の執行・価格特徴量に使わない |
| 保有銘柄の価格/API欠落 | 時価評価を停止し実験をfailedにする |
| 出来高欠落 / 0 | 執行を繰り延べ、理由を記録する |
| 保有中の連続取引不可 | 5営業日を超えたら停止する |
| IPO直後などの履歴不足 | 連続した特徴量期間が揃うまで対象から除外する |
| 部分的・不正OHLC | 既存quality検証で停止する |

価格・特徴量への無条件forward fillはない。保有価格欠落時の旧Phase 2 stale-mark方式は、Phase 5では停止方式になる。失敗した実験には失敗理由が残るが、停止までの部分的約定・欠損イベント列は保存されない。データ供給元が欠落理由を提供しない場合、API欠落と配信遅延を確定的に区別できない。

## 統計評価 / Cost Sensitivity

固定seedのcircular moving-block bootstrap。連続営業日のblockを復元抽出し、元の長さへ切り詰める。平均日次リターンCI、同一日付に揃えた戦略リターン差CIを算出する。予測accuracy / Brier scoreのCIは、その日に属する全銘柄を同じclusterとして抽出し、同日銘柄を独立標本としない。blockが2個分に満たない場合はCIを返さず理由を記録する。

fixture設定はblock=5営業日、200反復、95% percentile CI、seed=7。CIは平均日次リターン差であり、累積収益差のCIではない。block長・期間・非定常性に結果が依存する。モデル再学習やモデル選択の不確実性、多重比較補正は含まない。

コストはoptimistic=fee/slippage 0/0 bps、base=10/5 bps、conservative=50/25 bps。各ケースは同一Strategyを資金制約込みで再実行し、別Experiment IDとして保存する。高コストで投資額や売買数量が変わるので、一般の価格経路で利益が必ず単調に下がるとは限らない。一定価格fixtureでは厳密な悪化をテストする。

## Holdout / provenance

既存Phase 3のstudyは変更しない。Phase 5 fixtureは専用storeを使い、holdoutは開封しない。Phase 5市場コンテキストを付けたholdout実行は、明示flagがあっても拒否する。既存Phase 3の専用holdoutコマンドは分離された成果物・manifestを作成する監査可能な経路として維持するが、今回は実行していない。

Experimentのmarket_provenanceにprovider、raw hash、calendar version/hash、historical universe hash、corporate action hash、missing policy、cost scenario、統計設定を保存する。対応する全入力をconfig/raw/input artifactへ格納し、experiment_idから辿れる。OSレベルの改竄不能なWORMや外部監査署名ではなく、content hashによるアプリケーション上の不変性・再現検証である。

## 実市場・Phase 6前の課題

1. 契約範囲内のJ-Quants実API疎通、配信遅延・revision履歴・raw保管運用の確認。当時観測した履歴がない期間のobserved PITバックテストは行えない。
2. 上場廃止を含むライフサイクル履歴の完全性と、企業行動・配当権利日/支払日の根拠あるデータを調達する。現在masterだけで代用しない。
3. 公式カレンダーの取得履歴と臨時休場訂正を管理する。現実の短縮取引時間・引け時間と、注文受付時刻・価格利用可能時刻を明確化する。
4. 上場廃止の精算、端株、複合企業行動、遅延訂正の再処理、配当税、total-return特徴量・ラベルを設計する。
5. spread・出来高参加率・ストップ高安・流動性制約を含む約定モデルと、block長の感度・非定常性を評価する。
6. モデル・特徴量・コスト条件・評価基準を事前固定し、holdoutを開けずにForward Test用の観測ログとデータ品質監視を開始できる手順を用意する。broker接続は別段階。

## 参照仕様

- [J-Quants取引カレンダー](https://jpx-jquants.com/ja/spec/mkt-cal)
- [J-Quants休日区分](https://jpx-jquants.com/ja/spec/mkt-cal/holiday-division)
- [JPX営業時間・休業日](https://www.jpx.co.jp/corporate/about-jpx/calendar/)

## Fixtureコスト比較結果

共通評価期間の累積リターン（%）。検証専用の架空価格であり、実運用の期待収益を表さない。

| Strategy | optimistic | base | conservative | base turnover |
|---|---:|---:|---:|---:|
| equal_weight | 0.114 | -0.106 | -0.972 | 1.441 |
| momentum | -6.687 | -7.670 | -11.486 | 6.947 |
| ML | 15.649 | 13.414 | 4.927 | 12.924 |

MLのoptimistic→conservativeの低下は約10.722ポイント。売買回転の高い戦略のコスト影響を検出できている。turnoverの定義は既存metricsの「総売買代金 / 平均equity」。約定数量はコスト別に変わる。

9実験の各Experiment ID、metrics、paired return difference CIは生成先の `cost-report.json` に保存する。最終検証用storeは `experiments/phase5-final`。各実験には統計設定と予測accuracy/Brier CIも保存される。

最終回帰検証: `JQUANTS_API_KEY` を外した状態で135テスト成功（既存100件＋Phase 5追加35件）。9本のExperimentはすべてcompleted、保存直後のverifyで再実行一致。`git diff --check` 成功。専用storeのholdoutディレクトリは未作成。
