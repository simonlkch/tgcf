import json
import os
import sqlite3
from typing import Any, Dict, List

import streamlit as st

from tgcf.config import read_config
from tgcf.web_ui.password import check_password
from tgcf.web_ui.utils import apply_page_chrome, hide_st, switch_theme

CONFIG = read_config()
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
LOG_FILE_PATH = os.path.join(BASE_DIR, "logs.txt")
DB_PATH = os.path.join(BASE_DIR, "logs_review.sqlite3")
MAX_LOG_ROWS = 1000


def _ensure_log_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(logs)").fetchall()}
    alter_statements = []
    for column in [
        "commit_hash TEXT",
        "instance_id TEXT",
        "region TEXT",
        "service TEXT",
        "service_version TEXT",
        "event TEXT",
        "exception TEXT",
    ]:
        name = column.split()[0]
        if name not in existing:
            alter_statements.append(f"ALTER TABLE logs ADD COLUMN {column}")

    for statement in alter_statements:
        conn.execute(statement)


def _enforce_row_cap(conn: sqlite3.Connection, cap: int = MAX_LOG_ROWS) -> None:
    conn.execute(
        """
        DELETE FROM logs
        WHERE id NOT IN (
            SELECT id FROM logs ORDER BY id DESC LIMIT ?
        )
        """,
        (cap,),
    )


