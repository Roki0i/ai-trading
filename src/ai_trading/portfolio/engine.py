"""平均取得原価・評価・閾値通知。研究戦略や注文執行を参照しない純粋計算。"""
from dataclasses import asdict
from decimal import Decimal, localcontext
from .models import (ARITHMETIC, Position, Transaction, RuleConfig, MarketSnapshot,
                     PortfolioError, instant, stamp, number)

ZERO = Decimal(0)


def replay(transactions):
    """約定時刻順。同時刻は台帳追加順。全売却で残存原価を厳密にゼロにする。"""
    transactions = list(transactions)
    if any(not isinstance(tx, Transaction) for tx in transactions):
        raise PortfolioError("invalid_transaction")
    with localcontext(ARITHMETIC):
        states, identifiers, economic = {}, set(), set()
        for tx in sorted(transactions, key=lambda row: row.executed_at):
            if tx.id in identifiers or tx.economic_key() in economic:
                raise PortfolioError("duplicate_transaction")
            identifiers.add(tx.id)
            economic.add(tx.economic_key())
            key = (tx.symbol, tx.currency)
            quantity, cost, realized = states.get(key, (ZERO, ZERO, ZERO))
            if tx.side == "buy":
                quantity += tx.quantity
                cost += tx.quantity * tx.price + tx.fee
            else:
                if tx.quantity > quantity:
                    raise PortfolioError("oversell")
                allocated = cost if tx.quantity == quantity else cost * tx.quantity / quantity
                realized += tx.quantity * tx.price - tx.fee - allocated
                quantity -= tx.quantity
                cost -= allocated
            states[key] = quantity, cost, realized
        return [Position(symbol, q, cost / q if q else None, cost, pnl, currency)
                for (symbol, currency), (q, cost, pnl) in sorted(states.items())]


def status(transactions, provider, config, as_of):
    """通貨ごとに集計。価格が欠ける通貨の合計評価額・構成比は算出しない。"""
    at = instant(as_of)
    if not isinstance(config, RuleConfig):
        raise PortfolioError("invalid_config")
    txs = list(transactions)
    if any(not isinstance(tx, Transaction) for tx in txs):
        raise PortfolioError("invalid_transaction")
    if any(tx.executed_at > at for tx in txs):
        raise PortfolioError("valuation_precedes_transaction")
    with localcontext(ARITHMETIC):
        positions, groups = [], {}
        for position in replay(txs):
            row = {key: number(value) if isinstance(value, Decimal) else value
                   for key, value in asdict(position).items()}
            row.update(current_price=None, market_value=None, unrealized_pnl=None,
                       unrealized_pnl_pct=None, portfolio_weight=None, daily_move=None,
                       snapshot=None, valuation_status="missing")
            snapshot = provider.snapshot(position.symbol, position.currency) if position.quantity else None
            if snapshot is not None:
                if (not isinstance(snapshot, MarketSnapshot)
                        or (snapshot.symbol, snapshot.currency) != (position.symbol, position.currency)):
                    raise PortfolioError("snapshot_identity_mismatch")
                row["snapshot"] = snapshot.to_dict()
                age = (at - snapshot.as_of).total_seconds()
                row["valuation_status"] = ("future" if age < 0 or (snapshot.ingested_at is not None and snapshot.ingested_at > at) else "stale"
                    if age > config.max_snapshot_age_seconds else "before_transaction"
                    if snapshot.as_of < max(tx.executed_at for tx in txs
                        if (tx.symbol, tx.currency) == (position.symbol, position.currency)) else "ok")
            group = groups.setdefault(position.currency, dict(total_cost=ZERO, realized_pnl=ZERO,
                known_market_value=ZERO, complete=True, unpriced_symbols=[]))
            group["total_cost"] += position.total_cost
            group["realized_pnl"] += position.realized_pnl
            if not position.quantity:
                row.update(valuation_status="closed", market_value="0", unrealized_pnl="0")
            elif row["valuation_status"] == "ok":
                value = position.quantity * snapshot.price
                row.update(current_price=number(snapshot.price), market_value=number(value),
                           unrealized_pnl=number(value - position.total_cost),
                           unrealized_pnl_pct=number((value-position.total_cost) / position.total_cost),
                           daily_move=number((snapshot.price-snapshot.previous_close) / snapshot.previous_close)
                           if snapshot.previous_close is not None else None)
                group["known_market_value"] += value
            else:
                group["complete"] = False
                group["unpriced_symbols"].append(position.symbol)
            row["stale"] = row["valuation_status"] == "stale"
            positions.append(row)
        totals = []
        for currency, group in sorted(groups.items()):
            value = group["known_market_value"] if group["complete"] else None
            for row in positions:
                if row["currency"] == currency and value is not None and value > 0:
                    row["portfolio_weight"] = number(Decimal(row["market_value"]) / value)
            totals.append(dict(currency=currency, total_cost=number(group["total_cost"]),
                market_value=number(value), unrealized_pnl=number(value-group["total_cost"]) if value is not None else None,
                realized_pnl=number(group["realized_pnl"]), complete=group["complete"],
                known_market_value=number(group["known_market_value"]), unpriced_symbols=group["unpriced_symbols"]))
        return dict(as_of=stamp(at), weight_basis="same_currency_open_positions",
                    positions=positions, currency_summaries=totals, rules=config.to_dict(),
                    portfolio_summary=dict(transaction_count=len(txs),
                        open_position_count=sum(Decimal(row["quantity"]) > 0 for row in positions),
                        closed_position_count=sum(Decimal(row["quantity"]) == 0 for row in positions),
                        currencies=sorted(groups), valuation_complete=all(g["complete"] for g in groups.values())))


def alerts(report, config):
    """到達・警戒・情報だけを返す。欠測をfalseや0に置き換えない。"""
    result = []
    with localcontext(ARITHMETIC):
        for row in report["positions"]:
            if Decimal(row["quantity"]) == 0:
                continue
            rules = [
                ("take_profit_threshold", "info", row["unrealized_pnl_pct"], config.take_profit_pct, "ge"),
                ("loss_warning_threshold", "warning", row["unrealized_pnl_pct"], config.loss_warning_pct, "le"),
                ("daily_move_threshold", "warning", row["daily_move"], config.daily_move_pct, "abs_ge"),
                ("concentration_threshold", "warning", row["portfolio_weight"], config.max_position_weight_pct, "gt"),
            ]
            for kind, severity, raw, threshold_pct, comparison in rules:
                if threshold_pct is None:
                    continue
                threshold = threshold_pct / 100
                value = Decimal(raw) if raw is not None else None
                triggered = None if value is None else {
                    "ge": lambda: value >= threshold, "le": lambda: value <= threshold,
                    "abs_ge": lambda: abs(value) >= threshold, "gt": lambda: value > threshold
                }[comparison]()
                result.append(dict(type=kind, symbol=row["symbol"], currency=row["currency"],
                    severity=severity, triggered=triggered, value=number(value), threshold=number(threshold),
                    stale=row["stale"], valuation_status=row["valuation_status"],
                    evaluation="not_evaluable" if value is None else "evaluated",
                    reason="price_or_weight_unavailable" if value is None else
                           "configured_threshold_reached" if triggered else "configured_threshold_not_reached"))
    return dict(as_of=report["as_of"], alerts=result)
