"""単体利用と外部client向けのPortfolio CLI。注文・通信・shell実行は行わない。"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path
from dataclasses import replace
from uuid import uuid4

from .models import Transaction, RuleConfig, PortfolioError, now, stamp, object_from_json
from .store import PortfolioStore
from .market import JsonSnapshotProvider
from .engine import status, alerts, replay

SCHEMA_VERSION = 1


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparseの生入力反射を避け、機械可読エラーを統一する。
        raise PortfolioError("invalid_arguments")


def parser():
    root = Parser(description="実保有株の取引台帳・評価・閾値通知")
    root.add_argument("--db", type=Path, default=Path("data/user-portfolio/portfolio.sqlite3"))
    root.add_argument("--json", action="store_true")
    sub = root.add_subparsers(dest="command", required=True, parser_class=Parser)
    for name in ("init", "add", "transactions", "status", "alerts", "config", "assess"):
        cmd = sub.add_parser(name)
        # 共通引数はサブコマンド前後のどちらにも置ける。
        cmd.add_argument("--db", type=Path, default=argparse.SUPPRESS)
        cmd.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        if name == "add":
            cmd.add_argument("--id", default=None)
            for key in ("symbol", "side", "quantity", "price", "currency", "executed-at"):
                cmd.add_argument("--" + key, required=True)
            cmd.add_argument("--fee", default="0")
            cmd.add_argument("--note", default="")
            cmd.add_argument("--asset-type", default="equity")
        if name in ("status", "alerts", "assess"):
            cmd.add_argument("--snapshot", type=Path)
            cmd.add_argument("--as-of")
            cmd.add_argument("--market-provider", choices=("manual", "jquants", "jquants-fixture"), default="manual")
            cmd.add_argument("--market-fixture", type=Path)
            cmd.add_argument("--market-lookback-days", type=int)
            cmd.add_argument("--max-price-age-seconds", type=int)
        if name == "assess":
            cmd.add_argument("--events-fixture", type=Path)
            cmd.add_argument("--news-fixture", type=Path)
        if name == "config":
            cmd.add_argument("--file", type=Path, help="省略時は現在設定を表示。指定時は全設定を置換")
    return root


def execute(args):
    store = PortfolioStore(args.db)
    if args.command == "init":
        PortfolioStore.initialize(args.db)
        return dict(initialized=True)
    if args.command == "add":
        tx = Transaction(id=args.id or uuid4().hex, symbol=args.symbol, side=args.side,
            quantity=args.quantity, price=args.price, fee=args.fee, currency=args.currency,
            executed_at=args.executed_at, note=args.note, created_at=now(), asset_type=args.asset_type)
        return dict(transaction=store.add(tx).to_dict())
    if args.command == "config" and args.file:
        with args.file.open("rb") as handle:
            raw = handle.read(16385)
        if len(raw) > 16384:
            raise PortfolioError("config_too_large")
        config = RuleConfig.from_dict(object_from_json(raw))
        store.configure(config)
        return dict(rules=config.to_dict())
    transactions, config = store.read()
    if args.command == "config":
        return dict(rules=config.to_dict())
    if args.command == "transactions":
        return dict(transactions=[tx.to_dict() for tx in transactions])
    if args.max_price_age_seconds is not None:
        config = replace(config, max_snapshot_age_seconds=args.max_price_age_seconds)
    if args.market_provider == "manual":
        if args.market_fixture is not None or args.market_lookback_days is not None:
            raise PortfolioError("invalid_arguments")
        provider = JsonSnapshotProvider.from_file(args.snapshot) if args.snapshot else JsonSnapshotProvider()
    else:
        if args.snapshot or (args.market_provider == "jquants-fixture") != (args.market_fixture is not None):
            raise PortfolioError("invalid_arguments")
        from .jquants import prepare_provider
        provider = prepare_provider(replay(transactions), at=args.as_of or now(),
            lookback_days=args.market_lookback_days if args.market_lookback_days is not None else 90,
            fixture_path=args.market_fixture, prefetch=args.command != "assess")
    if args.command == "assess":
        from .assessment import assess
        from .assessment_market import collect_snapshots
        provider, blocked = collect_snapshots(replay(transactions), provider)
        generated = now()
        report = status(transactions, provider, config, args.as_of or generated)
        from .events import JsonEventProvider, enrich_assessment
        event_provider = JsonEventProvider.from_file(args.events_fixture) if args.events_fixture else JsonEventProvider()
        events = [event for symbol in sorted({r["symbol"] for r in report["positions"] if r["quantity"] != "0"})
                  for event in event_provider.events(symbol)]
        assessment = enrich_assessment(assess(report, generated_at=generated, blocked=blocked), events, config.events)
        from .news import JsonNewsProvider, enrich_news
        news_provider = JsonNewsProvider.from_file(args.news_fixture) if args.news_fixture else JsonNewsProvider()
        articles = [article for symbol in sorted({r["symbol"] for r in assessment["assessments"]})
                    for article in news_provider.articles(symbol)]
        return enrich_news(assessment, articles, config.news)
    # 取得後に評価時刻を確定し、取得時刻を過去へ繰り上げない。
    report = status(transactions, provider, config, args.as_of or now())
    report["alerts"] = alerts(report, config)["alerts"]
    return report


def display(data, command):
    """人間向けの表示。機械向けclientは必ず--jsonを指定する。"""
    if command == "assess":
        from .assessment_display import display_assessment
        display_assessment(data)
    elif command == "init":
        print("Portfolio DBを作成しました。")
    elif command == "add":
        row = data["transaction"]
        print(f"取引を登録しました: {row['id']} {row['symbol']} {row['side']} {row['quantity']}")
    elif command == "config":
        for key, value in data["rules"].items():
            print(f"{key}: {value if value is not None else '無効'}")
    elif command == "transactions":
        print(f"取引件数: {len(data['transactions'])}")
        for row in data["transactions"]:
            print(f"{row['executed_at']} {row['id']} {row['symbol']} {row['side']} "
                  f"{row['quantity']} × {row['price']} {row['currency']} 手数料 {row['fee']}")
    else:
        print(f"評価時点: {data['as_of']}")
        print(f"保有銘柄数: {data['portfolio_summary']['open_position_count']}")
        for row in data["positions"]:
            snapshot = row["snapshot"]
            if snapshot and snapshot.get("data_date"):
                print(f"{row['symbol']}: 日足終値（リアルタイムではありません） "
                      f"data_date={snapshot['data_date']} as_of={snapshot['as_of']} "
                      f"source={snapshot['source']}")
        for row in data["currency_summaries"]:
            value = row["market_value"] if row["complete"] else "評価不能（価格欠落）"
            print(f"{row['currency']}: 原価 {row['total_cost']} 評価額 {value} 実現損益 {row['realized_pnl']}")
        if command == "status":
            for row in data["positions"]:
                print(f"{row['symbol']} {row['currency']} 数量 {row['quantity']} "
                      f"未実現損益 {row['unrealized_pnl'] if row['unrealized_pnl'] is not None else '評価不能'} "
                      f"({row['valuation_status']})")
        for row in data["alerts"]:
            label = "評価不能" if row["triggered"] is None else "到達" if row["triggered"] else "未到達"
            print(f"{row['symbol']} {row['type']}: {label}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    machine = "--json" in argv
    command = None
    try:
        args = parser().parse_args(argv)
        command, machine = args.command, args.json
        data = execute(args)
        result = (dict(data, ok=True, command=command) if command == "assess" else
                  dict(schema_version=SCHEMA_VERSION, generated_at=stamp(now()), ok=True, command=command, data=data))
    except (PortfolioError, OSError, sqlite3.Error) as exc:
        code = str(exc) if isinstance(exc, PortfolioError) else "storage_error"
        result = dict(schema_version=SCHEMA_VERSION, generated_at=stamp(now()), ok=False, command=command, error=dict(code=code))
        if machine:
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        else:
            print("Portfolioエラー: " + code, file=sys.stderr)
        return 2
    if machine:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    else:
        display(data, command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
