#!/usr/bin/env python3
"""
Analytics & Admin module for 卡塔音乐.

Tracks:
  - Visitor counts (daily / monthly / yearly) with repeat-visitor detection
  - Download events (song, platform, channel, success/fail)
  - API health checks (periodic ping of all external music APIs)

Storage: SQLite (stdlib, zero extra dependencies).
"""

from __future__ import annotations

import sqlite3
import threading
import time
import hashlib
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import Optional

# ---------------------------------------------------------------------------
# Database location
# ---------------------------------------------------------------------------
DB_PATH = Path(__file__).parent / "data" / "analytics.db"

# Ensure data directory exists
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Thread-local connections (sqlite3 requires same-thread usage)
_local = threading.local()

# ---------------------------------------------------------------------------
# Schema — auto-created on first use
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS visitors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    visitor_id  TEXT    NOT NULL,   -- hash(ip + user_agent prefix) for dedup
    ip          TEXT,
    visit_time  TEXT    NOT NULL,   -- ISO-8601
    date_key    TEXT    NOT NULL,   -- YYYY-MM-DD
    month_key   TEXT    NOT NULL,   -- YYYY-MM
    year_key    TEXT    NOT NULL    -- YYYY
);

CREATE TABLE IF NOT EXISTS downloads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    song_id       TEXT,
    song_title    TEXT,
    song_artist   TEXT,
    platform      TEXT,
    channel       TEXT,             -- myhkw_cached / myhkw_by_id / netease_direct / ...
    success       INTEGER DEFAULT 0,
    download_time TEXT    NOT NULL,
    date_key      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS api_health (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    api_name      TEXT    NOT NULL,
    is_available  INTEGER DEFAULT 0,
    response_ms   INTEGER,
    check_time    TEXT    NOT NULL,
    date_key      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_visitors_date    ON visitors(date_key);
CREATE INDEX IF NOT EXISTS idx_visitors_month   ON visitors(month_key);
CREATE INDEX IF NOT EXISTS idx_visitors_year    ON visitors(year_key);
CREATE INDEX IF NOT EXISTS idx_visitors_vid     ON visitors(visitor_id);
CREATE INDEX IF NOT EXISTS idx_downloads_date   ON downloads(date_key);
CREATE INDEX IF NOT EXISTS idx_downloads_channel ON downloads(channel);
CREATE INDEX IF NOT EXISTS idx_api_health_name  ON api_health(api_name);
CREATE INDEX IF NOT EXISTS idx_api_health_date  ON api_health(date_key);
"""


def _get_conn() -> sqlite3.Connection:
    """Return a thread-local database connection (auto-created)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=2000")
        conn.executescript(SCHEMA)
        conn.commit()
        _local.conn = conn
    return conn


def _today() -> str:
    return date.today().isoformat()


def _month_key() -> str:
    return date.today().strftime("%Y-%m")


def _year_key() -> str:
    return date.today().strftime("%Y")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Visitor tracking
# ---------------------------------------------------------------------------

def _make_visitor_id(ip: str, ua: str) -> str:
    """Create a stable anonymous visitor fingerprint."""
    # Use first 60 chars of UA (browser + OS fingerprint, not full string)
    raw = f"{ip}|{ua[:60]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def track_visit(ip: str, user_agent: str = "") -> dict:
    """Record a page visit. Returns basic stats for this visitor.

    Called from a Flask ``@app.before_request`` handler.
    """
    conn = _get_conn()
    vid = _make_visitor_id(ip, user_agent)
    now = _now_iso()
    dk = _today()
    mk = _month_key()
    yk = _year_key()

    # Check whether this visitor_id has EVER visited before today
    prev = conn.execute(
        "SELECT COUNT(*) AS cnt FROM visitors WHERE visitor_id = ? AND date_key < ?",
        (vid, dk),
    ).fetchone()
    is_repeat = prev["cnt"] > 0

    conn.execute(
        "INSERT INTO visitors (visitor_id, ip, visit_time, date_key, month_key, year_key) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (vid, ip, now, dk, mk, yk),
    )
    conn.commit()

    # Return lightweight stats
    today_visitors = conn.execute(
        "SELECT COUNT(DISTINCT visitor_id) FROM visitors WHERE date_key = ?", (dk,)
    ).fetchone()[0]
    today_total = conn.execute(
        "SELECT COUNT(*) FROM visitors WHERE date_key = ?", (dk,)
    ).fetchone()[0]

    return {
        "is_repeat": is_repeat,
        "today_visitors": today_visitors,
        "today_total": today_total,
    }


# ---------------------------------------------------------------------------
# Download tracking
# ---------------------------------------------------------------------------

def track_download(
    song_id: str,
    title: str,
    artist: str,
    platform: str,
    channel: str = "",
    success: bool = True,
):
    """Record a download attempt (called from the download route)."""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO downloads (song_id, song_title, song_artist, platform, channel, "
        "success, download_time, date_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (song_id, title, artist, platform, channel, int(success), _now_iso(), _today()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# API health tracking
# ---------------------------------------------------------------------------

def record_api_check(api_name: str, is_available: bool, response_ms: int = 0):
    """Store an API health-check result."""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO api_health (api_name, is_available, response_ms, check_time, date_key) "
        "VALUES (?, ?, ?, ?, ?)",
        (api_name, int(is_available), response_ms, _now_iso(), _today()),
    )
    # Keep only the 50 most recent checks per API to avoid unbounded growth
    conn.execute(
        "DELETE FROM api_health WHERE id NOT IN ("
        "  SELECT id FROM api_health WHERE api_name = ? "
        "  ORDER BY id DESC LIMIT 50"
        ")", (api_name,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Query: aggregate statistics
# ---------------------------------------------------------------------------

def get_visitor_stats() -> dict:
    """Return today / this-month / this-year unique-visitor and total-visit counts,
    plus repeat-visitor counts for each period."""
    conn = _get_conn()
    dk = _today()
    mk = _month_key()
    yk = _year_key()

    def _row(sql, *params):
        r = conn.execute(sql, params).fetchone()
        return dict(r) if r else {}

    today = _row(
        "SELECT COUNT(DISTINCT visitor_id) AS unique_visitors, "
        "COUNT(*) AS total_visits FROM visitors WHERE date_key = ?", dk)
    month = _row(
        "SELECT COUNT(DISTINCT visitor_id) AS unique_visitors, "
        "COUNT(*) AS total_visits FROM visitors WHERE month_key = ?", mk)
    year = _row(
        "SELECT COUNT(DISTINCT visitor_id) AS unique_visitors, "
        "COUNT(*) AS total_visits FROM visitors WHERE year_key = ?", yk)

    # Repeat visitors (visitors who came back after their first-ever visit)
    # "Repeat this month" = visited this month AND first visit was before this month
    repeat_month = conn.execute(
        "SELECT COUNT(DISTINCT v.visitor_id) FROM visitors v "
        "WHERE v.month_key = ? AND EXISTS ("
        "  SELECT 1 FROM visitors v2 WHERE v2.visitor_id = v.visitor_id "
        "  AND v2.date_key < v.date_key"
        ")", (mk,)
    ).fetchone()[0]

    repeat_year = conn.execute(
        "SELECT COUNT(DISTINCT v.visitor_id) FROM visitors v "
        "WHERE v.year_key = ? AND EXISTS ("
        "  SELECT 1 FROM visitors v2 WHERE v2.visitor_id = v.visitor_id "
        "  AND v2.date_key < v.date_key"
        ")", (yk,)
    ).fetchone()[0]

    # All-time totals
    all_time = _row(
        "SELECT COUNT(DISTINCT visitor_id) AS unique_visitors, "
        "COUNT(*) AS total_visits FROM visitors")

    return {
        "today": today.get("unique_visitors", 0),
        "today_total": today.get("total_visits", 0),
        "month": month.get("unique_visitors", 0),
        "month_total": month.get("total_visits", 0),
        "year": year.get("unique_visitors", 0),
        "year_total": year.get("total_visits", 0),
        "repeat_month": repeat_month,
        "repeat_year": repeat_year,
        "all_time_unique": all_time.get("unique_visitors", 0),
        "all_time_total": all_time.get("total_visits", 0),
    }


def get_visitor_trend(days: int = 30) -> list[dict]:
    """Return daily visitor counts for the last N days (for chart)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT date_key, COUNT(DISTINCT visitor_id) AS unique_visitors, "
        "COUNT(*) AS total_visits "
        "FROM visitors WHERE date_key >= ? "
        "GROUP BY date_key ORDER BY date_key ASC",
        ((date.today() - timedelta(days=days - 1)).isoformat(),),
    ).fetchall()

    # Fill in missing days with zeros
    result = []
    seen = {r["date_key"]: r for r in rows}
    for i in range(days):
        dk = (date.today() - timedelta(days=days - 1 - i)).isoformat()
        r = seen.get(dk)
        result.append({
            "date": dk,
            "unique_visitors": r["unique_visitors"] if r else 0,
            "total_visits": r["total_visits"] if r else 0,
        })
    return result


def get_download_stats() -> dict:
    """Return total downloads and success rate."""
    conn = _get_conn()
    total = conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
    success = conn.execute("SELECT COUNT(*) FROM downloads WHERE success = 1").fetchone()[0]
    today = conn.execute(
        "SELECT COUNT(*) FROM downloads WHERE date_key = ?", (_today(),)
    ).fetchone()[0]
    return {
        "total": total,
        "success": success,
        "failed": total - success,
        "today": today,
    }


def get_top_songs(limit: int = 5) -> list[dict]:
    """Return most-downloaded songs (successful downloads only)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT song_title, song_artist, platform, COUNT(*) AS cnt "
        "FROM downloads WHERE success = 1 AND song_title != '' "
        "GROUP BY song_title, song_artist "
        "ORDER BY cnt DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_top_channels(limit: int = 10) -> list[dict]:
    """Return download channel usage ranking."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT channel, COUNT(*) AS cnt, "
        "SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS success_cnt "
        "FROM downloads WHERE channel != '' "
        "GROUP BY channel ORDER BY cnt DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_recent_downloads(limit: int = 20) -> list[dict]:
    """Return most recent download attempts."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT song_title, song_artist, platform, channel, success, download_time "
        "FROM downloads ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_latest_api_status() -> list[dict]:
    """Return the most recent health-check result for each API."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT api_name, is_available, response_ms, check_time FROM api_health "
        "WHERE id IN ("
        "  SELECT MAX(id) FROM api_health GROUP BY api_name"
        ") ORDER BY api_name"
    ).fetchall()
    return [dict(r) for r in rows]


def get_visitor_hourly_distribution(days: int = 30) -> list[dict]:
    """Return visitor counts grouped by hour of day (0-23) for the last N days.

    Used by the admin dashboard to render the peak-hour wave chart.
    """
    conn = _get_conn()
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    rows = conn.execute(
        "SELECT CAST(strftime('%H', visit_time) AS INTEGER) AS hour, "
        "COUNT(*) AS visits "
        "FROM visitors WHERE date_key >= ? "
        "GROUP BY hour ORDER BY hour",
        (since,),
    ).fetchall()

    # Fill all 24 hours — missing hours get 0
    seen = {r["hour"]: r["visits"] for r in rows}
    result = []
    for h in range(24):
        result.append({
            "hour": h,
            "label": f"{h:02d}:00",
            "visits": seen.get(h, 0),
        })
    return result


def get_all_stats() -> dict:
    """Return a complete stats payload for the admin dashboard."""
    return {
        "visitors": get_visitor_stats(),
        "trend": get_visitor_trend(30),
        "hourly": get_visitor_hourly_distribution(30),
        "downloads": get_download_stats(),
        "top_songs": get_top_songs(5),
        "top_channels": get_top_channels(10),
        "recent_downloads": get_recent_downloads(10),
        "generated_at": _now_iso(),
    }
