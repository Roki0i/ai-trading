"""Immutable point-in-time observations and explicit availability policies."""

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Iterable, Optional


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware datetime required")
    return value.astimezone(timezone.utc)


def timestamp(value: str) -> datetime:
    return utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


@dataclass(frozen=True)
class Observation:
    dataset: str
    entity_id: str
    event_at: datetime
    published_at: Optional[datetime]
    available_at: datetime
    ingested_at: datetime
    revision_id: str
    source: str
    payload_json: str
    availability_basis: str = "observed"
    availability_evidence: str = ""

    def __post_init__(self):
        for name in ("event_at", "available_at", "ingested_at", "published_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, utc(value))
        for name in ("dataset", "entity_id", "revision_id", "source"):
            if not getattr(self, name).strip():
                raise ValueError(name + " must not be empty")
        payload = json.loads(self.payload_json)
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        if self.availability_basis not in ("observed", "documented", "estimated"):
            raise ValueError("unknown availability_basis")
        if self.published_at is not None and self.published_at > self.available_at:
            raise ValueError("available_at precedes publication")
        if self.availability_basis == "observed" and self.available_at < self.ingested_at:
            raise ValueError("observed availability precedes ingestion")
        if self.availability_basis != "observed":
            if self.published_at is None or not self.availability_evidence.strip():
                raise ValueError("historical availability requires publication and evidence")

    @property
    def key(self):
        # Cross-provider precedence must be defined explicitly before merging sources.
        return self.dataset, self.source, self.entity_id, self.event_at


def as_of(
    rows: Iterable[Observation], decision_at: datetime,
    *, mode: str = "observed", knowledge_at: Optional[datetime] = None,
    allow_estimated: bool = False,
) -> list[Observation]:
    """Latest version strictly available BEFORE a decision, independent of input order.

    observed: enforce actual local ingestion as well as source availability.
    historical: permit evidenced historical availability within a frozen knowledge cutoff.
    Future event dates are allowed for announced events (e.g. a future dividend).
    """
    decision_at = utc(decision_at)
    if mode not in ("observed", "historical"):
        raise ValueError("unknown PIT mode")
    if mode == "historical" and knowledge_at is None:
        raise ValueError("historical mode requires knowledge_at")
    if knowledge_at is not None:
        knowledge_at = utc(knowledge_at)
    selected, versions = {}, {}
    for row in rows:
        if row.available_at >= decision_at:
            continue
        if mode == "observed" and row.ingested_at >= decision_at:
            continue
        if knowledge_at is not None and row.ingested_at > knowledge_at:
            continue
        if row.availability_basis == "estimated" and not allow_estimated:
            continue
        version_key = row.key + (row.available_at,)
        simultaneous = versions.get(version_key)
        if simultaneous is not None and replace(row, ingested_at=simultaneous.ingested_at) != simultaneous:
            raise ValueError("ambiguous revisions at identical available_at")
        versions[version_key] = row
        old = selected.get(row.key)
        if old is None or row.available_at > old.available_at:
            selected[row.key] = row
        elif row.available_at == old.available_at:
            # No lexicographic revision ordering: ambiguous simultaneous revisions fail.
            if replace(row, ingested_at=old.ingested_at) != old:
                raise ValueError("ambiguous revisions at identical available_at")
            if row.ingested_at < old.ingested_at:
                selected[row.key] = row
    return [selected[key] for key in sorted(selected)]
