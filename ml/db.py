"""Read-only access to the behaviour-profile store for the ML pipeline.

The behavioural model has READ-ONLY DB permissions (per Adhere). This module opens a
read-only connection and streams the cache in chunks so a 1M+ row pull never blows up memory.
"""
from __future__ import annotations

import logging

import pandas as pd
import psycopg
from psycopg.rows import dict_row

from . import config

log = logging.getLogger("ml.db")


def connect():
    return psycopg.connect(config.pg_dsn(), autocommit=True, row_factory=dict_row)


def read_sql(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Run a read-only query and return a DataFrame."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return pd.DataFrame(rows)


def scalar(sql: str, params: tuple = ()):
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return next(iter(row.values())) if row else None
