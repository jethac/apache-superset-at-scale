"""The fact store is opened by more than one process and by more than one connection.

`serve` handles webhooks while `poll` runs intake and session sync on a timer, and modules that
own their own tables — `cicost`, `debt` — open their own connections to the same file.
`FactStore`'s lock serialises exactly one of those, so the property that keeps the dashboard
readable while the poller writes is WAL, not the lock. These tests fail if a connection is ever
opened on the default rollback journal again, where a writer holds an exclusive lock on the whole
database and readers block behind it until they time out.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from scoreboard import cicost
from scoreboard.store import FactStore


def _journal_mode(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        connection.close()


def test_the_store_opens_its_database_in_wal_mode(tmp_path: Path) -> None:
    path = tmp_path / "facts.db"
    FactStore(path)
    assert _journal_mode(path) == "wal"


def test_a_module_owned_connection_agrees_with_the_store(tmp_path: Path) -> None:
    """The module boundary is about which table, not about how the file is opened."""
    store = FactStore(tmp_path / "facts.db")
    cicost.ensure_schema(store)
    with cicost._connect(store) as connection:  # noqa: SLF001 - the connection policy is the point
        assert str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
        assert int(connection.execute("PRAGMA busy_timeout").fetchone()[0]) > 0


def test_the_store_sets_a_busy_timeout(tmp_path: Path) -> None:
    """Two writers still serialise; the timeout is what stops that becoming an error."""
    store = FactStore(tmp_path / "facts.db")
    row = store.query("PRAGMA busy_timeout")[0]
    assert int(row[0]) >= 30000


def test_a_reader_is_not_blocked_by_an_open_write_transaction(tmp_path: Path) -> None:
    """The poller's pass must not stall the page that reads on every load.

    This is the deployed shape: the `serve` container holds a long-lived store and reads through
    it on each dashboard load, while `poll` writes from another process every five minutes. Under
    a rollback journal the uncommitted write below holds an exclusive lock on the whole database
    and the read raises `database is locked` once the timeout expires. Under WAL it reads the last
    committed snapshot and returns immediately.
    """
    path = tmp_path / "facts.db"
    store = FactStore(path)
    store.record_snapshot(datetime(2026, 6, 1, tzinfo=UTC), "lint_violations", 1470)

    writer = sqlite3.connect(path, timeout=1.0)
    try:
        writer.execute("PRAGMA busy_timeout=1000")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO snapshot_daily (day, counter_name, value) VALUES (?, ?, ?)",
            ("2026-06-02", "lint_violations", 1400),
        )

        rows = store.query("SELECT counter_name, value FROM snapshot_daily ORDER BY day")

        assert [row["counter_name"] for row in rows] == ["lint_violations"]
        assert rows[0]["value"] == 1470, "the reader sees the last commit, not the open write"
    finally:
        writer.rollback()
        writer.close()


def test_opening_a_store_waits_for_a_writer_rather_than_failing(tmp_path: Path) -> None:
    """Constructing a `FactStore` writes: it applies the schema and runs migrations.

    So a container starting while the poller holds a write transaction is two writers, which WAL
    does not make concurrent — SQLite still serialises them. What stops that being a crash loop is
    the busy timeout, and this pins that the constructor inherits it rather than the 5-second
    default. The write below is committed promptly so the constructor proceeds.
    """
    path = tmp_path / "facts.db"
    FactStore(path)

    writer = sqlite3.connect(path, timeout=1.0)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO snapshot_daily (day, counter_name, value) VALUES (?, ?, ?)",
            ("2026-06-03", "lint_violations", 1390),
        )
        writer.commit()
    finally:
        writer.close()

    reopened = FactStore(path)
    assert int(reopened.query("PRAGMA busy_timeout")[0][0]) >= 30000
