import asyncio
import html
import json
from typing import Any, Dict, List, Optional

import streamlit as st
import streamlit.components.v1 as components
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.patched import MessageService

from tgcf.config import CONFIG, Forward, read_config, write_config
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


async def _search_peers(query: str, limit: int = 30) -> List[Dict[str, Any]]:
    q = (query or "").strip().lower()
    if not q:
        return []

    client = await _connect_client()
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

                    CONFIG.forwards[i].source = st.text_input(
                        "Source",
                        value=CONFIG.forwards[i].source,
                        key=f"source {con}",
                    ).strip()
                    st.caption("Only one source is allowed in each connection.")

                    CONFIG.forwards[i].dest = get_list(
                        st.text_area(
                            "Destinations",
                            value=get_string(CONFIG.forwards[i].dest),
                            key=f"dest {con}",
                            help="One destination per line. Supports channel IDs or usernames.",
                        )
                    )
                    st.caption("Write destinations one item per line.")

                    st.markdown(
                        (
                            "<div class='conn-card'>"
                            f"<span class='conn-chip'>Source set: {'yes' if str(CONFIG.forwards[i].source).strip() else 'no'}</span>"
                            f"<span class='conn-chip'>Destination count: {len(CONFIG.forwards[i].dest)}</span>"
                            "</div>"
                        ),
                        unsafe_allow_html=True,
                    )

                    if st.button("Display source/destination IDs and names", key=f"resolve-src-dest-{con}"):
                        peers = [("source", CONFIG.forwards[i].source)] + [
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
                    validation = _validate_forward(CONFIG.forwards[i])
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
                                preview = _run(_preview_next_message(CONFIG.forwards[i]))
                            src = preview["source"]
                            source_col, targets_col = st.columns(2)
                            with source_col:
                                st.write("Source")
                                st.dataframe([src], use_container_width=True)
                            if preview["targets"]:
                                with targets_col:
                                    st.write("Targets")
                                    st.dataframe(preview["targets"], use_container_width=True)
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
                                        use_container_width=True,
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
                                        use_container_width=True,
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

                    st.markdown("### Destination Lookups")
                    target_override = get_list(
                        st.text_area(
                            "Optional target channel/group/person IDs (one per line). Leave empty to use Destinations above.",
                            key=f"latest-target-input-{con}",
                            placeholder="-1001234567890\nmy_channel_username",
                        )
                    )
                    if st.button("Get targets and latest message ID", key=f"latest-target-msg-{con}"):
                        try:
                            with st.spinner("Resolving targets and fetching latest message IDs..."):
                                temp_forward = CONFIG.forwards[i].copy(deep=True)
                                if target_override:
                                    temp_forward.dest = target_override
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
                            st.write("Targets with latest message IDs")
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
                    st.write("Get caption/text by target ID and message ID")
                    target_for_caption = st.text_input(
                        "Target channel/group/person id",
                        key=f"caption-target-{con}",
                        placeholder="-1001234567890 or username",
                    )
                    message_id_for_caption = st.text_input(
                        "Message id",
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
                    st.markdown("### Search Dialogs")
                    search_query = st.text_input(
                        "Search channel/group/person by name",
                        key=f"search-name-{con}",
                        placeholder="Enter part of name or username",
                    )
                    results_key = f"search-name-results-{con}"
                    error_key = f"search-name-error-{con}"
                    query_key = f"search-name-last-query-{con}"
                    if st.button("Search by name", key=f"search-name-btn-{con}"):
                        try:
                            with st.spinner("Searching dialogs by name..."):
                                results = _run(_search_peers(search_query))
                            st.session_state[results_key] = results
                            st.session_state[error_key] = ""
                            st.session_state[query_key] = search_query
                        except Exception as err:
                            st.session_state[results_key] = []
                            st.session_state[error_key] = str(err)
                            st.session_state[query_key] = search_query

                    last_results = st.session_state.get(results_key)
                    last_error = st.session_state.get(error_key, "")
                    last_query = st.session_state.get(query_key, "")
                    if last_error:
                        st.error(f"Search failed: {last_error}")
                    elif last_results is not None:
                        if last_results:
                            if str(last_query).strip():
                                st.caption(f"Showing latest results for: {last_query}")
                            _render_copyable_table(
                                last_results,
                                [
                                    {"field": "id", "label": "ID"},
                                    {"field": "name", "label": "Name"},
                                    {"field": "username", "label": "Username"},
                                    {"field": "type", "label": "Type"},
                                ],
                                key=f"search_results_{con}",
                            )
                            st.caption("Use the id value from results as source/destination.")
                        else:
                            st.warning("No matches found.")

                    st.markdown("---")
                    st.markdown("### Search Messages By Text")
                    msg_search_target = st.text_input(
                        "Target ID for text search",
                        key=f"msg-search-target-{con}",
                        placeholder="-1001234567890 or username",
                    )
                    msg_search_text = st.text_input(
                        "Search string",
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

                    if st.button("Search message by string", key=f"msg-search-btn-{con}"):
                        try:
                            with st.spinner("Searching messages in target..."):
                                msg_search_result = _run(
                                    _search_messages_by_text(
                                        msg_search_target,
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
                            f"Target: {msg_search_result['target_name']} ({msg_search_result['target_id']}) | "
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
                            st.warning("No messages found for this string.")

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
