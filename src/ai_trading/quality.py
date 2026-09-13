"""Fail closed on malformed observations; missing quotes are never forward-filled."""

import json
import math
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Iterable, Optional

from .models import Observation, timestamp


@dataclass(frozen=True)
class Issue:
    severity: str
    code: str
    entity_id: str
    detail: str

    def to_dict(self):
        return asdict(self)


def numeric(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def inspect_daily(rows: Iterable[Observation], *, expected_keys: Optional[set] = None) -> list[Issue]:
    rows = list(rows)
    issues, versions, observed = [], {}, set()

    def add(row, severity, code, detail):
        issues.append(Issue(severity, code, row.entity_id if row else "", detail))

    if not rows:
        add(None, "error", "empty_dataset", "No observations received")
    for row in rows:
        version = row.key + (row.revision_id,)
        if version in versions:
            code = "duplicate_revision" if versions[version] == row.payload_json else "revision_conflict"
            add(row, "error", code, "Repeated logical observation and revision")
        versions[version] = row.payload_json
        try:
            data = json.loads(row.payload_json)
            session = date.fromisoformat(data["session_date"])
            if row.dataset != "daily_bars" or row.entity_id != row.source + ":" + data["code"]:
                raise ValueError("identity mismatch")
            expected_event = str(session) + "T00:00:00+09:00"
            if row.event_at != timestamp(expected_event):
                raise ValueError("session event mismatch")
            # A complete daily bar cannot be known before its session has even begun.
            if row.available_at < row.event_at + timedelta(days=1):
                add(row, "warning", "intraday_availability", "Verify full-session completion against calendar")
            if row.available_at < row.event_at:
                add(row, "error", "future_bar", "Daily bar available before session")
            observed.add((row.entity_id, str(session)))
            values = [data[k] for k in ("open", "high", "low", "close")]
            missing = sum(value is None for value in values)
            volume = data["volume"]
            if missing:
                if missing == 4 and (volume is None or volume == 0):
                    add(row, "warning", "missing_quotes", "No quotes: halt/no-trade/missing not yet classified")
                else:
                    add(row, "error", "partial_quotes", "Incomplete OHLC with inconsistent volume")
            elif not all(numeric(value) and value > 0 for value in values):
                add(row, "error", "invalid_price", "OHLC must be finite positive numbers")
            else:
                opening, high, low, close = values
                if not low <= min(opening, close) <= max(opening, close) <= high:
                    add(row, "error", "ohlc_order", "OHLC outside low/high bounds")
            if volume is None:
                add(row, "warning", "missing_volume", "Volume unknown")
            elif not numeric(volume) or volume < 0:
                add(row, "error", "invalid_volume", "Volume must be finite and nonnegative")
            elif volume == 0:
                add(row, "warning", "zero_volume", "Do not assume tradability")
            factor = data.get("adjustment_factor")
            if factor is None:
                add(row, "warning", "missing_adjustment_factor", "Corporate-action coverage incomplete")
            elif not numeric(factor) or factor <= 0:
                add(row, "error", "invalid_adjustment_factor", "Adjustment factor must be positive")
        except (KeyError, TypeError, ValueError):
            add(row, "error", "invalid_schema", "Invalid daily observation payload")
    if expected_keys is None:
        add(None, "warning", "coverage_unverified", "Historical universe/calendar coverage not supplied")
    else:
        for entity, session in sorted(expected_keys - observed):
            issues.append(Issue("error", "missing_session", entity, session))
        for entity, session in sorted(observed - expected_keys):
            issues.append(Issue("error", "unexpected_session", entity, session))
    return issues
