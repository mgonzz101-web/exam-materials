"""Tiny database layer that runs on SQLite (local/testing) or Postgres (production).

Set DATABASE_URL=postgresql://... to use Postgres. Otherwise a SQLite file is used.
All SQL in app.py is written to work on both. Connections run in autocommit mode and
transactions are explicit (begin/commit/rollback).
"""
import os
import sqlite3
from datetime import datetime, timezone

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
SQLITE_PATH = os.environ.get(
    "SQLITE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "inventory.db"),
)

if IS_PG:
    import psycopg2
    import psycopg2.extras

    IntegrityError = psycopg2.IntegrityError
else:
    IntegrityError = sqlite3.IntegrityError

# Row-lock hint used when we must read a row and then write it in the same transaction.
FOR_UPDATE = " FOR UPDATE" if IS_PG else ""


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Result:
    def __init__(self, cur):
        self.rowcount = cur.rowcount
        self.rows = [dict(r) for r in cur.fetchall()] if cur.description is not None else []

    def one(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class DB:
    def __init__(self):
        self.in_tx = False
        if IS_PG:
            url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
            self.conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
            self.conn.autocommit = True
        else:
            os.makedirs(os.path.dirname(SQLITE_PATH), exist_ok=True)
            self.conn = sqlite3.connect(SQLITE_PATH, isolation_level=None, timeout=15)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=15000")
            self.conn.execute("PRAGMA foreign_keys=ON")

    def execute(self, sql, params=()):
        if IS_PG:
            cur = self.conn.cursor()
            cur.execute(sql.replace("?", "%s"), params)
        else:
            cur = self.conn.execute(sql, params)
        return Result(cur)

    def begin(self):
        # SQLite: IMMEDIATE takes the write lock up front so concurrent checkouts queue up.
        # Postgres: row locks are taken by the conditional UPDATEs themselves.
        self.execute("BEGIN" if IS_PG else "BEGIN IMMEDIATE")
        self.in_tx = True

    def commit(self):
        if self.in_tx:
            self.execute("COMMIT")
            self.in_tx = False

    def rollback(self):
        if self.in_tx:
            try:
                self.execute("ROLLBACK")
            except Exception:
                pass
            self.in_tx = False

    def close(self):
        self.rollback()
        try:
            self.conn.close()
        except Exception:
            pass


def init_schema(d):
    pk = "BIGSERIAL PRIMARY KEY" if IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    if IS_PG:
        d.execute("SELECT pg_advisory_lock(8841001)")  # several workers may start at once
    try:
        stmts = [
            f"""CREATE TABLE IF NOT EXISTS users (
                id {pk},
                email TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL)""",
            f"""CREATE TABLE IF NOT EXISTS items (
                id {pk},
                exam TEXT NOT NULL,
                edition TEXT NOT NULL DEFAULT '',
                material_type TEXT NOT NULL,
                form_name TEXT NOT NULL DEFAULT '',
                quantity INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
                low_threshold INTEGER NOT NULL DEFAULT 5 CHECK (low_threshold >= 0),
                location TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                archived INTEGER NOT NULL DEFAULT 0,
                alert_active INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL)""",
            f"""CREATE TABLE IF NOT EXISTS checkouts (
                id {pk},
                token TEXT NOT NULL UNIQUE,
                user_id BIGINT,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'completed',
                edited INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                reversed_at TEXT,
                reversed_by TEXT,
                reverse_reason TEXT)""",
            f"""CREATE TABLE IF NOT EXISTS checkout_lines (
                id {pk},
                checkout_id BIGINT NOT NULL REFERENCES checkouts(id),
                item_id BIGINT NOT NULL REFERENCES items(id),
                exam TEXT NOT NULL,
                edition TEXT NOT NULL,
                material_type TEXT NOT NULL,
                form_name TEXT NOT NULL,
                qty INTEGER NOT NULL CHECK (qty >= 0),
                original_qty INTEGER NOT NULL,
                returned_qty INTEGER NOT NULL DEFAULT 0)""",
            f"""CREATE TABLE IF NOT EXISTS returns (
                id {pk},
                token TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL,
                created_at TEXT NOT NULL)""",
            f"""CREATE TABLE IF NOT EXISTS movements (
                id {pk},
                item_id BIGINT NOT NULL REFERENCES items(id),
                delta INTEGER NOT NULL,
                prev_qty INTEGER NOT NULL,
                new_qty INTEGER NOT NULL,
                kind TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                checkout_id BIGINT,
                actor_name TEXT NOT NULL DEFAULT '',
                actor_user_id BIGINT,
                created_at TEXT NOT NULL)""",
            f"""CREATE TABLE IF NOT EXISTS alerts (
                id {pk},
                item_id BIGINT NOT NULL,
                label TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                threshold INTEGER NOT NULL,
                trigger TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                recipients TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS idx_lines_checkout ON checkout_lines(checkout_id)",
            "CREATE INDEX IF NOT EXISTS idx_lines_item ON checkout_lines(item_id)",
            "CREATE INDEX IF NOT EXISTS idx_mov_item ON movements(item_id)",
            "CREATE INDEX IF NOT EXISTS idx_checkouts_created ON checkouts(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_alerts_item ON alerts(item_id)",
        ]
        for s in stmts:
            d.execute(s)
        # Upgrade databases created before test-kit returns existed.
        if IS_PG:
            d.execute("ALTER TABLE checkout_lines ADD COLUMN IF NOT EXISTS returned_qty INTEGER NOT NULL DEFAULT 0")
        else:
            cols = [r["name"] for r in d.execute("PRAGMA table_info(checkout_lines)").all()]
            if "returned_qty" not in cols:
                d.execute("ALTER TABLE checkout_lines ADD COLUMN returned_qty INTEGER NOT NULL DEFAULT 0")
    finally:
        if IS_PG:
            d.execute("SELECT pg_advisory_unlock(8841001)")
