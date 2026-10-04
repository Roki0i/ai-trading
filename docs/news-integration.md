# Phase 12: News Integration MVP

銘柄に明示関連付けされたニュースの見出し・短い要約・出所・公開時刻をAssessmentへ付加します。価格への影響や記事の良し悪しを判断する機能ではありません。sentiment未対応、LLM未使用、実News API未接続、Raphael未接続です。

## Architecture

```text
明示JSON fixture → NewsProvider → NewsArticle
                                    ↓
Phase 9 Assessment + Phase 10 events → enrich_news → news / news_flags / news_reasons
```

`portfolio/news.py`がモデル、Provider protocol、ローカルJSON Provider、純粋なenrichmentを持ちます。Assessment EngineはHTTP・CLI・特定News API・LLMへ依存しません。enrichmentは入力を変更せず、既存metrics、severity、assessment_flags、reasons、event_flags、event_reasonsと通貨summaryを維持します。ニュースが価格missing/staleの評価停止を解除することはありません。

## NewsArticle schema

| field | 契約 |
| --- | --- |
| id | 非空文字列、最大128文字。source内の記事識別子 |
| symbol | 既存Portfolio形式の銘柄。Providerの明示関連付け |
| title | 非空、最大300文字 |
| published_at | timezone付きISO日時をUTCに正規化。不明はnull |
| source | 非空、最大120文字 |
| url | http/httpsの絶対URL、最大2048文字 |
| summary | Providerが与えた短い要約。最大600文字、空文字可 |
| language | ja/en/ja-JP等の言語タグ形式。未確定はund。最大35文字 |
| metadata | 最大8項目の文字列辞書。key64文字、value256文字以下 |

本文fieldはありません。制御文字は拒否します。URLはhost・port・schemeを検査し、userinfo、空白、backslash、不正percent escape、javascript/file/data/shell/ftp等を拒否します。不正URLは`invalid_news_url`です。URLへアクセス・DNS照会・ブラウザ起動・shell実行はしません。http/https形式の受理はリンク先の信頼性保証ではありません。

将来のUIはtitle/summary/metadataを非信頼テキストとして表示する必要があります。本MVPではHTMLや指示として評価せず、CLIは文字列として表示します。ニュースの文章からTool実行や投資判断は発生しません。

## Provider・fixture・relevance

`NewsProvider.articles(symbol)`は明示symbolに対応するNewsArticle列を返します。MVP実装は`JsonNewsProvider`のみです。`--news-fixture`を指定しなければ空Providerとなり、ネットワークには接続しません。不正fixtureやファイル欠落はエラーにし、空の成功へfallbackしません。

symbolは完全一致で、会社名・titleからの推測、LLM関連度判定、4/5桁code補完はしません。各記事に`relevance="provider_symbol_exact"`を付加します。同じsymbolの複数通貨positionには同じニュースを付与します。市場を含む企業IDの解決は対象外です。

fixtureは最大1MiB、1000記事、schema_versionは整数1。全9fieldを必須にし、未知fieldや本文fieldを拒否します。同じ(symbol, source, id)の重複はエラーで、黙って最新版を選びません。異なるidの同一内容・転載記事の意味的重複判定は行いません。

```json
{
  "schema_version": 1,
  "articles": [{
    "id": "synthetic-1",
    "symbol": "7203",
    "title": "架空企業の展示会に関するテスト記事",
    "published_at": "2025-01-08T08:00:00Z",
    "source": "fixture",
    "url": "https://example.com/news/synthetic-1",
    "summary": "架空の短い要約です。",
    "language": "ja",
    "metadata": {}
  }]
}
```

`tests/fixtures/portfolio_news.json`はfresh/stale/unknownと複数symbolの架空記事だけを含みます。実記事のコピーではありません。

## Freshness、順序、上限

Assessmentのas_ofを評価時刻として使用します。age_secondsは評価時刻−published_atです。時刻はUTC正規化し、timezone offsetの違いを正しく扱います。ingestion時刻や公開時刻を推測しません。

- published_atが評価時刻より未来の記事は除外。
- 不明ならpublished_atとage_secondsはnull、freshness_status=unknown。
- age_seconds <= fresh_hours×3600ならfresh。境界ちょうどを含む。
- 超過ならstale。stale/unknownは出力に残せますがflagには使用しません。
- published_at降順、同時刻はsource/id昇順。不明時刻は既知時刻の後、source/id昇順。
- 未来除外と整列の後にsymbolごとのmax_articles_per_symbolを適用。

flagと理由の件数は**上限適用後に返す記事だけ**を対象とします。max_articles_per_symbol=1では入力にfresh記事が複数あってもmultiple_recent_newsは付けません。根拠がJSON内の記事と一致する仕様です。出力上限のため、省略された記事まで含む総ニュース件数・網羅性は保証しません。

## Configと旧DB互換

