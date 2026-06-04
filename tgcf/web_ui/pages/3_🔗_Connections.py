import asyncio
import csv
import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit as st
import streamlit.components.v1 as components
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.patched import MessageService

from tgcf.config import CONFIG, Forward, PastSettings, read_config, write_config
from tgcf.web_ui.password import check_password
from tgcf.web_ui.utils import apply_page_chrome, get_list, get_string, hide_st, switch_theme

CONFIG = read_config()


def _run(coro):
    """Run an async coroutine for Streamlit callbacks.

    Streamlit scripts are usually executed without an active event loop, so
    running coroutines directly avoids threadpool overhead and context warnings.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _preview_text(text: str, limit: int = 120) -> str:
    text = (text or "").replace("\n", " ").strip()
    if not text:
        return "(no text/caption)"
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _human_size(size_bytes: Optional[int]) -> str:
    if not size_bytes:
        return "-"
    value = float(size_bytes)
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    if idx == 0:
        return f"{int(value)} {units[idx]}"
    return f"{value:.2f} {units[idx]}"


def _message_media_info(message) -> Dict[str, Any]:
    file_obj = getattr(message, "file", None)
    raw_size = getattr(file_obj, "size", None)
    return {
        "has_media": bool(getattr(message, "media", None)),
        "file_name": getattr(file_obj, "name", "") or "-",
        "mime_type": getattr(file_obj, "mime_type", "") or "-",
        "size_bytes": raw_size or 0,
        "size_human": _human_size(raw_size),
        "duration_seconds": getattr(file_obj, "duration", None) or "-",
        "dimensions": (
            f"{getattr(file_obj, 'width', '?')}x{getattr(file_obj, 'height', '?')}"
            if getattr(file_obj, "width", None) and getattr(file_obj, "height", None)
            else "-"
        ),
    }


def _parse_peer(raw: Any):
    if isinstance(raw, int):
        return raw
    token = str(raw or "").strip()
    if token == "":
        return ""
    if token.lstrip("-").isdigit():
        try:
            return int(token)
        except ValueError:
            return token
    return token


def _build_session_from_login(login):
    if login.user_type == 1:
        if login.sessions and 0 <= login.active_session < len(login.sessions):
            sess = login.sessions[login.active_session].session_string
            if sess:
                return StringSession(sess)
        if login.SESSION_STRING:
            return StringSession(login.SESSION_STRING)
        return None
    return "tgcf_web"


def _peer_cache() -> Dict[str, Dict[str, Any]]:
    if "peer_resolve_cache" not in st.session_state:
        st.session_state.peer_resolve_cache = {}
    return st.session_state.peer_resolve_cache


def _cache_key(peer_raw: Any) -> str:
    return str(_parse_peer(peer_raw))


def _render_copyable_table(
        rows: List[Dict[str, Any]],
        columns: List[Dict[str, str]],
        key: str,
        *,
        min_height: int = 140,
) -> None:
        """Render a lightweight HTML table where clicking a cell copies its value."""

        if not rows:
                return

        dark = CONFIG.theme == "dark"
        table_bg = "#0f172a" if dark else "#ffffff"
        header_bg = "#111827" if dark else "#f1f5f9"
        odd_row_bg = "#0f172a" if dark else "#ffffff"
        even_row_bg = "#1e293b" if dark else "#f8fafc"
        border_color = "#334155" if dark else "#d9d9d9"
        text_color = "#e2e8f0" if dark else "#111827"
        header_text_color = "#f8fafc" if dark else "#0f172a"
        status_color = "#94a3b8" if dark else "#666"

        table_id = f"copy_table_{key}".replace(" ", "_")
        header_html = "".join(
                f"<th>{html.escape(col['label'])}</th>" for col in columns
        )
        body_rows = []
        for row in rows:
                cells = []
                for col in columns:
                        value = row.get(col["field"], "")
                        text = "" if value is None else str(value)
                        payload = json.dumps(text)
                        cells.append(
                                f"<td onclick='copyCell_{table_id}({payload})' title='Click to copy'>{html.escape(text)}</td>"
                        )
                body_rows.append(f"<tr>{''.join(cells)}</tr>")

        height = max(min_height, min(520, 68 + len(rows) * 34))
        html_doc = f"""
        <style>
            #{table_id} {{
                width: 100%;
                border-collapse: collapse;
                font-family: sans-serif;
                font-size: 13px;
                color: {text_color};
                background: {table_bg};
            }}
            #{table_id} th, #{table_id} td {{
                border: 1px solid {border_color};
                padding: 6px 8px;
                text-align: left;
            }}
            #{table_id} thead th {{
                background: {header_bg};
                color: {header_text_color};
                font-weight: 700;
            }}
            #{table_id} tbody tr:nth-child(odd) {{ background: {odd_row_bg}; }}
            #{table_id} tbody tr:nth-child(even) {{ background: {even_row_bg}; }}
            #{table_id} tbody td {{ cursor: copy; color: {text_color}; }}
            #{table_id}_status {{ margin-top: 6px; font-size: 12px; color: {status_color}; }}
        </style>
        <table id="{table_id}">
            <thead><tr>{header_html}</tr></thead>
            <tbody>{''.join(body_rows)}</tbody>
        </table>
        <div id="{table_id}_status">Tip: click any cell to copy.</div>
        <script>
            function copyCell_{table_id}(text) {{
                navigator.clipboard.writeText(text).then(function() {{
                    document.getElementById('{table_id}_status').innerText = 'Copied: ' + text;
                }}).catch(function() {{
                    document.getElementById('{table_id}_status').innerText = 'Copy failed';
                }});
            }}
        </script>
        """
        components.html(html_doc, height=height, scrolling=True)


async def _connect_client() -> TelegramClient:
    login = CONFIG.login
    if not login.API_ID or not login.API_HASH:
        raise ValueError("API_ID/API_HASH is missing. Configure Telegram Login first.")

    session = _build_session_from_login(login)
    if login.user_type == 1 and session is None:
        raise ValueError("No user session found. Please login on Telegram Login page first.")

    client = TelegramClient(session, login.API_ID, login.API_HASH)
    if login.user_type == 0:
        if not login.BOT_TOKEN:
            raise ValueError("BOT_TOKEN is missing for bot login.")
        await client.start(bot_token=login.BOT_TOKEN)
    else:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise ValueError("Saved user session is not authorized. Re-login from Telegram Login page.")
    return client


async def _connect_user_client() -> TelegramClient:
    """Connect using the user session string only (never bot token)."""
    login = CONFIG.login
    if not login.API_ID or not login.API_HASH:
        raise ValueError("API_ID/API_HASH is missing. Configure Telegram Login first.")

    session = None
    if login.sessions and 0 <= login.active_session < len(login.sessions):
        sess = login.sessions[login.active_session].session_string
        if sess:
            session = StringSession(sess)
    if session is None and login.SESSION_STRING:
        session = StringSession(login.SESSION_STRING)
    if session is None:
        raise ValueError("No user session found. Please login on Telegram Login page first.")

    client = TelegramClient(session, login.API_ID, login.API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise ValueError("User session is not authorized. Re-login from Telegram Login page.")
    return client


def _entity_name(entity) -> str:
    title = getattr(entity, "title", None)
    if title:
        return title
    first = getattr(entity, "first_name", "") or ""
    last = getattr(entity, "last_name", "") or ""
    full = (first + " " + last).strip()
    if full:
        return full
    username = getattr(entity, "username", None)
    if username:
        return f"@{username}"
    return str(getattr(entity, "id", "unknown"))


def _entity_type(entity) -> str:
    if getattr(entity, "broadcast", False):
        return "channel"
    if getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False):
        return "group"
    if hasattr(entity, "first_name"):
        return "person"
    if hasattr(entity, "title"):
        return "group/channel"
    return "peer"


async def _resolve_peer_with_client(client: TelegramClient, peer_raw: Any) -> Dict[str, Any]:
    peer = _parse_peer(peer_raw)
    if peer == "":
        raise ValueError("empty peer")

    entity = await client.get_entity(peer)
    peer_id = await client.get_peer_id(entity)
    return {
        "input": str(peer_raw),
        "id": peer_id,
        "name": _entity_name(entity),
        "username": getattr(entity, "username", "") or "",
        "type": _entity_type(entity),
    }


async def _resolve_peers_batch(peers_with_role: List[Any]) -> Dict[str, List[Dict[str, Any]]]:
    cache = _peer_cache()
    rows: List[Dict[str, Any]] = []
    errors: List[str] = []
    unresolved = []

    for role, peer in peers_with_role:
        if str(peer).strip() == "":
            continue
        cached = cache.get(_cache_key(peer))
        if cached:
            row = dict(cached)
            row["role"] = role
            rows.append(row)
        else:
            unresolved.append((role, peer))

    if unresolved:
        client = await _connect_client()
        try:
            for role, peer in unresolved:
                try:
                    resolved = await _resolve_peer_with_client(client, peer)
                    cache[_cache_key(peer)] = dict(resolved)
                    resolved["role"] = role
                    rows.append(resolved)
                except Exception as err:
                    errors.append(f"{role} {peer}: {err}")
        finally:
            await client.disconnect()

    return {"rows": rows, "errors": errors}


def _peer_dropdown_labels(peers: List[str]) -> Dict[str, str]:
    options = [str(item).strip() for item in peers if str(item).strip()]
    if not options:
        return {}

    cache = _peer_cache()
    unresolved = [item for item in options if _cache_key(item) not in cache]
    if unresolved:
        try:
            _run(_resolve_peers_batch([("channel", item) for item in unresolved]))
        except Exception:
            # Best-effort enrichment: keep raw IDs/usernames when resolution fails.
            pass

    labels: Dict[str, str] = {}
    for item in options:
        cached = cache.get(_cache_key(item), {})
        name = str(cached.get("name", "")).strip()
        peer_id = cached.get("id", item)
        if name:
            labels[item] = f"{name} ({peer_id})"
        else:
            labels[item] = item
    return labels


def _available_session_names() -> List[str]:
    names: List[str] = []
    for idx, sess in enumerate(CONFIG.login.sessions):
        raw_name = str(getattr(sess, "name", "") or "").strip()
        if raw_name:
            names.append(raw_name)
        else:
            names.append(f"Session {idx + 1}")
    return names


async def _search_peers(query: str, limit: int = 30) -> List[Dict[str, Any]]:
    q = (query or "").strip().lower()
    if not q:
        return []

    client = await _connect_user_client()
    try:
        found: List[Dict[str, Any]] = []
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            name = _entity_name(entity)
            username = getattr(entity, "username", "") or ""
            joined = f"{name} {username} {dialog.id}".lower()
            if q in joined:
                found.append(
                    {
                        "id": dialog.id,
                        "name": name,
                        "username": f"@{username}" if username else "",
                        "type": _entity_type(entity),
                    }
                )
            if len(found) >= limit:
                break
        return found
    finally:
        await client.disconnect()


def _validate_forward(forward: Forward) -> Dict[str, List[str]]:
    errors: List[str] = []
    warnings: List[str] = []

    source = _parse_peer(forward.source)
    dest = [_parse_peer(item) for item in forward.dest if str(item).strip() != ""]

    if source == "":
        errors.append("Source is required.")
    if not dest:
        errors.append("At least one destination is required.")

    if forward.offset < 0:
        errors.append("Offset must be >= 0.")
    if forward.end is not None and forward.end < 0:
        errors.append("End must be >= 0.")
    if forward.end and forward.offset and forward.end < forward.offset:
        errors.append("End must be greater than or equal to Offset.")

    if source != "" and source in dest:
        warnings.append("Source is also present in destinations.")

    if len(dest) != len(set(dest)):
        warnings.append("Duplicate destinations found.")

    session_names = _available_session_names()
    download_session_name = (getattr(forward, "download_session_name", "") or "").strip()
    upload_session_name = (getattr(forward, "upload_session_name", "") or "").strip()
    if download_session_name and download_session_name not in session_names:
        warnings.append(f"Download session '{download_session_name}' is not found in Telegram Login sessions.")
    if upload_session_name and upload_session_name not in session_names:
        warnings.append(f"Upload session '{upload_session_name}' is not found in Telegram Login sessions.")

    return {"errors": errors, "warnings": warnings}


async def _preview_next_message(forward: Forward) -> Dict[str, Any]:
    source = _parse_peer(forward.source)
    if source == "":
        raise ValueError("Source is empty.")

    client = await _connect_client()
    cache = _peer_cache()
    try:
        source_cached = cache.get(_cache_key(source))
        if source_cached:
            source_entity = await client.get_entity(source_cached["id"])
        else:
            source_entity = await client.get_entity(source)
        source_id = await client.get_peer_id(source_entity)
        if not source_cached:
            cache[_cache_key(source)] = {
                "input": str(forward.source),
                "id": source_id,
                "name": _entity_name(source_entity),
                "username": getattr(source_entity, "username", "") or "",
                "type": _entity_type(source_entity),
            }

        destinations = []
        for d in forward.dest:
            if str(d).strip() == "":
                continue
            try:
                cached = cache.get(_cache_key(d))
                if cached:
                    destinations.append(
                        {
                            "id": cached["id"],
                            "name": cached["name"],
                            "type": cached["type"],
                        }
                    )
                    continue
                ent = await client.get_entity(_parse_peer(d))
                resolved_id = await client.get_peer_id(ent)
                cache[_cache_key(d)] = {
                    "input": str(d),
                    "id": resolved_id,
                    "name": _entity_name(ent),
                    "username": getattr(ent, "username", "") or "",
                    "type": _entity_type(ent),
                }
                destinations.append(
                    {
                        "id": resolved_id,
                        "name": _entity_name(ent),
                        "type": _entity_type(ent),
                    }
                )
            except Exception as err:
                destinations.append(
                    {
                        "id": str(d),
                        "name": "<unresolved>",
                        "type": str(err),
                    }
                )

        next_message = None
        exact_mode = bool(forward.offset and forward.end and int(forward.offset) == int(forward.end))

        if exact_mode:
            msg = await client.get_messages(source_id, ids=forward.offset)
            if msg and not isinstance(msg, MessageService):
                next_message = msg
        else:
            async for msg in client.iter_messages(source_id, reverse=True, offset_id=forward.offset):
                if forward.end and msg.id > forward.end:
                    continue
                if isinstance(msg, MessageService):
                    continue
                next_message = msg
                break

        if not next_message:
            return {
                "source": {
                    "id": source_id,
                    "name": _entity_name(source_entity),
                    "type": _entity_type(source_entity),
                },
                "targets": destinations,
                "message": None,
            }

        text = getattr(next_message, "message", "") or ""
        return {
            "source": {
                "id": source_id,
                "name": _entity_name(source_entity),
                "type": _entity_type(source_entity),
            },
            "targets": destinations,
            "message": {
                "id": next_message.id,
                "grouped_id": getattr(next_message, "grouped_id", None),
                "text_preview": _preview_text(text, limit=240),
                "media": _message_media_info(next_message),
            },
        }
    finally:
        await client.disconnect()


async def _message_caption_for_target(target_raw: Any, message_id_raw: Any) -> Dict[str, Any]:
    """Fetch one message by target peer and message id."""

    target = _parse_peer(target_raw)
    message_id = int(str(message_id_raw).strip())
    if target == "":
        raise ValueError("Target channel/group/person id is required.")
    if message_id <= 0:
        raise ValueError("Message ID must be a positive integer.")

    client = await _connect_client()
    try:
        entity = await client.get_entity(target)
        target_id = await client.get_peer_id(entity)
        msg = await client.get_messages(entity, ids=message_id)
        if not msg:
            return {
                "target_id": target_id,
                "target_name": _entity_name(entity),
                "target_type": _entity_type(entity),
                "message_id": message_id,
                "caption": "",
                "found": False,
            }

        text = (getattr(msg, "raw_text", None) or getattr(msg, "message", "") or "").strip()
        caption_message_id = msg.id
        grouped_id = getattr(msg, "grouped_id", None)
        caption_source = "exact_message"

        return {
            "target_id": target_id,
            "target_name": _entity_name(entity),
            "target_type": _entity_type(entity),
            "message_id": caption_message_id,
            "requested_message_id": message_id,
            "grouped_id": grouped_id,
            "caption": text,
            "caption_source": caption_source,
            "found": True,
        }
    finally:
        await client.disconnect()


async def _debug_messages_around_target(target_raw: Any, message_id_raw: Any, radius: int = 4) -> Dict[str, Any]:
    """Fetch a window of nearby messages to debug wrong-id/wrong-caption issues."""

    target = _parse_peer(target_raw)
    message_id = int(str(message_id_raw).strip())
    if target == "":
        raise ValueError("Target channel/group/person id is required.")
    if message_id <= 0:
        raise ValueError("Message ID must be a positive integer.")

    radius = max(1, min(20, int(radius)))
    low = max(1, message_id - radius)
    high = message_id + radius

    client = await _connect_client()
    try:
        entity = await client.get_entity(target)
        target_id = await client.get_peer_id(entity)
        ids = list(range(low, high + 1))

        rows: List[Dict[str, Any]] = []
        for idx in ids:
            msg = await client.get_messages(entity, ids=idx)
            if not msg:
                rows.append(
                    {
                        "message_id": idx,
                        "exists": "no",
                        "is_requested": "yes" if idx == message_id else "",
                        "grouped_id": "-",
                        "has_media": "-",
                        "text_preview": "",
                    }
                )
                continue

            text = (getattr(msg, "raw_text", None) or getattr(msg, "message", "") or "").strip()
            rows.append(
                {
                    "message_id": msg.id,
                    "exists": "yes",
                    "is_requested": "yes" if msg.id == message_id else "",
                    "grouped_id": getattr(msg, "grouped_id", None) or "-",
                    "has_media": "yes" if bool(getattr(msg, "media", None)) else "no",
                    "text_preview": _preview_text(text, limit=90),
                }
            )

        return {
            "target_id": target_id,
            "target_name": _entity_name(entity),
            "target_type": _entity_type(entity),
            "requested_message_id": message_id,
            "range_start": low,
            "range_end": high,
            "rows": rows,
        }
    finally:
        await client.disconnect()


async def _search_messages_by_text(target_raw: Any, query_raw: Any, limit: int = 20) -> Dict[str, Any]:
    """Search messages in a target chat by text and return matching messages."""

    target = _parse_peer(target_raw)
    query = str(query_raw or "").strip()
    if target == "":
        raise ValueError("Target channel/group/person id is required.")
    if not query:
        raise ValueError("Search string is required.")

    limit = max(1, min(100, int(limit)))

    client = await _connect_client()
    try:
        entity = await client.get_entity(target)
        target_id = await client.get_peer_id(entity)

        rows: List[Dict[str, Any]] = []
        async for msg in client.iter_messages(entity, search=query, limit=limit):
            text = (getattr(msg, "raw_text", None) or getattr(msg, "message", "") or "").strip()
            rows.append(
                {
                    "message_id": msg.id,
                    "grouped_id": getattr(msg, "grouped_id", None) or "-",
                    "has_media": "yes" if bool(getattr(msg, "media", None)) else "no",
                    "text_preview": _preview_text(text, limit=120),
                    "full_text": text,
                }
            )

        return {
            "target_id": target_id,
            "target_name": _entity_name(entity),
            "target_type": _entity_type(entity),
            "query": query,
            "rows": rows,
        }
    finally:
        await client.disconnect()


async def _latest_target_messages(forward: Forward) -> List[Dict[str, Any]]:
    """Return destination peers with their latest message ID."""

    client = await _connect_client()
    cache = _peer_cache()
    rows: List[Dict[str, Any]] = []
    try:
        for d in forward.dest:
            if str(d).strip() == "":
                continue

            row: Dict[str, Any] = {
                "input": str(d),
                "target_id": "-",
                "target_name": "<unresolved>",
                "target_type": "-",
                "latest_message_id": "-",
                "latest_grouped_id": "-",
            }

            try:
                cached = cache.get(_cache_key(d))
                if cached:
                    target_entity = await client.get_entity(cached["id"])
                    target_id = cached["id"]
                    target_name = cached["name"]
                    target_type = cached["type"]
                else:
                    target_entity = await client.get_entity(_parse_peer(d))
                    target_id = await client.get_peer_id(target_entity)
                    target_name = _entity_name(target_entity)
                    target_type = _entity_type(target_entity)
                    cache[_cache_key(d)] = {
                        "input": str(d),
                        "id": target_id,
                        "name": target_name,
                        "username": getattr(target_entity, "username", "") or "",
                        "type": target_type,
                    }

                row["target_id"] = target_id
                row["target_name"] = target_name
                row["target_type"] = target_type

                latest = None
                async for msg in client.iter_messages(target_entity, limit=1):
                    if isinstance(msg, MessageService):
                        continue
                    latest = msg
                    break

                if latest:
                    row["latest_message_id"] = latest.id
                    row["latest_grouped_id"] = getattr(latest, "grouped_id", None) or "-"
                else:
                    row["latest_message_id"] = "(no user message found)"
            except Exception as err:
                row["latest_message_id"] = f"error: {err}"

            rows.append(row)

        return rows
    finally:
        await client.disconnect()


def _message_export_row(message, source_name: str, source_id: int) -> Dict[str, Any]:
    media = _message_media_info(message)
    reply_to = getattr(message, "reply_to", None)
    media_obj = getattr(message, "media", None)
    return {
        "source_name": source_name,
        "source_id": source_id,
        "message_id": getattr(message, "id", ""),
        "date": getattr(message, "date", None).isoformat() if getattr(message, "date", None) else "",
        "grouped_id": getattr(message, "grouped_id", None) or "",
        "sender_id": getattr(message, "sender_id", None) or "",
        "is_service": bool(isinstance(message, MessageService)),
        "has_media": media["has_media"],
        "media_class": type(media_obj).__name__ if media_obj is not None else "-",
        "mime_type": media["mime_type"],
        "file_name": media["file_name"],
        "size_bytes": media["size_bytes"],
        "size_human": media["size_human"],
        "duration_seconds": media["duration_seconds"],
        "dimensions": media["dimensions"],
        "views": getattr(message, "views", None) or "",
        "forwards": getattr(message, "forwards", None) or "",
        "reply_to_msg_id": getattr(reply_to, "reply_to_msg_id", None) or "",
        "text": (getattr(message, "message", "") or "").strip(),
    }


async def _export_channel_messages_to_csv(source_raw: Any) -> Dict[str, Any]:
    client = await _connect_client()
    try:
        source = _parse_peer(source_raw)
        if source == "":
            raise ValueError("Source is empty.")

        entity = await client.get_entity(source)
        source_id = await client.get_peer_id(entity)
        source_name = _entity_name(entity)

        export_dir = Path("document")
        export_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_name = f"channel_messages_{abs(source_id)}_{timestamp}.csv"
        file_path = export_dir / file_name

        fieldnames = [
            "source_name",
            "source_id",
            "message_id",
            "date",
            "grouped_id",
            "sender_id",
            "is_service",
            "has_media",
            "media_class",
            "mime_type",
            "file_name",
            "size_bytes",
            "size_human",
            "duration_seconds",
            "dimensions",
            "views",
            "forwards",
            "reply_to_msg_id",
            "text",
        ]

        count = 0
        with file_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            async for message in client.iter_messages(entity, reverse=True):
                writer.writerow(_message_export_row(message, source_name, source_id))
                count += 1
                if count % 500 == 0:
                    print(f"    exported {count} messages...", flush=True)

        return {
            "file_path": str(file_path),
            "file_name": file_name,
            "message_count": count,
            "channel_name": source_name,
            "channel_id": source_id,
            "source_name": source_name,
            "source_id": source_id,
        }
    finally:
        await client.disconnect()


def _caption_text(message) -> str:
    return (getattr(message, "raw_text", None) or getattr(message, "message", "") or "").strip()


async def _last_non_service_message(entity, client: TelegramClient, limit: int = 30):
    async for msg in client.iter_messages(entity, limit=limit):
        if isinstance(msg, MessageService):
            continue
        return msg
    return None


async def _find_source_message_by_exact_caption(
        source_entity,
        caption: str,
        client: TelegramClient,
):
    async for msg in client.iter_messages(source_entity, search=caption, limit=100):
        if isinstance(msg, MessageService):
            continue
        if _caption_text(msg) == caption:
            return msg
    return None


async def _collect_album_ids(
        source_entity,
        grouped_id: int,
        anchor_id: int,
        client: TelegramClient,
) -> List[int]:
    lower_id = max(0, anchor_id - 200)
    upper_id = anchor_id + 200
    ids: List[int] = []
    async for msg in client.iter_messages(source_entity, min_id=lower_id, max_id=upper_id, reverse=True):
        if isinstance(msg, MessageService):
            continue
        if getattr(msg, "grouped_id", None) == grouped_id:
            ids.append(msg.id)
    ids.sort()
    return ids


async def _apply_resume_offsets_and_end() -> Dict[str, Any]:
    """Compute and persist offset/end from destination caption for enabled connections."""

    rows: List[Dict[str, Any]] = []
    updated = 0

    client = await _connect_client()
    try:
        for idx, forward in enumerate(CONFIG.forwards):
            con_label = forward.con_name.strip() or f"Connection {idx + 1}"

            if not forward.use_this:
                rows.append({"connection": con_label, "status": "skipped", "details": "disabled"})
                continue

            source = _parse_peer(forward.source)
            destinations = [_parse_peer(item) for item in forward.dest if str(item).strip() != ""]
            if source == "" or not destinations:
                rows.append(
                    {
                        "connection": con_label,
                        "status": "skipped",
                        "details": "missing source or destination",
                    }
                )
                continue

            try:
                source_entity = await client.get_entity(source)
                dest_entity = await client.get_entity(destinations[0])

                destination_last = await _last_non_service_message(dest_entity, client)
                if destination_last is None:
                    rows.append(
                        {
                            "connection": con_label,
                            "status": "no-update",
                            "details": "destination has no user message",
                        }
                    )
                    continue

                destination_grouped_id = getattr(destination_last, "grouped_id", None)
                caption = _caption_text(destination_last)
                if destination_grouped_id is not None and not caption:
                    async for msg in client.iter_messages(dest_entity, limit=30):
                        if isinstance(msg, MessageService):
                            continue
                        if getattr(msg, "grouped_id", None) == destination_grouped_id:
                            caption = _caption_text(msg)
                            if caption:
                                break

                if not caption:
                    rows.append(
                        {
                            "connection": con_label,
                            "status": "no-update",
                            "details": "destination last message has no caption/text",
                        }
                    )
                    continue

                source_match = await _find_source_message_by_exact_caption(source_entity, caption, client)
                if source_match is None:
                    rows.append(
                        {
                            "connection": con_label,
                            "status": "no-update",
                            "details": "source caption match not found",
                        }
                    )
                    continue

                source_last = await client.get_messages(source_entity, limit=1)
                source_last_id = source_match.id
                if source_last:
                    source_last_id = source_last[0].id

                new_offset = source_match.id
                source_grouped_id = getattr(source_match, "grouped_id", None)
                if destination_grouped_id is not None and source_grouped_id is not None:
                    album_ids = await _collect_album_ids(
                        source_entity,
                        source_grouped_id,
                        source_match.id,
                        client,
                    )
                    if album_ids:
                        new_offset = album_ids[-1]

                old_offset = int(forward.offset or 0)
                old_end = int(forward.end or 0)
                new_end = int(source_last_id)
                forward.offset = int(new_offset)
                forward.end = new_end
                updated += 1
                rows.append(
                    {
                        "connection": con_label,
                        "status": "updated",
                        "details": f"offset {old_offset} -> {forward.offset}, end {old_end} -> {forward.end}",
                    }
                )
            except Exception as err:
                rows.append(
                    {
                        "connection": con_label,
                        "status": "error",
                        "details": str(err),
                    }
                )
    finally:
        await client.disconnect()

    if updated > 0:
        write_config(CONFIG)

    return {"updated": updated, "rows": rows}


def _inject_connections_theme() -> None:
    dark = CONFIG.theme == "dark"
    hero_border = "rgba(56, 189, 248, 0.35)" if dark else "rgba(14, 96, 82, 0.25)"
    hero_grad_a = "rgba(14, 165, 233, 0.2)" if dark else "rgba(31, 184, 205, 0.18)"
    hero_grad_b = "rgba(16, 185, 129, 0.25)" if dark else "rgba(22, 163, 74, 0.18)"
    hero_bg_a = "rgba(8, 19, 31, 0.95)" if dark else "rgba(240, 253, 250, 0.95)"
    hero_bg_b = "rgba(16, 36, 55, 0.92)" if dark else "rgba(236, 253, 245, 0.92)"
    hero_title = "#e2e8f0" if dark else "#065f46"
    hero_sub = "#94a3b8" if dark else "#14532d"
    card_border = "rgba(148, 163, 184, 0.35)" if dark else "rgba(148, 163, 184, 0.25)"
    card_bg_top = "rgba(15, 23, 42, 0.62)" if dark else "rgba(255,255,255,0.92)"
    card_bg_bottom = "rgba(15, 23, 42, 0.48)" if dark else "rgba(248,250,252,0.86)"
    chip_text = "#cbd5e1" if dark else "#0f172a"
    chip_bg = "rgba(15, 23, 42, 0.62)" if dark else "rgba(255,255,255,0.8)"

    st.markdown(
        f"""
        <style>
            .conn-hero {{
                border: 1px solid {hero_border};
                background:
                    radial-gradient(1200px 300px at 15% -20%, {hero_grad_a}, transparent),
                    radial-gradient(1200px 300px at 95% 120%, {hero_grad_b}, transparent),
                    linear-gradient(135deg, {hero_bg_a}, {hero_bg_b});
                border-radius: 16px;
                padding: 18px 20px;
                margin-bottom: 14px;
            }}

            .conn-hero h2 {{
                margin: 0;
                color: {hero_title};
                font-size: 1.4rem;
            }}

            .conn-hero p {{
                margin: 8px 0 0 0;
                color: {hero_sub};
                font-size: 0.96rem;
            }}

            .conn-card {{
                border: 1px solid {card_border};
                border-radius: 14px;
                padding: 12px 14px;
                background: linear-gradient(180deg, {card_bg_top}, {card_bg_bottom});
                margin: 8px 0;
            }}

            .conn-chip {{
                display: inline-block;
                border-radius: 999px;
                padding: 3px 10px;
                font-size: 0.78rem;
                font-weight: 700;
                margin-right: 6px;
                border: 1px solid rgba(15, 23, 42, 0.12);
                background: {chip_bg};
                color: {chip_text};
            }}
        </style>
        """,
        unsafe_allow_html=True,
    )

st.set_page_config(
    page_title="Connections",
    page_icon="🔗",
    layout="wide",
)
hide_st(st)
switch_theme(st,CONFIG)
if check_password(st):
    _inject_connections_theme()
    apply_page_chrome(
        st,
        CONFIG,
        "Connection Studio",
        "Design, validate, and inspect forwarding routes with faster diagnostics and cleaner controls.",
        chips=["Routing", "Preview", "Lookup", "Past Mode"],
    )

    action_left, action_mid, action_right = st.columns([1.15, 0.95, 0.9])
    with action_left:
        add_new = st.button("＋ Add Connection", type="primary", use_container_width=True)
    with action_mid:
        save_top = st.button("Save All Changes", key="save-top", use_container_width=True)
    with action_right:
        st.markdown(
            f"<div class='conn-card'><span class='conn-chip'>Total</span><strong>{len(CONFIG.forwards)}</strong> connection(s)</div>",
            unsafe_allow_html=True,
        )

    if add_new:
        CONFIG.forwards.append(Forward())
        write_config(CONFIG)
        st.rerun()

    if save_top:
        write_config(CONFIG)
        st.rerun()

    with st.expander("🔍 Channel Search", expanded=False):
        st.markdown("Search your Telegram dialogs by name, username, or ID keyword.")
        search_col, limit_col = st.columns([3, 1])
        with search_col:
            channel_search_query = st.text_input(
                "Search query",
                key="global-channel-search-query",
                placeholder="e.g. news, @mychannel, 1234567890",
            )
        with limit_col:
            channel_search_limit = int(
                st.number_input(
                    "Max results",
                    min_value=1,
                    max_value=200,
                    value=30,
                    step=1,
                    key="global-channel-search-limit",
                )
            )
        if st.button("Search channels", key="global-channel-search-btn"):
            if not channel_search_query.strip():
                st.session_state["global-channel-search-error"] = "Please enter a search query."
                st.session_state["global-channel-search-rows"] = []
            else:
                try:
                    with st.spinner("Searching dialogs..."):
                        found = _run(_search_peers(channel_search_query, limit=channel_search_limit))
                    st.session_state["global-channel-search-rows"] = found
                    st.session_state["global-channel-search-error"] = ""
                except Exception as err:
                    st.session_state["global-channel-search-rows"] = []
                    st.session_state["global-channel-search-error"] = str(err)

        search_error = st.session_state.get("global-channel-search-error", "")
        if search_error:
            st.error(search_error)
        search_rows = st.session_state.get("global-channel-search-rows")
        if search_rows is not None:
            if search_rows:
                st.caption(f"Found {len(search_rows)} result(s). Click any cell to copy.")
                _render_copyable_table(
                    search_rows,
                    [
                        {"field": "id", "label": "ID"},
                        {"field": "name", "label": "Name"},
                        {"field": "username", "label": "Username"},
                        {"field": "type", "label": "Type"},
                    ],
                    key="global_channel_search",
                    min_height=160,
                )
            else:
                st.info("No dialogs matched your query.")

    with st.expander("Past Resume Settings", expanded=False):
        current_resume_setting = bool(
            getattr(CONFIG.past, "resume_from_destination_caption", True)
        )
        resume_setting = st.checkbox(
            "Resume by destination last caption / album caption",
            value=current_resume_setting,
            help=(
                "Use destination latest caption to match source message, then auto-set offset/end. "
                "If the match is an album, send that source album first."
            ),
        )
        try:
            CONFIG.past.resume_from_destination_caption = resume_setting
        except Exception:
            # Rebuild legacy/partial past settings with the new field present.
            raw_past = {}
            if hasattr(CONFIG.past, "dict"):
                raw_past = CONFIG.past.dict()
            raw_past["resume_from_destination_caption"] = resume_setting
            CONFIG.past = PastSettings(**raw_past)
        st.caption("This is a global past-mode behavior and applies to all connections.")
        if st.button("Save resume setting", key="save-past-resume-setting"):
            write_config(CONFIG)
            st.rerun()

        if st.button("Update offsets/end from destination captions", key="apply-past-resume-offset-end"):
            try:
                with st.spinner("Computing resume offsets/end for enabled connections..."):
                    apply_result = _run(_apply_resume_offsets_and_end())
                st.session_state["past-resume-apply-result"] = apply_result
            except Exception as err:
                st.session_state["past-resume-apply-result"] = {
                    "updated": 0,
                    "rows": [{"connection": "-", "status": "error", "details": str(err)}],
                }

        apply_result = st.session_state.get("past-resume-apply-result")
        if apply_result:
            st.caption(f"Updated connections: {apply_result.get('updated', 0)}")
            _render_copyable_table(
                apply_result.get("rows", []),
                [
                    {"field": "connection", "label": "Connection"},
                    {"field": "status", "label": "Status"},
                    {"field": "details", "label": "Details"},
                ],
                key="past_resume_apply_rows",
                min_height=160,
            )

    num = len(CONFIG.forwards)

    if num == 0:
        st.info(
            "No connections found yet. Click 'Add Connection' to create your first route."
        )
    else:
        active_count = len([f for f in CONFIG.forwards if f.use_this])
        paused_count = num - active_count
        st.markdown(
            (
                "<div class='conn-card'>"
                f"<span class='conn-chip'>Active {active_count}</span>"
                f"<span class='conn-chip'>Paused {paused_count}</span>"
                "<span class='conn-chip'>Tip: Save after editing to persist changes</span>"
                "</div>"
            ),
            unsafe_allow_html=True,
        )

        tab_strings = []
        for i in range(num):
            if CONFIG.forwards[i].con_name:
                label = CONFIG.forwards[i].con_name
            else:
                label = f"Connection {i+1}"
            if CONFIG.forwards[i].use_this:
                status = "🟢"
            else:
                status = "🟡"

            tab_strings.append(f"{status} {label}")

        tabs = st.tabs(list(tab_strings))

        for i in range(num):
            with tabs[i]:
                con = i + 1
                name = CONFIG.forwards[i].con_name
                if name:
                    label = f"{con} [{name}]"
                else:
                    label = str(con)

                state_badge = "🟢 Enabled" if CONFIG.forwards[i].use_this else "🟠 Paused"
                st.markdown(
                    (
                        "<div class='conn-card'>"
                        f"<div class='conn-title' style='font-size:1.05rem;'>Connection {label}</div>"
                        f"<div style='margin-top:6px;'><span class='conn-chip'>{state_badge}</span></div>"
                        "</div>"
                    ),
                    unsafe_allow_html=True,
                )

                source_candidates = []
                if str(CONFIG.forwards[i].source).strip():
                    source_candidates = [str(CONFIG.forwards[i].source).strip()]
                active_source = str(CONFIG.forwards[i].source).strip()

                with st.expander("Metadata", expanded=True):
                    st.write(f"Connection ID: **{con}**")
                    CONFIG.forwards[i].con_name = st.text_input(
                        "Name of this connection",
                        value=CONFIG.forwards[i].con_name,
                        key=f"name {con}",
                    )

                    st.caption("Untick to suspend this connection without deleting it.")
                    CONFIG.forwards[i].use_this = st.checkbox(
                        "Enable this connection",
                        value=CONFIG.forwards[i].use_this,
                        key=f"use {con}",
                    )

                with st.expander("Routing", expanded=True):
                    st.write(f"Configure source and destinations for connection {label}")

                    default_sources = source_candidates or [""]
                    source_candidates = get_list(
                        st.text_area(
                            "Sources (one per line)",
                            value=get_string(default_sources),
                            key=f"sources {con}",
                            help="Enter multiple source IDs/usernames. The selected active source is used by runtime and tools.",
                        )
                    )

                    if source_candidates:
                        source_labels = _peer_dropdown_labels(source_candidates)
                        source_picker_key = f"active-source-{con}"
                        previous_active = st.session_state.get(source_picker_key)
                        if previous_active not in source_candidates:
                            previous_active = source_candidates[0]

                        if len(source_candidates) <= 5:
                            active_source = st.segmented_control(
                                "Active source",
                                options=source_candidates,
                                default=previous_active,
                                key=source_picker_key,
                            )
                        else:
                            active_source = st.selectbox(
                                "Active source",
                                options=source_candidates,
                                index=source_candidates.index(previous_active),
                                format_func=lambda item: source_labels.get(item, item),
                                key=source_picker_key,
                            )
                        CONFIG.forwards[i].source = (active_source or source_candidates[0]).strip()
                    else:
                        CONFIG.forwards[i].source = ""
                        active_source = ""

                    if len(source_candidates) > 1:
                        st.caption("Multiple sources are configured. Forwarding and preview use the active source above.")
                    else:
                        st.caption("Add more than one line to maintain a source roster for this connection.")

                    CONFIG.forwards[i].dest = get_list(
                        st.text_area(
                            "Destinations",
                            value=get_string(CONFIG.forwards[i].dest),
                            key=f"dest {con}",
                            help="One destination per line. Supports channel IDs or usernames.",
                        )
                    )
                    st.caption("Write destinations one item per line.")

                    session_choices = [""] + _available_session_names()
                    current_download_name = (getattr(CONFIG.forwards[i], "download_session_name", "") or "").strip()
                    current_upload_name = (getattr(CONFIG.forwards[i], "upload_session_name", "") or "").strip()
                    download_index = session_choices.index(current_download_name) if current_download_name in session_choices else 0
                    upload_index = session_choices.index(current_upload_name) if current_upload_name in session_choices else 0

                    sess_left, sess_right = st.columns(2)
                    with sess_left:
                        selected_download_session = st.selectbox(
                            "Download session (optional)",
                            options=session_choices,
                            index=download_index,
                            key=f"download-session-{con}",
                            format_func=lambda value: value if value else "Use runtime source session",
                            help="Use this user session when reading/downloading source media for this connection.",
                        )
                    with sess_right:
                        selected_upload_session = st.selectbox(
                            "Upload session (optional)",
                            options=session_choices,
                            index=upload_index,
                            key=f"upload-session-{con}",
                            format_func=lambda value: value if value else "Use runtime source session",
                            help="Use this user session when sending uploaded media for this connection.",
                        )

                    CONFIG.forwards[i].download_session_name = (selected_download_session or "").strip()
                    CONFIG.forwards[i].upload_session_name = (selected_upload_session or "").strip()
                    st.caption("Set account A for download and account B for upload by choosing different sessions here.")

                    st.markdown(
                        (
                            "<div class='conn-card'>"
                            f"<span class='conn-chip'>Sources: {len(source_candidates)}</span>"
                            f"<span class='conn-chip'>Active source set: {'yes' if str(CONFIG.forwards[i].source).strip() else 'no'}</span>"
                            f"<span class='conn-chip'>Destination count: {len(CONFIG.forwards[i].dest)}</span>"
                            f"<span class='conn-chip'>Download session: {(CONFIG.forwards[i].download_session_name or 'default')}</span>"
                            f"<span class='conn-chip'>Upload session: {(CONFIG.forwards[i].upload_session_name or 'default')}</span>"
                            "</div>"
                        ),
                        unsafe_allow_html=True,
                    )

                    if st.button("Display source/destination IDs and names", key=f"resolve-src-dest-{con}"):
                        peers = [(f"source-{idx+1}", src) for idx, src in enumerate(source_candidates)] + [
                            ("destination", item) for item in CONFIG.forwards[i].dest
                        ]
                        try:
                            with st.spinner("Resolving source/destination peers..."):
                                resolved = _run(_resolve_peers_batch(peers))
                            st.session_state[f"resolve-src-dest-rows-{con}"] = resolved["rows"]
                            st.session_state[f"resolve-src-dest-errors-{con}"] = resolved["errors"]
                        except Exception as err:
                            st.session_state[f"resolve-src-dest-rows-{con}"] = []
                            st.session_state[f"resolve-src-dest-errors-{con}"] = [str(err)]

                    resolved_rows = st.session_state.get(f"resolve-src-dest-rows-{con}", [])
                    resolved_errors = st.session_state.get(f"resolve-src-dest-errors-{con}", [])
                    if resolved_errors:
                        for err in resolved_errors:
                            st.error(err)
                    if resolved_rows:
                        _render_copyable_table(
                            resolved_rows,
                            [
                                {"field": "role", "label": "Role"},
                                {"field": "id", "label": "ID"},
                                {"field": "name", "label": "Name"},
                                {"field": "username", "label": "Username"},
                                {"field": "type", "label": "Type"},
                            ],
                            key=f"src_dest_{con}",
                        )

                with st.expander("Validation, Preview, and Lookup", expanded=True):
                    st.markdown("### Source and Destination Context")
                    source_labels = _peer_dropdown_labels(source_candidates)
                    ctx_left, ctx_right = st.columns(2)
                    with ctx_left:
                        source_input_mode = st.segmented_control(
                            "Source channel input",
                            options=["Dropdown", "Manual input"],
                            default="Dropdown",
                            key=f"ctx-source-mode-{con}",
                        )
                        if source_input_mode == "Dropdown" and source_candidates:
                            selected_source = st.selectbox(
                                "Source channel ID / Username",
                                options=source_candidates,
                                index=(source_candidates.index(active_source) if active_source in source_candidates else 0),
                                format_func=lambda item: source_labels.get(item, item),
                                key=f"ctx-source-dropdown-{con}",
                            )
                        else:
                            selected_source = st.text_input(
                                "Source channel ID / Username",
                                value=active_source,
                                key=f"ctx-source-manual-{con}",
                                placeholder="-1001234567890 or channel_username",
                            ).strip()

                    dest_candidates = [str(item).strip() for item in CONFIG.forwards[i].dest if str(item).strip()]
                    dest_labels = _peer_dropdown_labels(dest_candidates)
                    with ctx_right:
                        dest_input_mode = st.segmented_control(
                            "Destination channel input",
                            options=["Dropdown", "Manual input"],
                            default="Dropdown",
                            key=f"ctx-dest-mode-{con}",
                        )
                        if dest_input_mode == "Dropdown" and dest_candidates:
                            selected_destination = st.selectbox(
                                "Destination channel ID / Username",
                                options=dest_candidates,
                                format_func=lambda item: dest_labels.get(item, item),
                                key=f"ctx-dest-dropdown-{con}",
                            )
                        else:
                            selected_destination = st.text_input(
                                "Destination channel ID / Username",
                                value=(dest_candidates[0] if dest_candidates else ""),
                                key=f"ctx-dest-manual-{con}",
                                placeholder="-1001234567890 or channel_username",
                            ).strip()

                    scoped_forward = CONFIG.forwards[i].copy(deep=True)
                    if selected_source:
                        scoped_forward.source = selected_source
                    if selected_destination:
                        scoped_forward.dest = [selected_destination]

                    validation = _validate_forward(scoped_forward)
                    if validation["errors"]:
                        for err in validation["errors"]:
                            st.error(err)
                    else:
                        st.success("Parameter validation passed.")
                    for warn in validation["warnings"]:
                        st.warning(warn)

                    if st.button("Preview next past-mode message", key=f"preview-next-{con}"):
                        try:
                            with st.spinner("Loading message preview..."):
                                preview = _run(_preview_next_message(scoped_forward))
                            src = preview["source"]
                            source_col, targets_col = st.columns(2)
                            with source_col:
                                st.write("Source")
                                st.dataframe([src], width="stretch")
                            if preview["targets"]:
                                with targets_col:
                                    st.write("Targets")
                                    st.dataframe(preview["targets"], width="stretch")
                            if preview["message"]:
                                st.write("Message preview")
                                msg = preview["message"]
                                msg_col1, msg_col2 = st.columns(2)
                                with msg_col1:
                                    st.dataframe(
                                        [
                                            {
                                                "id": msg["id"],
                                                "grouped_id": msg["grouped_id"],
                                                "has_media": msg["media"]["has_media"],
                                                "file_name": msg["media"]["file_name"],
                                                "mime_type": msg["media"]["mime_type"],
                                            }
                                        ],
                                        width="stretch",
                                    )
                                with msg_col2:
                                    st.dataframe(
                                        [
                                            {
                                                "blob_size": msg["media"]["size_human"],
                                                "size_bytes": msg["media"]["size_bytes"],
                                                "duration_seconds": msg["media"]["duration_seconds"],
                                                "dimensions": msg["media"]["dimensions"],
                                            }
                                        ],
                                        width="stretch",
                                    )
                                st.text_area(
                                    "Caption/Text preview",
                                    value=msg["text_preview"],
                                    height=120,
                                    disabled=True,
                                    key=f"message-preview-text-{con}",
                                )
                            else:
                                st.warning("No eligible message found for current offset/end.")
                        except Exception as err:
                            st.error(f"Preview failed: {err}")

                    st.markdown("### Destination Channel Lookup")
                    target_override = get_list(
                        st.text_area(
                            "Destination channel ID / Username (one per line). Leave empty to use the destination context above.",
                            key=f"latest-target-input-{con}",
                            placeholder="-1001234567890\nmy_channel_username",
                        )
                    )
                    if st.button("Get destination latest message ID", key=f"latest-target-msg-{con}"):
                        try:
                            with st.spinner("Resolving destination channels and fetching latest message IDs..."):
                                temp_forward = scoped_forward.copy(deep=True)
                                if target_override:
                                    temp_forward.dest = target_override
                                elif selected_destination:
                                    temp_forward.dest = [selected_destination]
                                latest_rows = _run(_latest_target_messages(temp_forward))
                            st.session_state[f"latest-target-rows-{con}"] = latest_rows
                            st.session_state[f"latest-target-error-{con}"] = ""
                        except Exception as err:
                            st.session_state[f"latest-target-rows-{con}"] = []
                            st.session_state[f"latest-target-error-{con}"] = str(err)

                    latest_rows = st.session_state.get(f"latest-target-rows-{con}")
                    latest_error = st.session_state.get(f"latest-target-error-{con}", "")
                    if latest_error:
                        st.error(f"Failed to fetch latest message IDs: {latest_error}")
                    elif latest_rows is not None:
                        if latest_rows:
                            st.write("Destination channels with latest message IDs")
                            _render_copyable_table(
                                latest_rows,
                                [
                                    {"field": "input", "label": "Input"},
                                    {"field": "target_id", "label": "Target ID"},
                                    {"field": "target_name", "label": "Target Name"},
                                    {"field": "target_type", "label": "Target Type"},
                                    {"field": "latest_message_id", "label": "Latest Message ID"},
                                    {"field": "latest_grouped_id", "label": "Latest Grouped ID"},
                                ],
                                key=f"latest_target_{con}",
                            )
                        else:
                            st.warning("No destinations configured.")

                    st.markdown("---")
                    st.write("Get message caption/text by source or destination")
                    caption_channel_mode = st.segmented_control(
                        "Lookup channel input",
                        options=["Dropdown", "Manual input"],
                        default="Dropdown",
                        key=f"caption-channel-mode-{con}",
                    )
                    caption_channel_options = [item for item in [selected_source, selected_destination] if str(item).strip()]
                    target_for_caption = ""
                    if caption_channel_mode == "Dropdown" and caption_channel_options:
                        caption_labels = _peer_dropdown_labels(caption_channel_options)
                        target_for_caption = st.selectbox(
                            "Lookup channel ID / Username",
                            options=caption_channel_options,
                            format_func=lambda item: caption_labels.get(item, item),
                            key=f"caption-target-dropdown-{con}",
                        )
                    else:
                        target_for_caption = st.text_input(
                            "Lookup channel ID / Username",
                            key=f"caption-target-manual-{con}",
                            value=(caption_channel_options[0] if caption_channel_options else ""),
                            placeholder="-1001234567890 or channel_username",
                        ).strip()

                    message_id_for_caption = st.text_input(
                        "Message ID",
                        key=f"caption-message-id-{con}",
                        placeholder="12345",
                    )
                    if st.button("Show message caption/text", key=f"caption-btn-{con}"):
                        try:
                            with st.spinner("Fetching message..."):
                                message_view = _run(
                                    _message_caption_for_target(
                                        target_for_caption,
                                        message_id_for_caption,
                                    )
                                )
                            st.session_state[f"caption-result-{con}"] = message_view
                            st.session_state[f"caption-error-{con}"] = ""
                            st.session_state.pop(f"caption-debug-result-{con}", None)
                            st.session_state.pop(f"caption-debug-error-{con}", None)
                        except Exception as err:
                            st.session_state[f"caption-error-{con}"] = str(err)

                    caption_error = st.session_state.get(f"caption-error-{con}", "")
                    if caption_error:
                        st.error(caption_error)
                    caption_result = st.session_state.get(f"caption-result-{con}")
                    if caption_result:
                        _render_copyable_table(
                            [
                                {
                                    "target_id": caption_result["target_id"],
                                    "target_name": caption_result["target_name"],
                                    "target_type": caption_result["target_type"],
                                    "requested_message_id": caption_result.get("requested_message_id", caption_result["message_id"]),
                                    "message_id": caption_result["message_id"],
                                    "grouped_id": caption_result.get("grouped_id") or "-",
                                    "caption_source": caption_result.get("caption_source", "exact_message"),
                                }
                            ],
                            [
                                {"field": "target_id", "label": "Target ID"},
                                {"field": "target_name", "label": "Target Name"},
                                {"field": "target_type", "label": "Target Type"},
                                {"field": "requested_message_id", "label": "Requested Message ID"},
                                {"field": "message_id", "label": "Caption Source Message ID"},
                                {"field": "grouped_id", "label": "Grouped ID"},
                                {"field": "caption_source", "label": "Caption Source"},
                            ],
                            key=f"caption_meta_{con}",
                            min_height=120,
                        )
                        if caption_result.get("found"):
                            caption_key = (
                                f"caption-text-{con}-"
                                f"{caption_result.get('requested_message_id', caption_result.get('message_id', '-'))}-"
                                f"{caption_result.get('message_id', '-') }"
                            )
                            st.text_area(
                                "Message caption/text",
                                value=caption_result.get("caption", ""),
                                height=140,
                                disabled=True,
                                key=caption_key,
                            )
                        else:
                            st.warning("Message not found.")

                        st.markdown("### Debug Message ID Window")
                        dbg_radius = int(
                            st.number_input(
                                "Debug radius (messages on each side)",
                                min_value=1,
                                max_value=20,
                                value=4,
                                step=1,
                                key=f"caption-debug-radius-{con}",
                            )
                        )
                        if st.button("Debug nearby message IDs", key=f"caption-debug-btn-{con}"):
                            try:
                                with st.spinner("Loading nearby messages for debug..."):
                                    debug_result = _run(
                                        _debug_messages_around_target(
                                            target_for_caption,
                                            message_id_for_caption,
                                            radius=dbg_radius,
                                        )
                                    )
                                st.session_state[f"caption-debug-result-{con}"] = debug_result
                                st.session_state[f"caption-debug-error-{con}"] = ""
                            except Exception as err:
                                st.session_state[f"caption-debug-error-{con}"] = str(err)

                        debug_error = st.session_state.get(f"caption-debug-error-{con}", "")
                        if debug_error:
                            st.error(f"Debug failed: {debug_error}")

                        debug_result = st.session_state.get(f"caption-debug-result-{con}")
                        if debug_result:
                            st.caption(
                                f"Target: {debug_result['target_name']} ({debug_result['target_id']}) | "
                                f"Requested ID: {debug_result['requested_message_id']} | "
                                f"Window: {debug_result['range_start']}..{debug_result['range_end']}"
                            )
                            _render_copyable_table(
                                debug_result.get("rows", []),
                                [
                                    {"field": "message_id", "label": "Message ID"},
                                    {"field": "exists", "label": "Exists"},
                                    {"field": "is_requested", "label": "Requested"},
                                    {"field": "grouped_id", "label": "Grouped ID"},
                                    {"field": "has_media", "label": "Has Media"},
                                    {"field": "text_preview", "label": "Text Preview"},
                                ],
                                key=f"caption_debug_{con}",
                                min_height=180,
                            )

                    st.markdown("---")
                    st.markdown("### Search Messages by Source and Destination")
                    msg_search_text = st.text_input(
                        "Search text",
                        key=f"msg-search-text-{con}",
                        placeholder="#Cat",
                    )
                    msg_search_limit = int(
                        st.number_input(
                            "Max results",
                            min_value=1,
                            max_value=100,
                            value=20,
                            step=1,
                            key=f"msg-search-limit-{con}",
                        )
                    )

                    search_src_col, search_dst_col = st.columns(2)
                    with search_src_col:
                        if st.button("Search in source channel", key=f"msg-search-src-btn-{con}"):
                            try:
                                with st.spinner("Searching messages in source channel..."):
                                    msg_search_result = _run(
                                        _search_messages_by_text(
                                            selected_source,
                                            msg_search_text,
                                            limit=msg_search_limit,
                                        )
                                    )
                                st.session_state[f"msg-search-result-{con}"] = msg_search_result
                                st.session_state[f"msg-search-error-{con}"] = ""
                            except Exception as err:
                                st.session_state[f"msg-search-error-{con}"] = str(err)

                    with search_dst_col:
                        if st.button("Search in destination channel", key=f"msg-search-dst-btn-{con}"):
                            try:
                                with st.spinner("Searching messages in destination channel..."):
                                    msg_search_result = _run(
                                        _search_messages_by_text(
                                            selected_destination,
                                            msg_search_text,
                                            limit=msg_search_limit,
                                        )
                                    )
                                st.session_state[f"msg-search-result-{con}"] = msg_search_result
                                st.session_state[f"msg-search-error-{con}"] = ""
                            except Exception as err:
                                st.session_state[f"msg-search-error-{con}"] = str(err)

                    msg_search_error = st.session_state.get(f"msg-search-error-{con}", "")
                    if msg_search_error:
                        st.error(f"Message search failed: {msg_search_error}")

                    msg_search_result = st.session_state.get(f"msg-search-result-{con}")
                    if msg_search_result:
                        rows = msg_search_result.get("rows", [])
                        st.caption(
                            f"Channel: {msg_search_result['target_name']} ({msg_search_result['target_id']}) | "
                            f"Query: {msg_search_result['query']} | Results: {len(rows)}"
                        )
                        if rows:
                            _render_copyable_table(
                                rows,
                                [
                                    {"field": "message_id", "label": "Message ID"},
                                    {"field": "grouped_id", "label": "Grouped ID"},
                                    {"field": "has_media", "label": "Has Media"},
                                    {"field": "text_preview", "label": "Text Preview"},
                                ],
                                key=f"msg_search_{con}",
                                min_height=180,
                            )
                            st.text_area(
                                "Top result full message",
                                value=rows[0].get("full_text", ""),
                                height=120,
                                disabled=True,
                                key=f"msg-search-top-full-{con}-{rows[0].get('message_id', '-')}",
                            )
                        else:
                            st.warning("No messages found for this search text.")

                    st.markdown("---")
                    st.markdown("### Export Source Channel")
                    st.caption("Export all messages from the selected source channel to a CSV file.")
                    export_state_key = f"channel-export-result-{con}"
                    export_error_key = f"channel-export-error-{con}"
                    if st.button("Export source channel messages to CSV", key=f"export-channel-csv-{con}"):
                        try:
                            with st.spinner("Exporting channel messages..."):
                                export_result = _run(
                                    _export_channel_messages_to_csv(active_source)
                                )
                            st.session_state[export_state_key] = export_result
                            st.session_state[export_error_key] = ""
                        except Exception as err:
                            st.session_state[export_state_key] = None
                            st.session_state[export_error_key] = str(err)

                    export_error = st.session_state.get(export_error_key, "")
                    if export_error:
                        st.error(f"Export failed: {export_error}")

                    export_result = st.session_state.get(export_state_key)
                    if export_result:
                        st.success(
                            f"Exported {export_result['message_count']} messages from {export_result.get('channel_name', export_result.get('source_name', '-'))}"
                        )
                        export_path = Path(export_result["file_path"])
                        if export_path.exists():
                            st.download_button(
                                "Download exported CSV",
                                data=export_path.read_bytes(),
                                file_name=export_result["file_name"],
                                mime="text/csv",
                                key=f"download-channel-csv-{con}",
                            )

                    st.markdown("### Export Destination Channel")
                    st.caption("Export all messages from the selected destination channel to a CSV file.")

                    export_dest_state_key = f"dest-channel-export-result-{con}"
                    export_dest_error_key = f"dest-channel-export-error-{con}"

                    chosen_dest = str(selected_destination or "").strip()
                    if not chosen_dest:
                        st.caption("No destination channel is selected.")

                    if st.button("Export destination channel messages to CSV", key=f"export-dest-channel-csv-{con}"):
                        if not chosen_dest:
                            st.session_state[export_dest_state_key] = None
                            st.session_state[export_dest_error_key] = "No destination channel selected."
                        else:
                            try:
                                with st.spinner("Exporting destination channel messages..."):
                                    export_dest_result = _run(
                                        _export_channel_messages_to_csv(chosen_dest)
                                    )
                                st.session_state[export_dest_state_key] = export_dest_result
                                st.session_state[export_dest_error_key] = ""
                            except Exception as err:
                                st.session_state[export_dest_state_key] = None
                                st.session_state[export_dest_error_key] = str(err)

                    export_dest_error = st.session_state.get(export_dest_error_key, "")
                    if export_dest_error:
                        st.error(f"Destination export failed: {export_dest_error}")

                    export_dest_result = st.session_state.get(export_dest_state_key)
                    if export_dest_result:
                        st.success(
                            f"Exported {export_dest_result['message_count']} messages from destination {export_dest_result.get('channel_name', export_dest_result.get('source_name', '-'))}"
                        )
                        export_dest_path = Path(export_dest_result["file_path"])
                        if export_dest_path.exists():
                            st.download_button(
                                "Download destination CSV",
                                data=export_dest_path.read_bytes(),
                                file_name=export_dest_result["file_name"],
                                mime="text/csv",
                                key=f"download-dest-channel-csv-{con}",
                            )

                with st.expander("Past Mode Settings", expanded=False):
                    CONFIG.forwards[i].offset = int(
                        st.number_input(
                            "Offset",
                            min_value=0,
                            value=int(CONFIG.forwards[i].offset or 0),
                            step=1,
                            key=f"offset {con}",
                            help="Start forwarding from this message ID in past mode.",
                        )
                    )
                    CONFIG.forwards[i].end = int(
                        st.number_input(
                            "End",
                            min_value=0,
                            value=int(CONFIG.forwards[i].end or 0),
                            step=1,
                            key=f"end {con}",
                            help="Stop forwarding at this message ID (inclusive).",
                        )
                    )

                with st.expander("Delete this connection"):
                    st.warning(
                        f"Clicking the 'Remove' button will **delete** connection **{label}**. This action cannot be reversed once done.",
                        icon="⚠️",
                    )

                    if st.button(f"Remove connection **{label}**"):
                        del CONFIG.forwards[i]
                        write_config(CONFIG)
                        st.rerun()

    if st.button("Save", key="save-bottom"):
        write_config(CONFIG)
        st.rerun()
