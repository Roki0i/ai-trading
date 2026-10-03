"""通貨・数量・時刻を明示した株式専用モデル。浮動小数点入力は拒否する。"""
import json
import re
import unicodedata
from dataclasses import dataclass, fields
from datetime import date, datetime, timezone
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Optional

ARITHMETIC = Context(prec=60, rounding=ROUND_HALF_EVEN)
CURRENCIES = frozenset("JPY USD EUR GBP CHF CAD AUD NZD HKD SGD CNY SEK NOK DKK INR KRW TWD".split())


class PortfolioError(ValueError):
    """入力本文やcredentialを含めない固定のエラーコード。"""


def decimal(value, *, zero=False):
    """18桁の整数部・12桁の小数部まで。float/bool/NaN/無限大を拒否する。"""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise PortfolioError("invalid_decimal")
    if len(str(value)) > 64:
        raise PortfolioError("invalid_decimal")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise PortfolioError("invalid_decimal") from None
    if (not result.is_finite() or result < 0 or (not zero and result == 0)
            or result >= Decimal("1e18") or result.as_tuple().exponent < -12):
        raise PortfolioError("invalid_decimal")
    return result


def number(value):
    """計算結果もJSONでは10進文字列にし、負のゼロと不要な末尾ゼロを除く。"""
    if value is None:
        return None
    if value == 0:
        return "0"
    return format(value, "f").rstrip("0").rstrip(".") if "." in format(value, "f") else format(value, "f")


def instant(value, *, date_only=False):
    """日付だけの取引日はUTC 00:00。日時はタイムゾーンを必須とする。"""
    try:
        if isinstance(value, str):
            if date_only and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                value += "T00:00:00+00:00"
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError()
        return value.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise PortfolioError("invalid_timestamp") from None


def stamp(value):
    return instant(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def now():
    return datetime.now(timezone.utc)


def text(value, limit, *, empty=False):
    if (not isinstance(value, str) or len(value) > limit or (not empty and not value.strip())
            or any(unicodedata.category(c).startswith("C") for c in value)):
        raise PortfolioError("invalid_text")
    return value


def identity(symbol, currency):
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,31}", symbol):
        raise PortfolioError("invalid_symbol")
    if not isinstance(currency, str) or currency not in CURRENCIES:
        raise PortfolioError("unsupported_currency")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def object_from_json(value):
    """重複キー・NaNを許容しないJSON読取り。"""
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise PortfolioError("duplicate_json_key")
            result[key] = item
        return result

    def invalid(_):
        raise PortfolioError("invalid_json")
    try:
        return json.loads(value, object_pairs_hook=pairs, parse_float=Decimal, parse_constant=invalid)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise PortfolioError("invalid_json") from None


@dataclass(frozen=True)
class Transaction:
    id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    currency: str
    executed_at: datetime
    note: str
    created_at: datetime
    asset_type: str = "equity"

    def __post_init__(self):
        if not isinstance(self.id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", self.id):
            raise PortfolioError("invalid_transaction_id")
        identity(self.symbol, self.currency)
        if self.asset_type != "equity" or self.side not in ("buy", "sell"):
            raise PortfolioError("unsupported_transaction")
        for name in ("quantity", "price", "fee"):
            object.__setattr__(self, name, decimal(getattr(self, name), zero=name == "fee"))
        object.__setattr__(self, "executed_at", instant(self.executed_at, date_only=True))
        object.__setattr__(self, "created_at", instant(self.created_at))
        text(self.note, 2000, empty=True)
        if self.executed_at > self.created_at:
            raise PortfolioError("future_transaction")

    def to_dict(self):
        result = {f.name: getattr(self, f.name) for f in fields(self)}
        for name in ("quantity", "price", "fee"):
            result[name] = number(result[name])
        for name in ("executed_at", "created_at"):
            result[name] = stamp(result[name])
        return result

    def economic_key(self):
        return canonical({key: value for key, value in self.to_dict().items()
                          if key not in ("id", "note", "created_at")})


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: Decimal
    average_cost: Optional[Decimal]
    total_cost: Decimal
    realized_pnl: Decimal
    currency: str


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    price: Decimal
    previous_close: Optional[Decimal]
    currency: str
    as_of: datetime
    source: str
    market: Optional[str] = None
    data_date: Optional[str] = None
    ingested_at: Optional[datetime] = None

    def __post_init__(self):
        identity(self.symbol, self.currency)
        object.__setattr__(self, "price", decimal(self.price))
        if self.previous_close is not None:
            object.__setattr__(self, "previous_close", decimal(self.previous_close))
        object.__setattr__(self, "as_of", instant(self.as_of))
        text(self.source, 120)
        if any(value is not None for value in (self.market, self.data_date, self.ingested_at)):
            text(self.market, 40)
            try:
                if date.fromisoformat(self.data_date).isoformat() != self.data_date:
                    raise ValueError()
            except (ValueError, TypeError):
                raise PortfolioError("invalid_data_date") from None
            object.__setattr__(self, "ingested_at", instant(self.ingested_at))
            if self.ingested_at < self.as_of:
                raise PortfolioError("snapshot_ingestion_precedes_data")

    def to_dict(self):
        result = dict(symbol=self.symbol, price=number(self.price),
                    previous_close=number(self.previous_close), currency=self.currency,
                    as_of=stamp(self.as_of), source=self.source)
        if self.data_date is not None:
            result.update(market=self.market, data_date=self.data_date, ingested_at=stamp(self.ingested_at))
        return result


@dataclass(frozen=True)
class RuleConfig:
    take_profit_pct: Optional[Decimal] = None
    loss_warning_pct: Optional[Decimal] = None
    daily_move_pct: Optional[Decimal] = None
    max_position_weight_pct: Optional[Decimal] = None
    max_snapshot_age_seconds: int = 86400

    def __post_init__(self):
        for name in ("take_profit_pct", "loss_warning_pct", "daily_move_pct", "max_position_weight_pct"):
            value = getattr(self, name)
            if value is None:
                continue
            if name == "loss_warning_pct":
                if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
                    raise PortfolioError("invalid_threshold")
                try:
                    value = Decimal(value)
                except InvalidOperation:
                    raise PortfolioError("invalid_threshold") from None
                if not value.is_finite() or not Decimal("-100") <= value < 0:
                    raise PortfolioError("invalid_threshold")
                value = decimal(value.copy_abs()).copy_negate()
            else:
                value = decimal(value)
                if name == "max_position_weight_pct" and value > 100:
                    raise PortfolioError("invalid_threshold")
            object.__setattr__(self, name, value)
        if type(self.max_snapshot_age_seconds) is not int or not 1 <= self.max_snapshot_age_seconds <= 31536000:
            raise PortfolioError("invalid_snapshot_age")

    def to_dict(self):
        return {f.name: (getattr(self, f.name) if f.name == "max_snapshot_age_seconds"
                         else number(getattr(self, f.name))) for f in fields(self)}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) - {f.name for f in fields(cls)}:
            raise PortfolioError("invalid_config")
        return cls(**value)
