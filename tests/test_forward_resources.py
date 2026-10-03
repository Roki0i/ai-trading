"""研究の意味を変えず、正常・例外経路の接続終了とcommit/rollbackを確認する。"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ai_trading.forward import Ledger


class ForwardResourceTests(unittest.TestCase):
    def test_commit_closes_connection_and_retained_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger.__new__(Ledger)
            ledger.db_path = Path(tmp)/"ledger.sqlite3"
            with ledger.connect() as db:
                db.execute("CREATE TABLE example (value INTEGER)")
                db.execute("INSERT INTO example VALUES (1)")
                cursor = db.execute("SELECT * FROM example")
                self.assertEqual(cursor.fetchone(), (1,))
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute("SELECT 1")
            with self.assertRaises(sqlite3.ProgrammingError):
                cursor.fetchone()
            with ledger.connect() as reopened:
                self.assertEqual(reopened.execute("SELECT value FROM example").fetchall(), [(1,)])
            # db/cursorを参照したままでも、Windowsで削除できる必要がある。
            ledger.db_path.unlink()

    def test_rollback_and_close_on_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger.__new__(Ledger)
            ledger.db_path = Path(tmp)/"ledger.sqlite3"
            with ledger.connect() as db:
                db.execute("CREATE TABLE example (value INTEGER)")
            with self.assertRaisesRegex(RuntimeError, "injected"):
                with ledger.connect() as failed:
                    failed.execute("INSERT INTO example VALUES (2)")
                    raise RuntimeError("injected")
            with self.assertRaises(sqlite3.ProgrammingError):
                failed.execute("SELECT 1")
            with ledger.connect() as db:
                self.assertEqual(db.execute("SELECT * FROM example").fetchall(), [])
            ledger.db_path.unlink()


if __name__ == "__main__":
    unittest.main()
