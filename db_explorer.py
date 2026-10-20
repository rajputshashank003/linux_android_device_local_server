"""Read-only, authenticated-facing inspection helpers for MyForever's local SQLite DB."""
import re
import sqlite3
import time
from pathlib import Path

import app_db

MAX_PAGE_SIZE = 100
_SECRET_NAME = re.compile(r"(?:password|passwd|token|secret|api.?key|authorization|cookie|credential|command_json|environment_keys_json|log_file|runtime_alias|process_pid|process_start_ticks)", re.I)
_SECRET_VALUE = re.compile(r"(?i)(\b(?:sk-[A-Za-z0-9_-]{12,}|Bearer\s+)[A-Za-z0-9._-]+|(?:api[_-]?key|access[_-]?token|password)\s*[:=]\s*\S+)")


def _connect():
    path = app_db.DB_PATH.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError("The application database does not exist yet.")
    # SQLite URI mode=ro plus query_only blocks accidental writes from this feature.
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("PRAGMA busy_timeout=3000")
    return db


def _tables(db):
    return [row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]


def _quoted_identifier(value):
    # Call only after verifying table/column names against SQLite metadata.
    return '"' + value.replace('"', '""') + '"'


def overview():
    path = app_db.DB_PATH.expanduser()
    with _connect() as db:
        db.execute("SELECT 1").fetchone()
        result = []
        for name in _tables(db):
            count = db.execute(f"SELECT COUNT(*) FROM {_quoted_identifier(name)}").fetchone()[0]
            result.append({"name": name, "recordCount": count})
        version = db.execute("PRAGMA user_version").fetchone()[0]
    return {"type": "SQLite", "state": "healthy", "identifier": "~/.local/share/myforever/platform.sqlite3" if path.name == "platform.sqlite3" else "Configured application database",
            "sizeBytes": path.stat().st_size, "schemaVersion": version, "readOnly": True, "checkedAt": time.time(), "tables": result}


def table(name, *, page=1, limit=25, search="", sort="", direction="asc"):
    if not isinstance(name, str) or len(name) > 128:
        raise ValueError("Choose a valid table name.")
    try:
        page = max(1, min(int(page), 1_000_000))
        limit = max(1, min(int(limit), MAX_PAGE_SIZE))
    except (TypeError, ValueError):
        raise ValueError("Page and limit must be integers.")
    search = str(search or "")[:200]
    direction = "DESC" if str(direction).lower() == "desc" else "ASC"
    with _connect() as db:
        available = _tables(db)
        if name not in available:
            raise ValueError("Table not found.")
        quoted = _quoted_identifier(name)
        columns = [dict(row) for row in db.execute(f"PRAGMA table_info({quoted})")]
        foreign_keys = [dict(row) for row in db.execute(f"PRAGMA foreign_key_list({quoted})")]
        visible_columns = [col["name"] for col in columns if not _SECRET_NAME.search(col["name"])]
        where = ""
        params = []
        if search.strip() and visible_columns:
            where = " WHERE " + " OR ".join(f"CAST({_quoted_identifier(col)} AS TEXT) LIKE ? ESCAPE '\\'" for col in visible_columns)
            needle = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params = [f"%{needle}%"] * len(visible_columns)
        total = db.execute(f"SELECT COUNT(*) FROM {quoted}{where}", params).fetchone()[0]
        selected_page = min(page, max(1, (total + limit - 1) // limit))
        sort_column = sort if sort in visible_columns else next((c["name"] for c in columns), "")
        order = f" ORDER BY {_quoted_identifier(sort_column)} {direction}" if sort_column else ""
        offset = (selected_page - 1) * limit
        raw_rows = db.execute(f"SELECT * FROM {quoted}{where}{order} LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
        rows = []
        for raw in raw_rows:
            item = {}
            for key in raw.keys():
                if _SECRET_NAME.search(key):
                    item[key] = "[REDACTED]"
                    continue
                value = raw[key]
                if isinstance(value, str):
                    value = _SECRET_VALUE.sub("[REDACTED]", value)
                    if len(value) > 6000: value = value[:6000] + "… [truncated]"
                if isinstance(value, bytes): value = f"[binary data: {len(value)} bytes]"
                item[key] = value
            rows.append(item)
    safe_columns = [{"name": c["name"], "type": c["type"], "primaryKey": bool(c["pk"]),
                     "notNull": bool(c["notnull"]),
                     "default": "[REDACTED]" if _SECRET_NAME.search(c["name"]) and c["dflt_value"] is not None else c["dflt_value"],
                     "redacted": bool(_SECRET_NAME.search(c["name"]))} for c in columns]
    return {"table": name, "columns": safe_columns, "relationships": foreign_keys,
            "records": rows, "total": total, "page": selected_page, "limit": limit,
            "pages": max(1, (total + limit - 1) // limit), "sort": sort_column, "direction": direction,
            "search": search}
