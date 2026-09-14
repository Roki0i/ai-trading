"""Daily net-of-cost performance; Sharpe uses zero risk-free return."""
import math
import statistics
from datetime import date


def performance(initial_cash, snapshots, fills, annual_sessions=252):
    values = [float(initial_cash)] + [float(s["equity"]) for s in snapshots]
    returns = [b / a - 1 for a, b in zip(values, values[1:])]
    peak, drawdown = values[0], 0.0
    for value in values:
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1)
    std = statistics.stdev(returns) if len(returns) >= 2 else 0.0
    days = (date.fromisoformat(snapshots[-1]["session"]) -
            date.fromisoformat(snapshots[0]["session"])).days
    ratio = values[-1] / values[0]
    return {
        "cumulative_return": ratio - 1,
        "cagr": ratio ** (365.25 / days) - 1 if days else None,
        "max_drawdown": -drawdown,
        "annualized_volatility": std * math.sqrt(annual_sessions),
        "sharpe_ratio": statistics.mean(returns) / std * math.sqrt(annual_sessions) if std else None,
        "turnover": sum(float(f["notional"]) for f in fills) / statistics.mean(values[1:]),
    }
