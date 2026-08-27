"""SQLite store for the mock DMS. Deliberately small; it is a fixture."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).parent / "dms.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY, password TEXT NOT NULL, display_name TEXT
);
CREATE TABLE IF NOT EXISTS vehicles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_no TEXT UNIQUE NOT NULL, vin TEXT UNIQUE NOT NULL,
    year INTEGER, make TEXT, model TEXT, trim TEXT,
    mileage INTEGER, condition TEXT, price REAL,
    status TEXT DEFAULT 'draft', acquired_at REAL, created_at REAL
);
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, email TEXT, phone TEXT,
    customer_group TEXT DEFAULT 'Retail', created_at REAL
);
CREATE TABLE IF NOT EXISTS deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER, vehicle_id INTEGER,
    fi_products TEXT DEFAULT '', vehicle_price REAL DEFAULT 0,
    fi_total REAL DEFAULT 0, total REAL DEFAULT 0,
    status TEXT DEFAULT 'draft', created_at REAL,
    FOREIGN KEY (customer_id) REFERENCES customers(id),
    FOREIGN KEY (vehicle_id) REFERENCES vehicles(id)
);
CREATE TABLE IF NOT EXISTS service_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ro_number TEXT UNIQUE, vehicle_id INTEGER, customer_id INTEGER,
    complaint TEXT, technician TEXT, status TEXT DEFAULT 'open', created_at REAL
);
"""

SEED_VEHICLES = [
    ("STK1001", "1HGCV1F30LA000111", 2020, "Honda", "Accord", "EX-L", 41200, "Good", 21500, "listed", 95),
    ("STK1002", "5YJ3E1EA7KF000222", 2019, "Tesla", "Model 3", "Standard", 58300, "Good", 24900, "listed", 140),
    ("STK1003", "1FTFW1E50NF000333", 2022, "Ford", "F-150", "XLT", 18900, "Excellent", 42750, "listed", 32),
    ("STK1004", "JTDKARFU0J3000444", 2018, "Toyota", "Prius", "Two", 77400, "Fair", 15250, "listed", 210),
    ("STK1005", "WBA8E9C50GK000555", 2016, "BMW", "328i", "Sport", 92100, "Fair", 12900, "listed", 305),
    ("STK1006", "3VW2K7AJ9EM000666", 2014, "Volkswagen", "Jetta", "SE", 118000, "Poor", 6800, "listed", 412),
]

SEED_CUSTOMERS = [
    ("Marcus Webb", "m.webb@example.com", "555-0142", "Retail"),
    ("Priya Raman", "p.raman@example.com", "555-0198", "Retail"),
    ("Delgado Fleet Services", "ops@delgadofleet.example", "555-0231", "Fleet"),
]


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def reset(seed: bool = True) -> None:
    """Restore a known state. The eval harness calls this between scenarios."""
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = connect()
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO users (username, password, display_name) VALUES (?, ?, ?)",
        ("operator", "operator", "Sam Operator"),
    )
    if seed:
        now = time.time()
        for stock, vin, year, make, model, trim, miles, cond, price, status, age_days in SEED_VEHICLES:
            conn.execute(
                "INSERT INTO vehicles (stock_no, vin, year, make, model, trim, mileage, "
                "condition, price, status, acquired_at, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (stock, vin, year, make, model, trim, miles, cond, price, status,
                 now - age_days * 86400, now - age_days * 86400),
            )
        for name, email, phone, group in SEED_CUSTOMERS:
            conn.execute(
                "INSERT INTO customers (name, email, phone, customer_group, created_at) "
                "VALUES (?,?,?,?,?)",
                (name, email, phone, group, now),
            )
    conn.commit()
    conn.close()


def query(sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def execute(sql: str, args: tuple = ()) -> int:
    conn = connect()
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid or 0
    finally:
        conn.close()


def ensure() -> None:
    if not DB_PATH.exists():
        reset()
