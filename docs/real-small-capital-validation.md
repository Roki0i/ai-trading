# 実市場・少額資金の比較手順（開発段階）

この経路は保存済みJ-Quants v2原本を、既存の観測時点・市場参照・ExperimentStoreへ渡す。
実注文や証券APIへの発注はしない。現時点のリポジトリには実市場の原本、過去の取得履歴、
独立した銘柄ライフサイクルと企業行動の網羅記録がなく、実市場の収益率は未検証。
`JQUANTS_API_KEY` がない環境でも入力検査と合成データのテストは動く。

## 利用できる経路と不足する根拠

| 入力 | 既存実装 | 実市場の比較に必要なもの |
|---|---|---|
| 日足・出来高 | `JQuantsTransport`、`MarketDataProvider.acquire/regenerate`、原本hash | 各判断日の18時JSTより前に実際に保存した応答。後日の取得時刻を過去へ戻さない |
| 営業日 | `acquire_reference`、`calendar_from_raw` | 期間全日の原本と最初の判断時点より前の取得記録 |
| 日付指定master | `acquire_reference`、既存coverage | 各日の取得原本と当時の取得記録。現在masterから上場・廃止日を逆算しない |
| 上場・廃止履歴 | `historical_universe` のPIT選択 | 独立資料に基づく全候補の上場日、廃止日、訂正、入手時刻 |
| 分割・併合・配当 | `corporate_actions` と原株価の照合・保有数量/債権処理 | 権利日、比率・金額、支払日、訂正、網羅範囲の独立した根拠 |
| 売買単位 | `lot_size=100` | 対象銘柄・期間の100株単位を裏付ける資料。変更・端株は別設計 |

日足の調整済み価格は後から遡及して変わり得るため、執行には未調整値を使う。
`AdjFactor` だけでは権利内容や全企業行動の網羅性を証明できない。
[J-Quants日足仕様](https://jpx-jquants.com/ja/spec/eq-bars-daily)、
[日付指定master](https://jpx-jquants.com/ja/spec/eq-master)、
[取引カレンダー](https://jpx-jquants.com/ja/spec/mkt-cal)、
[配当情報](https://jpx-jquants.com/ja/spec/fin-dividend)を参照。
契約ごとの利用可能期間や実レスポンスは認証済みの実取得で別途確認する。

## オフライン入力

- `--config`: `BacktestConfig` の全項目。`market.data_provider=jquants_v2`、
  `market.raw_data` に既存の原本bundle、`market.calendar` に `calendar_from_raw` の結果、
  `market.historical_universe` と `market.corporate_actions` に独立根拠の観測を入れる。
  `ml`、`max_position_weight`、`min_cash_reserve_weight`、手数料、スリッページ、
  `volume_participation`、`lot_size=100` も明記する。
- `--data`: `save_observations` が作るhash付きprocessed JSONL。原本からの
  `MarketDataProvider.regenerate` と一致する必要がある。
- `--study`: 開発・検証・holdoutの期間を事前固定した既存study形式。
  `historical_point_in_time` の候補集合を使う。
- `--evidence`: 下記の原本bundleと研究者が確認した独立参照元。参照元の文字列は
  完全性の証明ではなく、後日の検証先を残す宣言である。

```json
{
  "schema_version": 1,
  "period": ["開始営業日", "終了営業日"],
  "universe": ["jquants_v2:銘柄コード"],
  "calendar_bundle": {"provider": "jquants_v2", "schema_version": 1, "receipts": []},
  "master_bundles": [],
  "calendar_bundle_sha256": "canonical(calendar_bundle)のSHA-256",
  "master_bundles_sha256": "canonical(master_bundles)のSHA-256",
  "historical_universe_sha256": "canonical(market.historical_universe)のSHA-256",
  "corporate_actions_sha256": "canonical(market.corporate_actions)のSHA-256",
  "lifecycle_source": "上場・廃止履歴の独立参照先",
  "corporate_action_source": "全企業行動の照合範囲と独立参照先",
  "trading_unit_source": "期間・銘柄別売買単位の参照先"
}
```

hashは `ai_trading.storage.digest(canonical(value))` で算出する。
calendar/master bundleは既存 `acquire_reference` の戻り値を `read_verified` で読み取る。
空の配列を「企業行動が存在しない根拠」とみなさず、`corporate_action_source` に
照合した範囲と資料を明記する。APIキー、認証header、個人情報は入力へ記録しない。

## 実行

```powershell
$env:PYTHONPATH = "src"
$env:PYTHONUTF8 = "1"
python -m ai_trading.real_validation --config <config.json> --study <study.json> --data <hash.jsonl> --evidence <evidence.json> --check-only
python -m ai_trading.real_validation --config <config.json> --study <study.json> --data <hash.jsonl> --evidence <evidence.json> --store <新規store>
```

事前検査は原本hash、日足・出来高、営業日の完全列、各日のmasterとPIT銘柄履歴の一致、
企業行動の整合性、観測時刻、MLに必要な期間を検査する。失敗時は実験を作らない。
成功後は10万・20万・30万円 × Buy & Hold・Momentum・MLを同じ設定と入力で実行し、
各資金額内の3戦略を `ExperimentStore.compare` で照合する。結果と参照宣言のhashを
content-addressedな `comparisons/` に保存する。holdoutはこのコマンドから開けない。

出力は **exploratory_observed_PIT** と明記する。参照元の完全性はコードだけでは証明できず、
実際の引け約定、値幅制限、スプレッド、税、銘柄別売買単位変更も未再現。
実収益性を判断する前に、独立資料の監査、事前固定した比較条件、別期間での検証が必要。
