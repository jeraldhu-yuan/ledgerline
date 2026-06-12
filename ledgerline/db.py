"""SQLite connection management and migrations. No ORM, plain SQL."""

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path("data") / "ledgerline.db"
# Inside the package so installed wheels carry the schema, not just repo checkouts
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def db_path() -> Path:
    return Path(os.environ.get("LEDGERLINE_DB", DEFAULT_DB_PATH))


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open a read-write connection, creating and migrating the DB if needed.
    Fresh database files are created owner-only (0600)."""
    path = Path(path) if path else db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    if fresh:
        os.chmod(path, 0o600)
    return conn


def connect_readonly(path: Path | str | None = None) -> sqlite3.Connection:
    """Open a read-only connection (mode=ro URI) for the LLM SQL tool.
    Result-size and statement-size limits bound memory even for hostile SQL."""
    path = Path(path) if path else db_path()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 10_000_000)
    conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 100_000)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (filename TEXT PRIMARY KEY)"
    )
    applied = {r[0] for r in conn.execute("SELECT filename FROM schema_migrations")}
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if sql_file.name in applied:
            continue
        conn.executescript(sql_file.read_text())
        conn.execute(
            "INSERT INTO schema_migrations (filename) VALUES (?)", (sql_file.name,)
        )
    conn.commit()


def schema_ddl(conn: sqlite3.Connection) -> str:
    """The full schema DDL, used as context for the `ask` command."""
    rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
        " AND name NOT LIKE 'sqlite_%' AND name != 'schema_migrations'"
        " ORDER BY type DESC, name"
    ).fetchall()
    return ";\n\n".join(r[0] for r in rows)
