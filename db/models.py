"""
SQLite schema initialisation and shared connection helper.
All modules import get_conn() from here to share one database file.
"""

import sqlite3
import os
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "pipeline.db"


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create all tables if they don't already exist."""
    conn = get_conn()
    cur = conn.cursor()

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS properties (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT    NOT NULL,          -- 'gumtree' | 'rightmove'
            title       TEXT,
            price       INTEGER,
            location    TEXT,
            description TEXT,
            phone       TEXT,
            url         TEXT    UNIQUE NOT NULL,
            posted_date TEXT,
            scraped_at  TEXT    NOT NULL,
            url_hash    TEXT    UNIQUE NOT NULL,   -- sha256 of url for fast dedup
            notified    INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_properties_hash ON properties(url_hash);
        CREATE INDEX IF NOT EXISTS idx_properties_source ON properties(source);

        CREATE TABLE IF NOT EXISTS deals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            property_id     INTEGER REFERENCES properties(id),
            address         TEXT,
            purchase_price  INTEGER,
            market_value    INTEGER,
            bmv_percent     REAL,
            gross_yield     REAL,
            net_yield       REAL,
            monthly_rent    INTEGER,
            status          TEXT NOT NULL DEFAULT 'found',
            -- found | analysed | sent | under_offer | completed | dead
            pass_fail       TEXT,
            notes           TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS investors (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT    NOT NULL,
            email           TEXT    UNIQUE NOT NULL,
            phone           TEXT,
            areas           TEXT,   -- JSON list of preferred areas
            strategy        TEXT,   -- BTL | HMO | SA | FLIP
            max_budget      INTEGER,
            active          INTEGER NOT NULL DEFAULT 1,
            notes           TEXT,
            created_at      TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS deal_investors (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            deal_id         INTEGER NOT NULL REFERENCES deals(id),
            investor_id     INTEGER NOT NULL REFERENCES investors(id),
            sent_at         TEXT,
            response        TEXT,   -- interested | not_interested | no_response
            responded_at    TEXT,
            UNIQUE(deal_id, investor_id)
        );

        CREATE TABLE IF NOT EXISTS interactions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            investor_id     INTEGER NOT NULL REFERENCES investors(id),
            deal_id         INTEGER,
            type            TEXT,   -- email_sent | call | note | follow_up
            notes           TEXT,
            created_at      TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS fees (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            deal_id         INTEGER NOT NULL REFERENCES deals(id),
            investor_id     INTEGER NOT NULL REFERENCES investors(id),
            amount          INTEGER NOT NULL,
            received_at     TEXT    NOT NULL,
            notes           TEXT
        );
    """)

    conn.commit()
    conn.close()
    print(f"[DB] Database ready at {DB_PATH}")
