"""ニュースの構造化契約と純粋なAssessment拡張。通信・本文取得・AI評価はしない。"""
import copy
import ipaddress
import re
from dataclasses import dataclass, field, fields
from typing import Optional, Protocol
from urllib.parse import urlsplit

from .models import PortfolioError, instant, stamp, text, identity, object_from_json


@dataclass(frozen=True)
class NewsConfig:
    fresh_hours: int = 48
    max_articles_per_symbol: int = 5

    def __post_init__(self):
        if type(self.fresh_hours) is not int or not 0 <= self.fresh_hours <= 87600:
            raise PortfolioError("invalid_news_config")
        if type(self.max_articles_per_symbol) is not int or not 1 <= self.max_articles_per_symbol <= 50:
            raise PortfolioError("invalid_news_config")

    def to_dict(self):
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) - {f.name for f in fields(cls)}:
            raise PortfolioError("invalid_news_config")
        return cls(**value)


def validate_url(value):
    """http/httpsの絶対URLだけを受理する。解決・アクセス・ブラウザ起動はしない。"""
    try:
        text(value, 2048)
        if any(c.isspace() for c in value) or "\\" in value or re.search(r"%(?![0-9A-Fa-f]{2})", value):
            raise ValueError()
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError()
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError()
        host = parsed.hostname
        if ":" in host:
            ipaddress.IPv6Address(host)
        else:
            host = host.encode("idna").decode("ascii").rstrip(".")
            if len(host) > 253 or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                                      for label in host.split(".")):
                raise ValueError()
    except (ValueError, TypeError, UnicodeError):
        raise PortfolioError("invalid_news_url") from None
    return value


@dataclass(frozen=True)
class NewsArticle:
    id: str
    symbol: str
    title: str
    published_at: Optional[str]
    source: str
    url: str
    summary: str
    language: str
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        text(self.id, 128)
        identity(self.symbol, "JPY")
        text(self.title, 300)
        text(self.source, 120)
        validate_url(self.url)
        text(self.summary, 600, empty=True)
        if not isinstance(self.language, str) or not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", self.language) or len(self.language) > 35:
            raise PortfolioError("invalid_news_language")
        if self.published_at is not None:
            object.__setattr__(self, "published_at", stamp(instant(self.published_at)))
        if not isinstance(self.metadata, dict) or len(self.metadata) > 8:
            raise PortfolioError("invalid_news_metadata")
        for key, value in self.metadata.items():
            text(key, 64)
            text(value, 256, empty=True)
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self):
        return {f.name: copy.deepcopy(getattr(self, f.name)) for f in fields(self)}


class NewsProvider(Protocol):
    def articles(self, symbol: str):
        """明示されたsymbolに一致するNewsArticle列を返す。"""
        ...


class JsonNewsProvider:
    """ローカルfixture専用。本文fieldや不正入力を黙って捨てない。"""
    def __init__(self, articles=()):
        self._articles = []
        seen = set()
        for article in articles:
            if not isinstance(article, NewsArticle):
                raise PortfolioError("invalid_news_article")
            key = article.symbol, article.source, article.id
            if key in seen:
                raise PortfolioError("duplicate_news_article")
            seen.add(key)
            self._articles.append(copy.deepcopy(article))
            if len(self._articles) > 1000:
                raise PortfolioError("news_article_limit")

    def articles(self, symbol):
        return [copy.deepcopy(a) for a in self._articles if a.symbol == symbol]

    @classmethod
    def from_file(cls, path):
        with path.open("rb") as handle:
            raw = handle.read(1024*1024+1)
        if len(raw) > 1024*1024:
            raise PortfolioError("news_fixture_too_large")
        data = object_from_json(raw)
        if (not isinstance(data, dict) or set(data) != {"schema_version", "articles"}
                or type(data["schema_version"]) is not int or data["schema_version"] != 1
                or not isinstance(data["articles"], list) or len(data["articles"]) > 1000):
            raise PortfolioError("invalid_news_fixture")
        articles = []
        required = {f.name for f in fields(NewsArticle)}
        for row in data["articles"]:
            if not isinstance(row, dict) or set(row) != required:
                raise PortfolioError("invalid_news_fixture")
            articles.append(NewsArticle(**row))
        return cls(articles)


def enrich_news(assessment, articles, config=None):
    """既知の記事を件数制限して付加する。価格・イベント・severityは変更しない。"""
    config = config if config is not None else NewsConfig()
    if not isinstance(config, NewsConfig):
        raise PortfolioError("invalid_news_config")
    provider = JsonNewsProvider(articles)
    result = copy.deepcopy(assessment)
    at = instant(result["as_of"])
    for row in result["assessments"]:
        selected = [a for a in provider.articles(row["symbol"])
                    if a.published_at is None or instant(a.published_at) <= at]
        # 同時刻はsource/id昇順で決定的に並べ、日時不明を最後に置く。
        selected.sort(key=lambda a: (a.source, a.id))
        selected.sort(key=lambda a: (a.published_at is not None, a.published_at or ""), reverse=True)
        selected = selected[:config.max_articles_per_symbol]
        row["news"], row["news_flags"], row["news_reasons"] = [], [], []
        for article in selected:
            age = (at-instant(article.published_at)).total_seconds() if article.published_at else None
            freshness = "unknown" if age is None else "fresh" if age <= config.fresh_hours*3600 else "stale"
            item = article.to_dict()
            item.update(age_seconds=age, freshness_status=freshness, relevance="provider_symbol_exact")
            row["news"].append(item)
        fresh = [a for a in row["news"] if a["freshness_status"] == "fresh"]
        if fresh:
            row["news_flags"].append("recent_news")
            if len(fresh) >= 2:
                row["news_flags"].append("multiple_recent_news")
            for flag in row["news_flags"]:
                row["news_reasons"].append(dict(rule=flag, article_count=len(fresh),
                    fresh_hours=config.fresh_hours, latest_published_at=fresh[0]["published_at"],
                    sources=sorted({a["source"] for a in fresh}),
                    articles=[dict(id=a["id"], source=a["source"]) for a in fresh],
                    count_basis="returned_articles"))
    return result