def _connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            line_no INTEGER NOT NULL,
            timestamp TEXT,
            level TEXT,
            logger TEXT,
            message TEXT,
            raw TEXT NOT NULL,
            parse_kind TEXT NOT NULL,
            commit_hash TEXT,
            instance_id TEXT,
            region TEXT,
            service TEXT,
            service_version TEXT,
            event TEXT,
            exception TEXT
        )
        """
    )
    _ensure_log_columns(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_line_no ON logs(line_no)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_logger ON logs(logger)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_event ON logs(event)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_service ON logs(service)")
    _enforce_row_cap(conn)
    conn.commit()
    return conn


def _parse_line(line: str, line_no: int) -> Dict[str, Any]:
    text = line.rstrip("\n")
    if not text:
        return {
            "line_no": line_no,
            "timestamp": None,
            "level": None,
            "logger": None,
            "message": "",
            "raw": text,
            "parse_kind": "empty",
            "commit_hash": None,
            "instance_id": None,
            "region": None,
            "service": None,
            "service_version": None,
            "event": None,
            "exception": None,
        }

    if text.startswith("{") and text.endswith("}"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return {
                    "line_no": line_no,
                    "timestamp": obj.get("timestamp"),
                    "level": str(obj.get("level") or "").lower() or None,
                    "logger": obj.get("logger"),
                    "message": obj.get("message") or "",
                    "raw": text,
                    "parse_kind": "json",
                    "commit_hash": obj.get("commit_hash"),
                    "instance_id": obj.get("instance_id"),
                    "region": obj.get("region"),
                    "service": obj.get("service"),
                    "service_version": obj.get("service_version"),
                    "event": obj.get("event"),
                    "exception": obj.get("exception"),
                }
        except json.JSONDecodeError:
            pass

    return {
        "line_no": line_no,
        "timestamp": None,
        "level": None,
        "logger": None,
        "message": text,
        "raw": text,
        "parse_kind": "plain",
        "commit_hash": None,
        "instance_id": None,
        "region": None,
        "service": None,
        "service_version": None,
        "event": None,
        "exception": None,
    }


def _rebuild_index(log_file_path: str) -> Dict[str, int]:
    if not os.path.exists(log_file_path):
        return {"indexed": 0}

    conn = _connect_db()
    try:
        conn.execute("DELETE FROM logs")
        with open(log_file_path, "r", encoding="utf8", errors="replace") as f:
            rows = []
            for line_no, line in enumerate(f, start=1):
                rows.append(_parse_line(line, line_no))

        if len(rows) > MAX_LOG_ROWS:
            rows = rows[-MAX_LOG_ROWS:]

        conn.executemany(
            """
            INSERT INTO logs(
                line_no, timestamp, level, logger, message, raw, parse_kind,
                commit_hash, instance_id, region, service, service_version, event, exception
            )
            VALUES(
                :line_no, :timestamp, :level, :logger, :message, :raw, :parse_kind,
                :commit_hash, :instance_id, :region, :service, :service_version, :event, :exception
            )
            """,
            rows,
        )
        _enforce_row_cap(conn)
        conn.commit()
        return {"indexed": len(rows)}
    finally:
        conn.close()


def _query_logs(
    search_text: str,
    levels: List[str],
    logger_contains: str,
    event_contains: str,
    service_contains: str,
    order_by: str,
    descending: bool,
    limit: int,
) -> List[Dict[str, Any]]:
    conn = _connect_db()
    try:
        where_parts: List[str] = []
        params: List[Any] = []

        if search_text.strip():
            where_parts.append("(raw LIKE ? OR message LIKE ?)")
            q = f"%{search_text.strip()}%"
            params.extend([q, q])

        if logger_contains.strip():
            where_parts.append("logger LIKE ?")
            params.append(f"%{logger_contains.strip()}%")

        if event_contains.strip():
            where_parts.append("event LIKE ?")
            params.append(f"%{event_contains.strip()}%")

        if service_contains.strip():
            where_parts.append("service LIKE ?")
            params.append(f"%{service_contains.strip()}%")

        if levels:
            placeholders = ",".join(["?" for _ in levels])
            where_parts.append(f"level IN ({placeholders})")
            params.extend([item.lower() for item in levels])

        where_sql = ""
        if where_parts:
            where_sql = "WHERE " + " AND ".join(where_parts)

        order_field = "line_no"
        if order_by in {
            "line_no",
            "timestamp",
            "level",
            "logger",
            "event",
            "service",
            "instance_id",
        }:
            order_field = order_by

        order_dir = "DESC" if descending else "ASC"

        sql = f"""
            SELECT
                line_no,
                timestamp,
                level,
                logger,
                event,
                service,
                instance_id,
                region,
                commit_hash,
                service_version,
                message,
                parse_kind,
                exception,
                raw
            FROM logs
            {where_sql}
            ORDER BY {order_field} {order_dir}
            LIMIT ?
        """
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _summary_counts() -> Dict[str, int]:
    conn = _connect_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        warn = conn.execute("SELECT COUNT(*) FROM logs WHERE level='warning'").fetchone()[0]
        err = conn.execute("SELECT COUNT(*) FROM logs WHERE level='error'").fetchone()[0]
        info = conn.execute("SELECT COUNT(*) FROM logs WHERE level='info'").fetchone()[0]
        return {"total": total, "warning": warn, "error": err, "info": info}
    finally:
        conn.close()


st.set_page_config(
    page_title="Logs Review",
    page_icon="📜",
    layout="wide",
)
hide_st(st)
switch_theme(st, CONFIG)

if check_password(st):
    apply_page_chrome(
        st,
        CONFIG,
        "Logs Review",
        "SQLite-backed log viewer for fast filtering and search in current JSON/plain format.",
        chips=["SQLite", "Search", "Filter", "Sort"],
    )

    top_left, top_mid, top_right = st.columns([1, 1, 2])
    with top_left:
        if st.button("Rebuild Log Index", type="primary", use_container_width=True):
            stats = _rebuild_index(LOG_FILE_PATH)
            st.session_state["logs_indexed_rows"] = stats.get("indexed", 0)
    with top_mid:
        if st.button("Refresh View", use_container_width=True):
            st.rerun()
    with top_right:
        indexed_rows = st.session_state.get("logs_indexed_rows")
        if indexed_rows is not None:
            st.info(f"Indexed rows: {indexed_rows}")
        else:
            st.caption("Click Rebuild Log Index after new log writes.")

    if not os.path.exists(LOG_FILE_PATH):
        st.warning(f"Log file not found: {LOG_FILE_PATH}")
    else:
        counts = _summary_counts()
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Rows", counts["total"])
        c2.metric("Warnings", counts["warning"])
        c3.metric("Errors", counts["error"])
        c4.metric("Info", counts["info"])

        f1, f2, f3, f4, f5, f6 = st.columns([1.9, 1.0, 1.2, 1.2, 1.2, 1.0])
        with f1:
            search_text = st.text_input("Search text", placeholder="payload is too big")
        with f2:
            levels = st.multiselect("Level", ["error", "warning", "info", "debug"], default=[])
        with f3:
            logger_contains = st.text_input("Logger contains", placeholder="tgcf.fast_transfer")
        with f4:
            event_contains = st.text_input("Event contains", placeholder="forward_source_batch")
        with f5:
            service_contains = st.text_input("Service contains", placeholder="tgcf")
        with f6:
            order_by = st.selectbox("Order by", ["line_no", "timestamp", "level", "logger", "event", "service", "instance_id"], index=0)

        g1, g2, g3 = st.columns([1, 1, 1.5])
        with g1:
            descending = st.checkbox("Descending", value=True)
        with g2:
            limit = int(st.number_input("Limit", min_value=50, max_value=MAX_LOG_ROWS, value=MAX_LOG_ROWS, step=50))
        with g3:
            st.caption(f"SQLite keeps latest {MAX_LOG_ROWS} log rows automatically.")

        rows = _query_logs(
            search_text=search_text,
            levels=levels,
            logger_contains=logger_contains,
            event_contains=event_contains,
            service_contains=service_contains,
            order_by=order_by,
            descending=descending,
            limit=limit,
        )

        if rows:
            st.dataframe(rows, use_container_width=True, height=620)
        else:
            st.info("No logs match current filters.")
