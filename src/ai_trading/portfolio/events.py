"""企業イベントの契約、ローカルfixture、純粋なAssessment拡張。通信しない。"""
import copy
from dataclasses import dataclass, field, fields
from datetime import date, timedelta, timezone
from typing import Optional, Protocol

from .models import PortfolioError, instant, stamp, text, identity, canonical, object_from_json

JST = timezone(timedelta(hours=9))
TYPES = ("earnings", "dividend", "stock_split")
STATUSES = ("scheduled", "announced", "completed", "cancelled", "unknown")
FLAGS = {"earnings": "earnings_soon", "dividend": "dividend_soon", "stock_split": "stock_split_upcoming"}


@dataclass(frozen=True)
class EventConfig:
    earnings_soon_days: int = 7
    dividend_soon_days: int = 7
    stock_split_soon_days: int = 14
    max_announcement_age_days: int = 30
    recent_days: int = 7

    def __post_init__(self):
        for f in fields(self):
            value = getattr(self, f.name)
            if type(value) is not int or not 0 <= value <= 3660:
                raise PortfolioError("invalid_event_config")

    def to_dict(self):
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) - {f.name for f in fields(cls)}:
            raise PortfolioError("invalid_event_config")
        return cls(**value)


@dataclass(frozen=True)
class CorporateEvent:
    symbol: str
    event_type: str
    event_date: str
    announced_at: Optional[str]
    source: str
    status: str
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        identity(self.symbol, "JPY")
        if self.event_type not in TYPES or self.status not in STATUSES:
            raise PortfolioError("invalid_event_type_or_status")
        try:
            if not isinstance(self.event_date, str) or date.fromisoformat(self.event_date).isoformat() != self.event_date:
                raise ValueError()
        except (ValueError, TypeError):
            raise PortfolioError("invalid_event_date") from None
        if self.announced_at is not None:
            object.__setattr__(self, "announced_at", stamp(instant(self.announced_at)))
        text(self.source, 120)
        if not isinstance(self.metadata, dict) or len(self.metadata) > 16:
            raise PortfolioError("invalid_event_metadata")
        for key, value in self.metadata.items():
            text(key, 64)
            text(value, 256, empty=True)
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self):
        return {f.name: copy.deepcopy(getattr(self, f.name)) for f in fields(self)}


class CorporateEventProvider(Protocol):
    def events(self, symbol: str):
        """指定symbolのCorporateEvent列を返す。"""
        ...


class JsonEventProvider:
    """明示されたfixtureだけを読み込む。欠落ファイルを成功扱いしない。"""
    def __init__(self, events=()):
        self._events = []
        seen = set()
        for event in events:
            if not isinstance(event, CorporateEvent):
                raise PortfolioError("invalid_event")
            fingerprint = canonical(event.to_dict())
            if fingerprint in seen:
                raise PortfolioError("duplicate_event")
            seen.add(fingerprint)
            self._events.append(copy.deepcopy(event))
            if len(self._events) > 1000:
                raise PortfolioError("event_limit")

    def events(self, symbol):
        return [copy.deepcopy(e) for e in self._events if e.symbol == symbol]

    @classmethod
    def from_file(cls, path):
        with path.open("rb") as handle:
            raw = handle.read(1024*1024+1)
        if len(raw) > 1024*1024:
            raise PortfolioError("event_fixture_too_large")
        data = object_from_json(raw)
        if (not isinstance(data, dict) or set(data) != {"schema_version", "events"}
                or type(data["schema_version"]) is not int or data["schema_version"] != 1
                or not isinstance(data["events"], list) or len(data["events"]) > 1000):
            raise PortfolioError("invalid_event_fixture")
        result = []
        required = {f.name for f in fields(CorporateEvent)}
        for row in data["events"]:
            if not isinstance(row, dict) or set(row) != required:
                raise PortfolioError("invalid_event_fixture")
            result.append(CorporateEvent(**row))
        return cls(result)


def enrich_assessment(assessment, events, config=None):
    """評価時点で既知の予定だけを付加し、既存の損益・severityを変更しない。"""
    config = config if config is not None else EventConfig()
    if not isinstance(config, EventConfig):
        raise PortfolioError("invalid_event_config")
    provider = JsonEventProvider(events)
    result = copy.deepcopy(assessment)
    at = instant(result["as_of"])
    today = at.astimezone(JST).date()
    for row in result["assessments"]:
        row["events"], row["event_flags"], row["event_reasons"] = [], [], []
        for event in sorted(provider.events(row["symbol"]), key=lambda e: (e.event_date, e.event_type, e.announced_at or "", e.source)):
            announced = instant(event.announced_at) if event.announced_at else None
            # 評価時点で未発表の情報は過去のAssessmentに混入させない。
            if announced is not None and announced > at:
                continue
            age = (at-announced).total_seconds() if announced else None
            freshness = ("unknown" if age is None else "stale"
                         if age > config.max_announcement_age_days*86400 else "fresh")
            days = (date.fromisoformat(event.event_date)-today).days
            upcoming = days >= 0 and event.status in ("scheduled", "announced")
            recent = -config.recent_days <= days < 0 and event.status != "cancelled"
            item = event.to_dict()
            item.update(days_until=days, is_upcoming=upcoming, is_recent=recent, freshness_status=freshness)
            row["events"].append(item)
            threshold = getattr(config, event.event_type+"_soon_days")
            if upcoming and freshness == "fresh" and days <= threshold:
                flag = FLAGS[event.event_type]
                if flag not in row["event_flags"]:
                    row["event_flags"].append(flag)
                row["event_reasons"].append(dict(rule=flag, days_until=days, threshold_days=threshold,
                    comparison="0<=days_until<=threshold_days", event_date=event.event_date,
                    source=event.source, announced_at=event.announced_at, status=event.status,
                    freshness_status=freshness))
    return result
