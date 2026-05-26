import asyncio
from typing import Any, Dict, List, Optional

import streamlit as st
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.patched import MessageService

from tgcf.config import CONFIG, Forward, read_config, write_config
from tgcf.web_ui.password import check_password
from tgcf.web_ui.utils import get_list, get_string, hide_st, switch_theme

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

st.set_page_config(
    page_title="Connections",
    page_icon="🔗",
)
hide_st(st)
switch_theme(st,CONFIG)
if check_password(st):
    add_new = st.button("Add new connection")
    if add_new:
        CONFIG.forwards.append(Forward())
        write_config(CONFIG)

    num = len(CONFIG.forwards)

    if num == 0:
        st.write(
            "No connections found. Click on Add new connection above to create one!"
        )
    else:
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
                    label = con
                with st.expander("Modify Metadata"):
                    st.write(f"Connection ID: **{con}**")
                    CONFIG.forwards[i].con_name = st.text_input(
                        "Name of this connection",
                        value=CONFIG.forwards[i].con_name,
                        key=con,
                    )

                    st.info(
                        "You can untick the below checkbox to suspend this connection."
                    )
                    CONFIG.forwards[i].use_this = st.checkbox(
                        "Use this connection",
                        value=CONFIG.forwards[i].use_this,
                        key=f"use {con}",
                    )
                with st.expander("Source and Destination"):
                    st.write(f"Configure connection {label}")

                    CONFIG.forwards[i].source = st.text_input(
                        "Source",
                        value=CONFIG.forwards[i].source,
                        key=f"source {con}",
                    ).strip()
                    st.write("only one source is allowed in a connection")
                    CONFIG.forwards[i].dest = get_list(
                        st.text_area(
                            "Destinations",
                            value=get_string(CONFIG.forwards[i].dest),
                            key=f"dest {con}",
                        )
                    )
                    st.write("Write destinations one item per line")

                with st.expander("Validate, Search, and Preview"):
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

                    st.markdown("---")
                    search_query = st.text_input(
                        "Search channel/group/person by name",
                        key=f"search-name-{con}",
                        placeholder="Enter part of name or username",
                    )
                    if st.button("Search by name", key=f"search-name-btn-{con}"):
                        try:
                            with st.spinner("Searching dialogs by name..."):
                                results = _run(_search_peers(search_query))
                            if results:
                                st.dataframe(results, use_container_width=True)
                                st.caption("Use the id value from results as source/destination.")
                            else:
                                st.warning("No matches found.")
                        except Exception as err:
                            st.error(f"Search failed: {err}")

                with st.expander("Past Mode Settings"):
                    CONFIG.forwards[i].offset = int(
                        st.text_input(
                            "Offset",
                            value=str(CONFIG.forwards[i].offset),
                            key=f"offset {con}",
                        )
                    )
                    CONFIG.forwards[i].end = int(
                        st.text_input(
                            "End", value=str(CONFIG.forwards[i].end), key=f"end {con}"
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

    if st.button("Save"):
        write_config(CONFIG)
        st.rerun()
