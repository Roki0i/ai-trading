"""Offline weekly, cash-only Japanese equity simulation using strict PIT data."""
import argparse
import json
import platform
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Context, Decimal, localcontext
from pathlib import Path
from typing import Optional

from .cli import code_fingerprint
from .metrics import performance
from .models import as_of, timestamp
from .quality import inspect_daily
from .storage import canonical, digest, load_observations, put, read_verified, save_observations
from .strategies import target_weights


def decimal(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("finite decimal required")
    return result


@dataclass(frozen=True)
class BacktestConfig:
    sessions: tuple
    universe: tuple
    strategy: str = "equal_weight"
    initial_cash: str = "1000000"
    lot_size: int = 100
    commission_bps: str = "0"
    commission_fixed: str = "0"
    slippage_bps: str = "0"
    lookback: int = 20
    top_n: int = 5
    pit_mode: str = "observed"
    knowledge_at: Optional[str] = None
    ml: Optional[dict] = None
    market: Optional[dict] = None

    def __post_init__(self):
        object.__setattr__(self, "sessions", tuple(self.sessions))
        object.__setattr__(self, "universe", tuple(sorted(self.universe)))
        if not self.sessions or tuple(sorted(set(self.sessions))) != self.sessions:
            raise ValueError("sessions must be nonempty, sorted and unique")
        for session in self.sessions:
            if date.fromisoformat(session).isoformat() != session:
                raise ValueError("use ISO session dates")
        if not self.universe or len(set(self.universe)) != len(self.universe):
            raise ValueError("unique nonempty universe required")
        if any(not isinstance(s, str) or not s.strip() for s in self.universe):
            raise ValueError("invalid universe")
        if self.strategy not in ("buy_and_hold", "equal_weight", "momentum", "ml"):
            raise ValueError("unknown strategy")
        if self.strategy == "ml" and self.ml is None:
            raise ValueError("ml configuration required")
        if self.ml is not None:
            from .ml import MLConfig
            object.__setattr__(self, "ml", json.loads(canonical(asdict(MLConfig(**self.ml)))))
        for name in ("lot_size", "lookback", "top_n"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(name + " must be a positive integer")
        for name in ("initial_cash", "commission_bps", "commission_fixed", "slippage_bps"):
            value = decimal(getattr(self, name))
            if value < 0 or (name == "initial_cash" and value == 0):
                raise ValueError("invalid " + name)
            object.__setattr__(self, name, str(value))
        if decimal(self.slippage_bps) >= 10000 or decimal(self.commission_bps) >= 10000:
            raise ValueError("basis-point costs must be below 10000")
        if self.pit_mode not in ("observed", "historical"):
            raise ValueError("unknown PIT mode")
        if self.pit_mode == "historical" and self.knowledge_at is None:
            raise ValueError("historical mode requires knowledge_at")
        if self.knowledge_at is not None:
            timestamp(self.knowledge_at)
        if self.market is not None:
            from .market import validate_market
            if self.pit_mode != "observed":
                raise ValueError("Phase 5 requires observed PIT")
            validate_market(self.market, self.sessions, self.universe)


def _bars(rows, cutoff, config):
    selected = as_of(rows, cutoff, mode=config.pit_mode,
                     knowledge_at=timestamp(config.knowledge_at) if config.knowledge_at else None)
    result = {}
    for row in selected:
        if row.entity_id not in config.universe or row.dataset != "daily_bars":
            continue
        data = json.loads(row.payload_json)
        if row.event_at >= cutoff:
            continue
        errors = [i.code for i in inspect_daily([row]) if i.severity == "error"]
        if errors:
            raise ValueError("invalid PIT bar: " + ", ".join(errors))
        if data.get("adjustment_factor") != 1:
            from .market import actions
            matches = [json.loads(a.payload_json) for a in actions(config, cutoff)
                       if a.entity_id == row.entity_id and json.loads(a.payload_json)['effective_date'] == data['session_date']
                       and json.loads(a.payload_json)['kind'] in ('stock_split', 'reverse_split')]
            if data.get('adjustment_factor') is None or len(matches) != 1 or decimal(data.get('adjustment_factor')) * decimal(matches[0]['ratio']) != 1:
                raise ValueError("corporate actions/unknown adjustment factors are unsupported")
        key = (row.entity_id, data["session_date"])
        if key in result:
            raise ValueError("ambiguous market bar")
        result[key] = data
    return result


def run_backtest(rows, config):
    # Keep financial arithmetic independent of the caller's Decimal context.
    with localcontext(Context(prec=28)):
        rows = list(rows)
        if config.market is not None:
            from .market import validate_market
            validate_market(config.market, config.sessions, config.universe)
        if config.ml is None:
            return _run(rows, config)
        from .ml import MLConfig, walk_forward
        cfg = MLConfig(**config.ml)
        research = walk_forward(rows, config, cfg)
        signals = {}
        for p in research['predictions']:
            if p['probability'] >= 0.5:
                signals.setdefault(p['session'], []).append(p['symbol'])
        result = _run(rows, config, signals, cfg.first_prediction)
        result['ml_research'] = research
        return result


def _run(rows, config, signals=None, evaluation_start=0):
    cash = decimal(config.initial_cash)
    positions, marks, orders, fills, snapshots = {}, {}, [], [], []
    rate = decimal(config.commission_bps) / 10000
    fixed = decimal(config.commission_fixed)
    slip = decimal(config.slippage_bps) / 10000
    previous_week, bought = None, False
    events, missing, applied, receivables, halts = [], [], {}, [], {}
    from .market import membership, actions, known, crosses_action
    for session in config.sessions[evaluation_start:]:
        cutoff = timestamp(session + "T18:00:00+09:00")
        eligible_universe = membership(config, session, cutoff)
        bars = _bars(rows, cutoff, config)
        current = {s: b for (s, d), b in bars.items() if d == session}
        if config.market is not None:
            # Entitlements belong to holders before the effective session's executions.
            for action in actions(config, cutoff):
                p = json.loads(action.payload_json)
                effective = p['effective_date']
                if effective > session:
                    continue
                key = action.key
                if key in applied:
                    if applied[key] != action.payload_json:
                        raise ValueError('revision of applied corporate action')
                    continue
                if effective < session:
                    if effective >= config.sessions[evaluation_start]:
                        raise ValueError('late corporate action with position')
                    applied[key] = action.payload_json
                    continue
                symbol = action.entity_id
                if p['kind'] in ('stock_split', 'reverse_split'):
                    b = current.get(symbol)
                    if b is None or b.get('adjustment_factor') is None or decimal(b['adjustment_factor']) * decimal(p['ratio']) != 1:
                        raise ValueError('split/raw adjustment factor inconsistent')
                q = positions.get(symbol, 0)
                if p['kind'] == 'dividend':
                    receivables.append(dict(symbol=symbol, amount=decimal(p['cash_per_share'])*q,
                                            payment_date=p['payment_date']))
                else:
                    ratio = decimal(p['ratio'])
                    new_q = q * ratio
                    if new_q != int(new_q):
                        raise ValueError('fractional split entitlement requires cash-in-lieu evidence')
                    if q:
                        positions[symbol] = int(new_q)
                        marks[symbol] = (marks[symbol][0]/ratio, marks[symbol][1])
                    for o in orders:
                        if o['symbol'] == symbol and o['status'] == 'pending':
                            o.update(status='cancelled', resolved_session=session, reason='corporate_action')
                applied[key] = action.payload_json
                events.append(dict(session=session, symbol=symbol, kind=p['kind'], quantity_before=q))
            for r in list(receivables):
                if r['payment_date'] <= session:
                    cash += r['amount']
                    events.append(dict(session=session, symbol=r['symbol'], kind='dividend_payment', amount=str(r['amount'])))
                    receivables.remove(r)
            for r in known(config.market, 'historical_universe', cutoff):
                p = json.loads(r.payload_json)
                if p['delisting_date'] and p['delisting_date'] <= session and positions.get(r.entity_id):
                    raise ValueError('delisting with open position: explicit liquidation evidence required')
            for symbol in eligible_universe:
                b = current.get(symbol)
                reason = ('api_gap' if b is None else 'missing_price' if b['close'] is None else
                          'missing_volume' if b['volume'] is None else 'trading_halt' if b['volume'] == 0 else None)
                if reason:
                    missing.append(dict(session=session, symbol=symbol, reason=reason))
                halts[symbol] = halts.get(symbol, 0)+1 if reason else 0
                if positions.get(symbol) and (b is None or b['close'] is None):
                    raise ValueError('missing held price: valuation stopped; '+symbol+' '+session+' '+reason)
                if positions.get(symbol) and halts[symbol] > config.market['missing_data_policy']['max_halt_sessions']:
                    raise ValueError('long trading halt: valuation stopped')
            for o in orders:
                if o['status'] == 'pending' and o['symbol'] not in eligible_universe:
                    o.update(status='cancelled', resolved_session=session, reason='universe_exit')
        # Orders created at an earlier session can execute only on a new session.
        pending = [o for o in orders if o["status"] == "pending" and o["decision_session"] < session]
        for order in sorted(pending, key=lambda o: (o["side"] != "sell", o["symbol"])):
            symbol, side = order["symbol"], order["side"]
            bar = current.get(symbol)
            if not bar or bar["close"] is None or not bar["volume"] or bar["volume"] <= 0:
                continue
            reference = decimal(bar["close"])
            price = reference * (1 + slip if side == "buy" else 1 - slip)
            quantity = order["quantity"]
            if side == "buy":
                affordable = max(0, int((cash - fixed) / (price * (1 + rate) * config.lot_size))) * config.lot_size
                quantity = min(quantity, affordable)
            else:
                quantity = min(quantity, positions.get(symbol, 0))
                if price * quantity * (1 - rate) < fixed:
                    quantity = 0
            if quantity == 0:
                order.update(status="rejected", resolved_session=session, reason="cash_or_fee_constraint")
                continue
            notional = price * quantity
            fee = notional * rate + fixed
            cash += notional - fee if side == "sell" else -notional - fee
            positions[symbol] = positions.get(symbol, 0) + (quantity if side == "buy" else -quantity)
            if not positions[symbol]:
                del positions[symbol]
            order.update(status="filled" if quantity == order["quantity"] else "partial_cancelled",
                         resolved_session=session)
            fills.append(dict(order_id=order["id"], session=session, symbol=symbol, side=side,
                              quantity=quantity, reference_price=str(reference), price=str(price),
                              notional=str(notional), commission=str(fee),
                              slippage_cost=str(abs(price - reference) * quantity), cash_after=str(cash)))
        for symbol, bar in current.items():
            if bar["close"] is not None:
                marks[symbol] = (decimal(bar["close"]), session)
        equity = cash + sum(marks[s][0] * q for s, q in positions.items()) + sum(r["amount"] for r in receivables)
        snapshots.append(dict(session=session, cash=str(cash), positions=dict(sorted(positions.items())),
                              marks={s: str(marks[s][0]) for s in sorted(positions)},
                              stale_marks=[s for s in sorted(positions) if marks[s][1] != session],
                              unavailable_symbols=[s for s in config.universe
                                                   if s not in current or current[s]["close"] is None],
                              equity=str(equity)))
        week = date.fromisoformat(session).isocalendar()[:2]
        if week == previous_week:
            continue
        previous_week = week
        if config.strategy == "buy_and_hold" and bought:
            continue
        for order in orders:
            if order["status"] == "pending":
                order.update(status="cancelled", resolved_session=session, reason="weekly_replacement")
        histories = {}
        for symbol in eligible_universe:
            # Require today's price and contiguous configured sessions for momentum.
            if symbol not in current or current[symbol]["close"] is None:
                continue
            dates = [d for d in config.sessions if d <= session]
            if config.strategy == "momentum":
                dates = dates[-config.lookback - 1:]
            if config.strategy == 'ml':
                dates = dates[-61:]
            required_history = 61 if config.strategy == 'ml' else config.lookback+1
            if config.market is not None and config.strategy in ('momentum', 'ml') and (
                    len(dates) < required_history or symbol not in membership(
                        config, dates[0], timestamp(dates[0]+'T18:00:00+09:00')) or crosses_action(config, symbol, dates[0], session, cutoff)):
                missing.append(dict(session=session, symbol=symbol, reason='insufficient_or_action_crossing_history'))
                continue
            prices = [bars.get((symbol, d), {}).get("close") for d in dates]
            if config.strategy == "momentum" and any(p is None for p in prices):
                continue
            histories[symbol] = [decimal(p) for p in prices if p is not None]
        if config.strategy == 'ml':
            eligible = [s for s in signals.get(session, []) if s in histories]
            weights = {s: Decimal(1)/len(eligible) for s in eligible}
        else:
            weights = target_weights(config.strategy, histories, eligible_universe, config.lookback, config.top_n)
        for symbol in sorted(set(weights) | set(positions)):
            target = int(equity * weights[symbol] / marks[symbol][0] / config.lot_size) * config.lot_size if symbol in weights else 0
            difference = target - positions.get(symbol, 0)
            if difference:
                orders.append(dict(id=len(orders) + 1, decision_session=session,
                                   decision_at=cutoff.isoformat(), symbol=symbol,
                                   side="buy" if difference > 0 else "sell", quantity=abs(difference), status="pending"))
        if config.strategy == "buy_and_hold" and weights:
            bought = True
    result = dict(schema_version=1, config=asdict(config), orders=orders, fills=fills,
                  snapshots=snapshots, metrics=performance(config.initial_cash, snapshots, fills))
    if config.market is not None:
        from .market import provenance
        from .statistics import interval, returns
        import statistics
        result.update(market_provenance=provenance(config.market), corporate_action_ledger=events,
                      missing_data_events=missing,
                      dividend_receivables=[dict(r, amount=str(r['amount'])) for r in receivables])
        result['statistical_evaluation'] = dict(mean_daily_return=interval(
            [v for d,v in returns(result)], statistics.mean, config.market['statistical_evaluation']))
    return result


def save_experiment(rows, config, output):
    rows = list(rows)
    result = run_backtest(rows, config)
    data_path = save_observations(output / "inputs", rows)
    result_path = put(output / "results", canonical(result))
    manifest = dict(schema_version=1, kind="non_ai_backtest", config=asdict(config),
                    config_sha256=digest(canonical(asdict(config))), input_sha256=data_path.stem,
                    result_sha256=result_path.stem, code_sha256=code_fingerprint(),
                    python=platform.python_version(), policy="JP_daily_cash_weekly_next_session_close_v1")
    return put(output / "manifests", canonical(manifest))


def replay_experiment(manifest_path):
    """Verify frozen inputs, config, code and result before deterministic replay."""
    manifest = json.loads(read_verified(manifest_path))
    if manifest["kind"] != "non_ai_backtest" or manifest["schema_version"] != 1:
        raise ValueError("unsupported manifest")
    if manifest["code_sha256"] != code_fingerprint():
        raise ValueError("code fingerprint changed")
    if manifest["python"] != platform.python_version():
        raise ValueError("Python version changed")
    if manifest["config_sha256"] != digest(canonical(manifest["config"])):
        raise ValueError("configuration hash mismatch")
    root = manifest_path.parent.parent
    rows = load_observations(root / "inputs" / (manifest["input_sha256"] + ".jsonl"))
    expected = read_verified(root / "results" / (manifest["result_sha256"] + ".json"))
    result = run_backtest(rows, BacktestConfig(**manifest["config"]))
    if canonical(result) != expected:
        raise ValueError("backtest result is not reproducible")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--data", required=True, type=Path, help="verified processed JSONL")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    config = BacktestConfig(**json.loads(args.config.read_text()))
    print(save_experiment(load_observations(args.data), config, args.output))


if __name__ == "__main__":
    main()
