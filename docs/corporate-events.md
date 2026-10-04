# Phase 10: Earnings / Corporate Events MVP

保有銘柄の予定をAssessmentへ付加するdomain contractです。対応はearnings（決算）、dividend（配当）、stock_split（株式分割）だけです。自動売買・売買推奨・注文・LLM判断は行いません。News未対応、実API未接続です。

## Architecture

```text
明示したJSON fixture → CorporateEventProvider → CorporateEvent
                                                ↓
Phase 9 Assessment → enrich_assessment → events / event_flags / event_reasons
```

`portfolio/events.py`にモデル、Provider protocol、ローカルJSON Provider、純粋な拡張関数を置きます。Phase 9のAssessment Engineは変更せず、拡張関数も時計・DB・特定外部Providerに依存しません。CLIがデータを取得して渡します。入力Assessmentの既存metrics、severity、flags、reasons、通貨集計、生成時刻は変えません。

## CorporateEvent

| field | 形式 |
| --- | --- |
| symbol | 既存Portfolioと同じ銘柄識別子 |
| event_type | earnings / dividend / stock_split |
| event_date | YYYY-MM-DD。Asia/Tokyoの予定日 |
| announced_at | timezone付きISO日時、内部・出力はUTC。時刻不明はnull |
| source | 非空文字列、最大120文字 |
| status | scheduled / announced / completed / cancelled / unknown |
| metadata | 最大16項目の文字列辞書。key最大64文字、value最大256文字 |

metadataは補足情報だけで、配当金額計算や分割数量補正には使いません。型・日付・制御文字・未知fieldを検証します。完全に同じレコードの重複は拒否します。異なる発表の版管理・取消関係の解決はMVP対象外です。fixtureには利用する版を明示してください。

## Providerとfixture

`CorporateEventProvider.events(symbol)`はその銘柄のCorporateEvent列を返す境界です。MVP実装は`JsonEventProvider`のみで、外部通信・scraping・暗黙fallbackはありません。`--events-fixture`を指定しなければ空Providerです。

fixtureは次の形式です。最大1MiB、1000件、schema_versionは整数1。各イベントの7fieldは必須で、metadata省略もJSONでは拒否します。

```json
{
  "schema_version": 1,
  "events": [{
    "symbol": "7203",
    "event_type": "earnings",
    "event_date": "2025-01-15",
    "announced_at": "2025-01-07T00:00:00Z",
    "source": "fixture",
    "status": "scheduled",
    "metadata": {}
  }]
}
```

`tests/fixtures/portfolio_events.json`に架空の決算・配当・分割予定があります。実市場の予定として使用しないでください。ファイル欠落・不正形式はエラーにし、「イベントなし」の成功に置き換えません。

## 日付、鮮度、flag

Assessmentのas_ofをUTC+09:00へ変換した日付からdays_untilを計算します。日本の現行Asia/Tokyo運用に対応する固定オフセットで、海外市場カレンダーや歴史的夏時間は対象外です。

- days_until: event_date − 評価日の暦日差。今日=0、過去は負。
- is_upcoming: days_until >= 0、かつstatusがscheduled/announced。
- is_recent: `-recent_days <= days_until < 0`、かつcancelledではない。予定日の近さを示すだけで、実施確認ではありません。
- 評価時点より未来のannounced_atを持つイベントは出力対象外。過去のAssessmentへ未来発表を混ぜません。
- freshness_status: announced_at不明ならunknown。評価時点との経過秒数がmax_announcement_age_days×86400を超えたらstale、それ以外はfresh。境界ちょうどはfresh。
- freshは発表時刻に基づく保守的な経過判定です。Providerへの再照会や予定の正確性を保証しません。変更のない古い発表もstaleになります。
- stale/unknownはイベントとmetadataを残し、flagを抑止。future/cancelled/completed/unknown status由来のflagも発火しません。

| flag | 対象 | 到達条件 |
| --- | --- | --- |
| earnings_soon | earnings | 0 <= days_until <= earnings_soon_days |
| dividend_soon | dividend | 0 <= days_until <= dividend_soon_days |
| stock_split_upcoming | stock_split | 0 <= days_until <= stock_split_soon_days |

