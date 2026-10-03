"""ネットワークに依存しない価格入力境界。原本や認証情報はDBへ渡さない。"""
from pathlib import Path
from typing import Optional, Protocol
from .models import MarketSnapshot, PortfolioError, object_from_json

class SnapshotProvider(Protocol):
    """同じ銘柄・通貨の価格を返す。未取得はNoneとし、補完しない。"""
    def snapshot(self, symbol: str, currency: str) -> Optional[MarketSnapshot]: ...


class JsonSnapshotProvider:
    """手動で用意したJSONまたはfake snapshotを評価エンジンへ渡す。"""
    def __init__(self, snapshots=()):
        self._snapshots = {}
        for snapshot in snapshots:
            if not isinstance(snapshot, MarketSnapshot):
                raise PortfolioError("invalid_snapshot")
            key = (snapshot.symbol, snapshot.currency)
            if key in self._snapshots:
                raise PortfolioError("duplicate_snapshot")
            self._snapshots[key] = snapshot

    def snapshot(self, symbol, currency):
        return self._snapshots.get((symbol, currency))

    @classmethod
    def from_file(cls, path):
        with Path(path).open("rb") as handle:
            raw = handle.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise PortfolioError("snapshot_too_large")
        data = object_from_json(raw)
        if (not isinstance(data, dict) or set(data) != {"schema_version", "snapshots"}
                or type(data["schema_version"]) is not int or data["schema_version"] != 1
                or not isinstance(data["snapshots"], list)):
            raise PortfolioError("invalid_snapshot_document")
        snapshots = []
        expected = {"symbol", "price", "previous_close", "currency", "as_of", "source"}
        for row in data["snapshots"]:
            if (not isinstance(row, dict) or not expected <= set(row)
                    or set(row) - expected - {"market", "data_date", "ingested_at"}):
                raise PortfolioError("invalid_snapshot")
            snapshots.append(MarketSnapshot(**row))
        return cls(snapshots)
