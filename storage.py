import sqlite3
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def connect():
    db = sqlite3.connect(
        DATA / "acquiring.sqlite3", timeout=10, isolation_level=None
    )
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA synchronous = FULL")
    db.execute("ATTACH DATABASE ? AS shop", (str(DATA / "orders.sqlite3"),))
    db.execute("PRAGMA shop.synchronous = FULL")
    return db


@contextmanager
def write(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise