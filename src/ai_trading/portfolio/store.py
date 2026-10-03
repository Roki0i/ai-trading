"""専用SQLiteの取引台帳。既存研究DBの自動移行・修復・再利用はしない。"""
import hashlib
import os
import sqlite3
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from .engine import replay
from .models import Transaction, RuleConfig, PortfolioError, canonical, object_from_json, now

APPLICATION_ID = 0x50544637
SCHEMA_VERSION = 1
GENESIS = hashlib.sha256(b"user-portfolio-v1").hexdigest()
SCHEMA = """
CREATE TABLE instruments (
 symbol TEXT NOT NULL, currency TEXT NOT NULL,
 asset_type TEXT NOT NULL CHECK(asset_type='equity'),
 PRIMARY KEY(symbol,currency)
);
CREATE TABLE transactions (
 sequence INTEGER PRIMARY KEY CHECK(sequence>0),
 id TEXT NOT NULL UNIQUE,
 symbol TEXT NOT NULL, currency TEXT NOT NULL,
 side TEXT NOT NULL CHECK(side IN ('buy','sell')),
 quantity TEXT NOT NULL CHECK(typeof(quantity)='text' AND length(quantity) BETWEEN 1 AND 64),
 price TEXT NOT NULL CHECK(typeof(price)='text' AND length(price) BETWEEN 1 AND 64),
 fee TEXT NOT NULL CHECK(typeof(fee)='text' AND length(fee) BETWEEN 1 AND 64),
 executed_at TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL,
 economic_hash TEXT NOT NULL UNIQUE CHECK(length(economic_hash)=64),
 previous_hash TEXT NOT NULL CHECK(length(previous_hash)=64),
 entry_hash TEXT NOT NULL CHECK(length(entry_hash)=64),
 FOREIGN KEY(symbol,currency) REFERENCES instruments(symbol,currency)
);
CREATE TABLE metadata (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 entry_count INTEGER NOT NULL CHECK(entry_count>=0),
 head TEXT NOT NULL CHECK(length(head)=64)
);
CREATE TABLE rules (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 body TEXT NOT NULL, hash TEXT NOT NULL CHECK(length(hash)=64)
);
CREATE TRIGGER transactions_no_update BEFORE UPDATE ON transactions BEGIN
 SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER transactions_no_delete BEFORE DELETE ON transactions BEGIN
 SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER instruments_no_update BEFORE UPDATE ON instruments BEGIN
 SELECT RAISE(ABORT, 'immutable_instrument'); END;
CREATE TRIGGER instruments_no_delete BEFORE DELETE ON instruments BEGIN
 SELECT RAISE(ABORT, 'immutable_instrument'); END;
"""


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def schema_rows(db):
    return [tuple(row) for row in db.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name")]


@lru_cache(maxsize=1)
def expected_schema():
    with sqlite3.connect(":memory:") as db:
        db.executescript(SCHEMA)
        result = schema_rows(db)
    db.close()
    return result


class PortfolioStore:
    """1操作1接続・1transaction。書込み前に台帳全体を検証して再生する。"""
    def __init__(self, path):
        self.path = Path(path).absolute()

    @classmethod
    def initialize(cls, path):
        store = cls(path)
        store.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(store.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise PortfolioError("database_already_exists") from None
        os.close(fd)
        db = None
        try:
            db = sqlite3.connect(store.path)
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript("BEGIN IMMEDIATE;" + SCHEMA)
            db.execute("PRAGMA application_id=" + str(APPLICATION_ID))
            db.execute("PRAGMA user_version=" + str(SCHEMA_VERSION))
            db.execute("INSERT INTO metadata VALUES (1,0,?)", (GENESIS,))
            body = canonical(RuleConfig().to_dict())
            db.execute("INSERT INTO rules VALUES (1,?,?)", (body, digest(body)))
            db.commit()
        except sqlite3.Error:
            if db:
                db.rollback()
                db.close()
                db = None
            # 自分が排他的に作成した未完成DBだけを除去する。
            store.path.unlink(missing_ok=True)
            raise PortfolioError("storage_error") from None
        finally:
            if db:
                db.close()
        return store

    @contextmanager
    def _connect(self, *, write=False):
        if not self.path.is_file():
            raise PortfolioError("database_not_initialized")
        db = None
        try:
            db = sqlite3.connect(self.path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                 uri=True, timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.execute("COMMIT" if write else "ROLLBACK")
        except sqlite3.Error:
            if db and db.in_transaction:
                db.rollback()
            raise PortfolioError("storage_error") from None
        except BaseException:
            if db and db.in_transaction:
                db.rollback()
            raise
        finally:
            if db:
                db.close()

    def _read(self, db):
        if (db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
                or db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION):
            raise PortfolioError("unsupported_database")
        if schema_rows(db) != expected_schema():
            raise PortfolioError("schema_integrity_error")
        if (db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                or db.execute("PRAGMA foreign_key_check").fetchall()):
            raise PortfolioError("database_integrity_error")
        meta = db.execute("SELECT * FROM metadata").fetchall()
        rules = db.execute("SELECT * FROM rules").fetchall()
        if len(meta) != 1 or len(rules) != 1:
            raise PortfolioError("database_integrity_error")
        body = rules[0]["body"]
        if not isinstance(body, str) or digest(body) != rules[0]["hash"]:
            raise PortfolioError("config_integrity_error")
        config = RuleConfig.from_dict(object_from_json(body))
        if canonical(config.to_dict()) != body:
            raise PortfolioError("config_integrity_error")
        previous, transactions = GENESIS, []
        for sequence, row in enumerate(db.execute("SELECT * FROM transactions ORDER BY sequence"), 1):
            values = {name: row[name] for name in (
                "id", "symbol", "side", "quantity", "price", "fee", "currency",
                "executed_at", "note", "created_at")}
            try:
                tx = Transaction(**values)
            except (ValueError, TypeError):
                raise PortfolioError("ledger_integrity_error") from None
            encoded = tx.to_dict()
            if any(encoded[name] != value for name, value in values.items()):
                raise PortfolioError("ledger_integrity_error")
            expected = digest(canonical(dict(sequence=sequence, previous_hash=previous, transaction=encoded)))
            if (row["sequence"] != sequence or row["previous_hash"] != previous
                    or row["entry_hash"] != expected or row["economic_hash"] != digest(tx.economic_key())):
                raise PortfolioError("ledger_integrity_error")
            transactions.append(tx)
            previous = expected
        if meta[0]["entry_count"] != len(transactions) or meta[0]["head"] != previous:
            raise PortfolioError("ledger_integrity_error")
        instruments = {(row["symbol"], row["currency"], row["asset_type"])
                       for row in db.execute("SELECT * FROM instruments")}
        if instruments != {(tx.symbol, tx.currency, tx.asset_type) for tx in transactions}:
            raise PortfolioError("ledger_integrity_error")
        replay(transactions)
        return transactions, config

    def read(self):
        with self._connect() as db:
            return self._read(db)

    def add(self, transaction):
        if not isinstance(transaction, Transaction) or transaction.created_at > now():
            raise PortfolioError("invalid_transaction")
        with self._connect(write=True) as db:
            transactions, _ = self._read(db)
            replay(transactions + [transaction])
            sequence = len(transactions) + 1
            previous = db.execute("SELECT head FROM metadata").fetchone()[0]
            tx = transaction.to_dict()
            entry_hash = digest(canonical(dict(sequence=sequence, previous_hash=previous, transaction=tx)))
            db.execute("INSERT OR IGNORE INTO instruments VALUES (?,?,?)",
                       (transaction.symbol, transaction.currency, transaction.asset_type))
            db.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sequence, tx["id"], tx["symbol"], tx["currency"], tx["side"],
                 tx["quantity"], tx["price"], tx["fee"], tx["executed_at"], tx["note"], tx["created_at"],
                 digest(transaction.economic_key()), previous, entry_hash))
            db.execute("UPDATE metadata SET entry_count=?,head=? WHERE singleton=1", (sequence, entry_hash))
        return transaction

    def configure(self, config):
        if not isinstance(config, RuleConfig):
            raise PortfolioError("invalid_config")
        with self._connect(write=True) as db:
            self._read(db)
            body = canonical(config.to_dict())
            db.execute("UPDATE rules SET body=?,hash=? WHERE singleton=1", (body, digest(body)))
        return config