上記はfreshかつis_upcomingの場合だけ発火します。同じflagは一度だけ表示し、根拠は該当イベントごとにすべて残します。イベントは価格の鮮度とは独立しており、価格missingでもfreshなイベント予定は表示可能です。イベントによって価格評価停止を解除したりseverityを変えたりしません。

## 設定と後方互換

既存`portfolio config --file`の設定JSONへ任意のeventsを追加できます。

```json
{
  "events": {
    "earnings_soon_days": 7,
    "dividend_soon_days": 7,
    "stock_split_soon_days": 14,
    "max_announcement_age_days": 30,
    "recent_days": 7
  }
}
```

各値は0〜3660の整数。省略fieldは上記既定値を使用します。設定コマンドは従来どおり全置換なので、既存の価格ルールを維持する場合はそれらもJSONに含めてください。

旧設定にeventsがなければ既定値で動作し、to_dictにもeventsを追加しません。旧SQLiteのcanonical設定・hashを維持し、migrationは不要です。明示設定した場合だけ既存rules領域へ保存します。イベント本体やAssessmentはDBに保存しません。

## CLIとJSON

Windowsでは子プロセスにもUTF-8を継承してください。

```powershell
$env:PYTHONPATH = "src"
$env:PYTHONUTF8 = "1"
py -3.12 -m ai_trading.portfolio assess
py -3.12 -m ai_trading.portfolio assess --json
py -3.12 -m ai_trading.portfolio assess --events-fixture tests/fixtures/portfolio_events.json --as-of 2025-01-08T09:00:00Z --json
```

fixture例は2025-01-08以前の7203保有があるテストDBで実行します。必要なら`--db <パス>`を指定します。市場価格は従来のmanual/fixture選択を併用でき、イベント用の新しい外部APIはありません。

schema_version=1を維持。各assessmentへevents、event_flags、event_reasonsの3項目を追加する互換拡張です。eventsはCorporateEventの7fieldにdays_until/is_upcoming/is_recent/freshness_statusを追加します。field名は例示のtype/dateではなく、モデルと統一したevent_type/event_dateです。イベント情報なしなら3項目とも空配列。既存のstatus/alerts契約は変更しません。

構造化根拠の例:

```json
{
  "rule": "earnings_soon",
  "days_until": 7,
  "threshold_days": 7,
  "comparison": "0<=days_until<=threshold_days",
  "event_date": "2025-01-15",
  "source": "fixture",
  "announced_at": "2025-01-07T00:00:00.000000Z",
  "status": "scheduled",
  "freshness_status": "fresh"
}
```

人間向けCLIはイベント種別、予定日、何日後/前、status、freshnessを表示します。古い予定や取消済みの予定も状態を添えて表示し、最新予定に見せません。

## 検証と制約

専用テストは閾値内外・境界、当日・過去・JST境界、stale/unknown、未来発表除外、状態別抑止、複数イベント・銘柄、根拠、旧設定/DB互換、JSON、人間向けCLIを対象にします。通常テストでは実APIを呼びません。

銘柄は文字列の完全一致で対応し、市場・通貨を含む企業IDや4/5桁コードの自動統一はしません。同一symbolの複数通貨positionには同じ企業イベントを付与します。source/metadataは入力者の申告で、原本の真正性や実予定を検証するものではありません。発表時刻不明の情報のPIT完全性は保証できず、unknownとしてflagを抑止します。

News、Sentiment、LLM、Raphael、新規外部API、Web scraping、broker、注文、自動売買、配当額計算、分割補正、FX、税、決算surprise分析、fundamental scoringは実装しません。


Windows / Python 3.12 / PYTHONUTF8=1での最終検証:

- Phase 10専用25件、Phase 9個別回帰41件成功。
- Python全327件成功（Phase 7の45件・Phase 8の33件を含む）。SQLite cleanup errorなし。
- market fixture、ML fixture、forward fixture、forward verify（5 entry）、forward report成功。
- forward reportはfixture / 5 sessions / insufficient_forward_history。実市場の成績とは扱わない。
- git diff --check成功。実API接続なし。workflow生成物・ログはTemp配下で、DB・secret・experiment/holdout artifactの変更混入なし。