既存configへ任意newsを追加します。

```json
{
  "news": {
    "fresh_hours": 48,
    "max_articles_per_symbol": 5
  }
}
```

fresh_hoursは0〜87600の整数、max_articles_per_symbolは1〜50の整数です。bool・文字列・未知項目は拒否します。省略項目は上記既定値を使用し、判定にユーザー設定を適用します。

旧configにnewsがなければ既定値を使い、保存形式へnewsを追加しません。既存SQLiteのcanonical設定とhashを維持し、migrationはありません。明示設定は既存rules領域へ保存します。

設定は従来の`portfolio config --file <設定JSON>`です。このコマンドは全設定置換なので、価格ルール・eventsを残す場合はそれらも設定ファイルに含めてください。

## flagsと構造化理由

freshな返却記事が1件以上ならrecent_news、2件以上ならmultiple_recent_newsも付加します。positive/negative・bullish/bearish・売買推奨は生成しません。既存severityを変えません。

```json
{
  "rule": "recent_news",
  "article_count": 2,
  "fresh_hours": 48,
  "latest_published_at": "2025-01-08T08:00:00.000000Z",
  "sources": ["fixture"],
  "articles": [
    {"id": "jp-fresh", "source": "fixture"},
    {"id": "jp-fresh-2", "source": "fixture"}
  ],
  "count_basis": "returned_articles"
}
```

各flagに独立したreasonを付け、記事ID/sourceで出力へ対応付けできます。

## CLI・JSON

Windowsでは子プロセスへもUTF-8を継承します。

```powershell
$env:PYTHONPATH = "src"
$env:PYTHONUTF8 = "1"
py -3.12 -m ai_trading.portfolio assess --news-fixture tests/fixtures/portfolio_news.json
py -3.12 -m ai_trading.portfolio assess --news-fixture tests/fixtures/portfolio_news.json --json
```

fixtureの鮮度判定を再現するには、2025-01-08以前の7203/JPYまたはNVDA/USDの架空取引があるテストDBを指定します。

```powershell
py -3.12 -m ai_trading.portfolio assess --db <テストDB> --news-fixture tests/fixtures/portfolio_news.json --as-of 2025-01-08T09:00:00Z --json
```

`--events-fixture`と併用できます。価格Providerは既存仕様どおりで、今回のNews実装が実APIを呼ぶことはありません。

schema_version=1を維持し、各assessmentへnews/news_flags/news_reasonsを追加します。newsはNewsArticleの9fieldにage_seconds/freshness_status/relevanceを加えます。情報なしは3項目とも空配列。status/alertsの出力契約は変更しません。

通常表示はtitle・公開時刻・source・鮮度だけで、summaryやmetadataを大量表示しません。unknown日時は「公開時刻不明」と表示し、stale/unknownを最新ニュースと偽装しません。

## Copyright / storage policy

ニュース本文全文の取得・保存は設計しません。fixtureは架空記事、JSONは上限付きのtitle/short summary/metadata等だけです。ニュースはメモリ上で扱い、DB・専用ログへ保存しません。CLIの標準出力を保存する場合も同じ上限付き形式です。

将来実Providerを導入する際は利用規約・再配布権を確認し、全文をsummaryやmetadataへ流用しないでください。長さ検査は権利確認の代わりにはなりません。

## Tests・残る制約

専用テストでは外部通信をmockで禁止し、単数/複数/なし、stale/unknown抑止、境界、未来除外、複数symbol、並び順、上限、根拠、URL・fixture拒否、旧config/DB、CLI、eventsとの併用を確認します。

ニュースの真偽・重要度・投資への影響・転載の意味的重複・Provider網羅性は判定しません。公開日時不明の記事はPIT完全性を保証できず、unknown表示とflag抑止に限定します。新規API、scraping、RSS収集、LLM sentiment、売買判断、Raphael、broker、通知、Web UIは未実装です。


最終検証（Windows / Python 3.12 / PYTHONUTF8=1）:

- Phase 12専用32件、Python全384件成功。SQLite cleanup errorなし。
- 個別回帰: Phase 7=45件、Phase 8=33件、Phase 9=41件、Phase 10=25件、Phase 11=25件、すべて成功。
- market fixture、ML fixture成功。forward fixture、verify（5 entry）、reportも成功。
- 初回forwardは実行中のREADME/docs追加で実験間のgit_statusが異なり、既存の環境比較が拒否した。保存済みenvironmentの差分がgit_statusだけであることを確認。研究コードは変えず、編集を止め新しいTemp先で再実行して成功。
- forward reportはfixture / 5 sessions / insufficient_forward_history。実市場の成績ではない。
- git diff --check成功。secret・DB・experiment/holdout成果物・実行生成物の差分混入なし。workflow生成物とログはTemp配下。
- 実News API、scraping、実J-Quants APIには接続していない。
