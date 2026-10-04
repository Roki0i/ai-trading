"""取得境界で企業行動による停止だけを記録し、評価可能なsnapshotを固定する。"""
from .assessment import BLOCKING_ERRORS
from .market import JsonSnapshotProvider
from .models import PortfolioError, MarketSnapshot


def collect_snapshots(positions, provider):
    """認証・通信・形式エラーは伝播させる。価格や調整係数は補完しない。"""
    snapshots, blocked = [], {}
    for position in positions:
        if not position.quantity:
            continue
        key = position.symbol, position.currency
        try:
            snapshot = provider.snapshot(*key)
        except PortfolioError as exc:
            if str(exc) not in BLOCKING_ERRORS:
                raise
            blocked[key] = str(exc)
            continue
        if snapshot is not None:
            if not isinstance(snapshot, MarketSnapshot) or (snapshot.symbol, snapshot.currency) != key:
                raise PortfolioError("snapshot_identity_mismatch")
            snapshots.append(snapshot)
    return JsonSnapshotProvider(snapshots), blocked
