"""既存の評価結果と設定から条件到達を構造化する、副作用のないdomain層。"""
from decimal import Decimal

from .engine import alerts
from .models import RuleConfig, PortfolioError, instant, stamp

RULES = {
    "take_profit_threshold": ("take_profit_threshold", ">="),
    "loss_warning_threshold": ("loss_warning", "<="),
    "daily_move_threshold": ("daily_move_warning", "abs>="),
    "concentration_threshold": ("concentration_warning", ">"),
}
BLOCKING_ERRORS = frozenset(("market_corporate_action_requires_review", "market_adjustment_unknown"))
METRICS = ("quantity", "average_cost", "total_cost", "current_price", "market_value",
           "unrealized_pnl", "unrealized_pnl_pct", "realized_pnl", "portfolio_weight")


def assess(report, *, generated_at, blocked=None):
    """statusの出力だけを入力し、時計・DB・通信に依存せず評価を返す。"""
    generated = stamp(instant(generated_at))
    config = RuleConfig.from_dict(report["rules"])
    blocked = dict(blocked or {})
    open_rows = [r for r in report["positions"] if Decimal(r["quantity"]) > 0]
    missing_keys = {(r["symbol"], r["currency"]) for r in open_rows if r["valuation_status"] == "missing"}
    if any(key not in missing_keys or code not in BLOCKING_ERRORS for key, code in blocked.items()):
        raise PortfolioError("invalid_assessment_block")
    evaluations = alerts(report, config)["alerts"]
    assessments = []
    for row in open_rows:
        key = row["symbol"], row["currency"]
        valuation = row["valuation_status"]
        state = ("blocked" if key in blocked else
                 {"ok": "fresh", "stale": "stale", "missing": "missing",
                  "future": "blocked", "before_transaction": "blocked"}[valuation])
        snapshot = row["snapshot"] or {}
        metadata = {name: snapshot.get(name) for name in
                    ("as_of", "source", "market", "data_date", "ingested_at")}
        metadata.update(evaluated_at=report["as_of"],
                        max_snapshot_age_seconds=config.max_snapshot_age_seconds)
        reasons, flags, triggered = [], [], []
        severity = "critical" if state == "blocked" else "warning" if state != "fresh" else "info"
        if state != "fresh":
            reasons.append(dict(kind="market_data", code=blocked.get(key, valuation),
                                status=state, **metadata))
        for alert in evaluations:
            if (alert["symbol"], alert["currency"]) != key:
                continue
            flag, comparison = RULES[alert["type"]]
            reason = dict(kind="rule", rule=alert["type"], flag=flag,
                          value=alert["value"], threshold=alert["threshold"], comparison=comparison,
                          triggered=alert["triggered"], evaluation=alert["evaluation"],
                          code=alert["reason"], data_date=metadata["data_date"],
                          source=metadata["source"], as_of=metadata["as_of"])
            if alert["triggered"] is None:
                reason["code"] = ("previous_close_unavailable" if state == "fresh" and flag == "daily_move_warning"
                                  else "currency_valuation_incomplete" if state == "fresh" and flag == "concentration_warning"
                                  else "market_data_unavailable")
            reasons.append(reason)
            if alert["triggered"]:
                flags.append(flag)
                triggered.append(alert["type"])
            if severity != "critical" and (alert["triggered"] is None or
                    (alert["triggered"] and alert["severity"] == "warning")):
                severity = "warning"
        metrics = {name: row[name] for name in METRICS}
        metrics["daily_move_pct"] = row["daily_move"]
        assessments.append(dict(symbol=row["symbol"], currency=row["currency"], metrics=metrics,
            market_data_status=state, market_snapshot=metadata, triggered_rules=triggered,
            assessment_flags=flags, severity=severity, reasons=reasons, generated_at=generated))
    currencies = []
    for group in report["currency_summaries"]:
        rows = [r for r in assessments if r["currency"] == group["currency"]]
        summary = {name: group[name] for name in
                   ("currency", "total_cost", "market_value", "unrealized_pnl", "realized_pnl")}
        summary.update(number_of_positions=len(rows),
            stale_position_count=sum(r["market_data_status"] == "stale" for r in rows),
            missing_price_count=sum(r["market_data_status"] == "missing" for r in rows),
            blocked_position_count=sum(r["market_data_status"] == "blocked" for r in rows),
            concentration_flags=[r["symbol"] for r in rows if "concentration_warning" in r["assessment_flags"]])
        currencies.append(summary)
    return dict(schema_version=1, generated_at=generated, as_of=report["as_of"],
                portfolio=dict(currencies=currencies, weight_basis=report["weight_basis"]),
                assessments=assessments)
