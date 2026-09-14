"""Deterministic, non-AI targets from PIT close histories only."""
from decimal import Decimal


def target_weights(name, histories, universe, lookback, top_n):
    eligible = [symbol for symbol in universe if histories.get(symbol)]
    if name == "momentum":
        scores = []
        for symbol in eligible:
            prices = histories[symbol]
            if len(prices) >= lookback + 1:
                score = prices[-1] / prices[-lookback - 1] - 1
                if score > 0:
                    scores.append((score, symbol))
        eligible = [symbol for _, symbol in sorted(scores, key=lambda x: (-x[0], x[1]))[:top_n]]
    elif name not in ("buy_and_hold", "equal_weight"):
        raise ValueError("unknown strategy")
    return {symbol: Decimal(1) / len(eligible) for symbol in eligible}
