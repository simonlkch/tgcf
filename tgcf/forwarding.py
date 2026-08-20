"""Shared helpers for forwarding single messages and albums."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import json
import logging
import mimetypes
import os
import time
from typing import Iterable, List, Optional

from telethon import TelegramClient
from telethon.tl.custom.message import Message
from tqdm import tqdm

from tgcf.fast_transfer import download_file
from tgcf.logging_utils import log_event
from tgcf.plugin_models import FileType
from tgcf.utils import _send_file_fast_compatible


ALBUM_DEBOUNCE_MS = 1000
MAX_NON_429_RETRIES = 3
BACKOFF_BASE_SECONDS = 1
MAX_FLOOD_WAIT_RETRIES = 10
FAST_SEND_FILE_PART_SIZE_KB = 1024
FORWARD_RESTRICTED_PAIRS = set()
LOGGER = logging.getLogger(__name__)
UPLOAD_SESSION_CLIENTS: dict[str, TelegramClient] = {}
# Track last-use time for cached session clients so we can recycle stale ones.
# Long-running processes (2-3+ hours) can have their connections silently dropped
# by NAT/firewalls, causing RPC calls to hang forever.
_UPLOAD_SESSION_LAST_USED: dict[str, float] = {}
_UPLOAD_SESSION_MAX_IDLE_SECONDS = 30 * 60  # 30 minutes
_CLIENT_PING_LOCK: dict[str, asyncio.Lock] = {}
_CLIENT_PING_TASK: dict[str, asyncio.Task] = {}


async def _ping_client_keepalive(client: TelegramClient, label: str) -> None:
    """Periodically ping a client to keep the connection alive through NAT/firewalls."""
    try:
        while client.is_connected():
            await asyncio.sleep(60)  # ping every 60 seconds
            try:
                await asyncio.wait_for(client.get_me(), timeout=15)
            except (asyncio.TimeoutError, ConnectionError, OSError) as ping_err:
                logging.warning(
                    "keepalive ping failed for session=%s error=%s; will reconnect on next use",
                    label,
                    ping_err,
                )
                with suppress(Exception):
                    await client.disconnect()
                return
            except Exception:
                # Ping failures on a healthy connection are non-fatal
                return
    except asyncio.CancelledError:
        return
    except Exception:
        return


async def _ensure_client_alive(client: TelegramClient, label: str) -> TelegramClient:
    """Verify a cached client is alive; reconnect if needed. Start keepalive pings."""
    try:
        if not client.is_connected():
            await client.connect()
        # Send a lightweight ping to verify liveness
        await asyncio.wait_for(client.get_me(), timeout=20)
    except Exception as err:
        logging.warning("cached session client '%s' is dead, reconnecting: %s", label, err)
        with suppress(Exception):
            await client.disconnect()
        await client.connect()
        if not await client.is_user_authorized():
            raise

    # Start a keepalive ping task if not already running
    key = label.casefold()
    existing_task = _CLIENT_PING_TASK.get(key)
    if not existing_task or existing_task.done():
        _CLIENT_PING_LOCK[key] = asyncio.Lock()
        _CLIENT_PING_TASK[key] = asyncio.create_task(_ping_client_keepalive(client, label))

    _UPLOAD_SESSION_LAST_USED[key] = time.time()
    return client


def _preview_text(text: Optional[str], limit: int = 120) -> str:
    """Return a compact single-line preview for logs."""

    clean = (text or "").replace("\n", " ").strip()
    if not clean:
        return "(no text/caption)"
    if len(clean) <= limit:
        preview = clean
    else:
        preview = clean[: limit - 3] + "..."

    return preview.encode("ascii", errors="backslashreplace").decode("ascii")


def _message_preview(message: Message) -> str:
    """Build a short preview from message body/caption for log visibility."""

    return _preview_text(getattr(message, "message", "") or "")


def _active_session_label() -> str:
    """Return a human-readable label for the active source session."""

    from tgcf.config import CONFIG

    login = CONFIG.login
    if login.user_type == 1:
        if login.sessions and 0 <= login.active_session < len(login.sessions):
            name = (login.sessions[login.active_session].name or "").strip()
            if name:
                return f"{name} (active source session)"
        if login.SESSION_STRING:
            return "legacy source session"
    if login.user_type == 0:
        return "bot session"
    return "source client session"


async def _peer_label(client: TelegramClient, peer_id: Optional[int]) -> str:
    """Return a readable peer label like 'Channel title (-100...)'."""

    if peer_id is None:
        return "unknown"
    try:
        entity = await client.get_entity(peer_id)
    except Exception:
        return str(peer_id)
    title = (
        getattr(entity, "title", None)
        or getattr(entity, "username", None)
        or " ".join(
            part
            for part in (
                getattr(entity, "first_name", None),
                getattr(entity, "last_name", None),
            )
            if part
        )
        or str(peer_id)
    )
    return f"{_preview_text(str(title), 80)} ({peer_id})"


@dataclass(frozen=True)
class ForwardBatch:
    """A normalized forwarding unit.

    A batch contains either one message or multiple messages that share the same
    grouped_id.
    """

    chat_id: int
    grouped_id: Optional[int]
    messages: List[Message]

    @property
    def is_album(self) -> bool:
        return self.grouped_id is not None


def build_forward_batches(messages: Iterable[Message]) -> List[ForwardBatch]:
    """Group messages into single-message or album batches.

    The input is expected to be in chronological order.
    """

    batches: List[ForwardBatch] = []
    current_grouped_id: Optional[int] = None
    current_chat_id: Optional[int] = None
    current_messages: List[Message] = []

    def flush() -> None:
        nonlocal current_grouped_id, current_chat_id, current_messages
        if not current_messages:
            return
        batches.append(
            ForwardBatch(
                chat_id=current_chat_id or current_messages[0].chat_id,
                grouped_id=current_grouped_id,
                messages=list(current_messages),
            )
        )
        current_grouped_id = None
        current_chat_id = None
        current_messages = []

    for message in messages:
        grouped_id = getattr(message, "grouped_id", None)
        if grouped_id is None:
            flush()
            batches.append(
                ForwardBatch(
                    chat_id=message.chat_id,
                    grouped_id=None,
                    messages=[message],
                )
            )
            continue

        if current_messages and (
            grouped_id != current_grouped_id or message.chat_id != current_chat_id
        ):
            flush()

        current_grouped_id = grouped_id
        current_chat_id = message.chat_id
        current_messages.append(message)

    flush()
    return batches


def is_flood_wait_error(err: Exception) -> bool:
    return err.__class__.__name__ == "FloodWaitError" or "FLOOD_WAIT" in str(err)


def flood_wait_seconds(err: Exception) -> int:
    return int(getattr(err, "seconds", 0) or 0)


def is_cannot_forward_error(err: Exception) -> bool:
    err_name = err.__class__.__name__
    err_text = str(err).upper()
    return any(
        marker in err_name
        for marker in (
            "ChatForwardsRestrictedError",
            "ChatNotModifiedError",
            "FileReferenceExpiredError",
        )
    ) or "FORWARDS_RESTRICTED" in err_text or "FILE_REFERENCE_EXPIRED" in err_text or "FORWARD" in err_text and "RESTRICT" in err_text


def is_chat_forwards_restricted_error(err: Exception) -> bool:
    err_name = err.__class__.__name__
    err_text = str(err).upper()
    return err_name == "ChatForwardsRestrictedError" or "FORWARDS_RESTRICTED" in err_text


def is_file_reference_expired_error(err: Exception) -> bool:
    err_name = err.__class__.__name__
    err_text = str(err).upper()
    return err_name == "FileReferenceExpiredError" or "FILE_REFERENCE_EXPIRED" in err_text


def is_permission_error(err: Exception) -> bool:
    err_name = err.__class__.__name__
    err_text = str(err).upper()
    return any(
        marker in err_name
        for marker in (
            "ChatWriteForbiddenError",
            "UserBannedInChannelError",
            "UserNotParticipantError",
            "ChannelPrivateError",
            "ChatAdminRequiredError",
        )
    ) or "WRITE_FORBIDDEN" in err_text or "ADMIN_REQUIRED" in err_text


def is_retryable_error(err: Exception) -> bool:
    if is_flood_wait_error(err):
        return True
    if is_permission_error(err):
        return False
    return True


async def _refresh_messages_for_forwarding(messages: List[Message]) -> List[Message]:
    """Refetch source messages so expired file references can be recreated."""

    if not messages:
        return messages

    first_message = messages[0]
    client = getattr(first_message, "client", None)
    source_chat_id = getattr(first_message, "chat_id", None)
    message_ids = [getattr(message, "id", None) for message in messages]
    if client is None or source_chat_id is None or any(message_id is None for message_id in message_ids):
        return messages

    refreshed = await client.get_messages(source_chat_id, ids=message_ids)
    if not refreshed:
        return messages
    if not isinstance(refreshed, list):
        refreshed = [refreshed]

    refreshed_by_id = {message.id: message for message in refreshed if getattr(message, "id", None) is not None}
    rebuilt = [refreshed_by_id.get(message_id, message) for message, message_id in zip(messages, message_ids)]
    return rebuilt


def _route_sessions_for_pair(source_chat_id: Optional[int], recipient: int) -> tuple[str, str]:
    """Return configured (download_session_name, upload_session_name) for a source/destination pair."""

    from tgcf import config

    if source_chat_id is None:
        return "", ""

    configured = config.forward_by_source.get(source_chat_id)
    if configured:
        resolved_destinations = config.from_to.get(source_chat_id, [])
        if recipient in resolved_destinations:
            return (
                (getattr(configured, "download_session_name", "") or "").strip(),
                (getattr(configured, "upload_session_name", "") or "").strip(),
            )

    return "", ""


async def _get_upload_client(upload_session_name: str, source_client: TelegramClient) -> TelegramClient:
    """Resolve upload client from a configured session name; fallback to source client."""

    from tgcf.config import CONFIG, get_session_for_name

    name = (upload_session_name or "").strip()
    if not name:
        return source_client
    if CONFIG.login.user_type != 1:
        return source_client

    key = name.casefold()

    # Evict clients that have been idle too long — their connections are likely dead
    # after long idle periods which causes freezes.
    last_used = _UPLOAD_SESSION_LAST_USED.get(key, 0)
    if last_used and (time.time() - last_used) > _UPLOAD_SESSION_MAX_IDLE_SECONDS:
        stale = UPLOAD_SESSION_CLIENTS.pop(key, None)
        _UPLOAD_SESSION_LAST_USED.pop(key, None)
        ping_task = _CLIENT_PING_TASK.pop(key, None)
        if ping_task and not ping_task.done():
            ping_task.cancel()
        if stale:
            with suppress(Exception):
                await stale.disconnect()
            logging.info("evicted stale cached session client '%s' (idle > %ss)", name, _UPLOAD_SESSION_MAX_IDLE_SECONDS)

    cached = UPLOAD_SESSION_CLIENTS.get(key)
    if cached:
        try:
            cached = await _ensure_client_alive(cached, name)
            UPLOAD_SESSION_CLIENTS[key] = cached
            return cached
        except Exception as err:
            logging.warning("cached session '%s' unrecoverable: %s; creating new client", name, err)
            UPLOAD_SESSION_CLIENTS.pop(key, None)
            _UPLOAD_SESSION_LAST_USED.pop(key, None)
            ping_task = _CLIENT_PING_TASK.pop(key, None)
            if ping_task and not ping_task.done():
                ping_task.cancel()

    session = get_session_for_name(name, default=f"tgcf_upload_{key}")
    client = TelegramClient(session, CONFIG.login.API_ID, CONFIG.login.API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise ValueError(f"Upload session '{name}' is not authorized")
    client = await _ensure_client_alive(client, name)
    UPLOAD_SESSION_CLIENTS[key] = client
    return client


async def forward_source_batch(messages: List[Message], destinations: List[int]):
    """Forward one normalized source batch to all destination chats."""

    from tgcf import storage as st
    from tgcf.config import CONFIG

    if not messages or not destinations:
        log_event(
            LOGGER,
            logging.INFO,
            "forward_source_batch",
            outcome="skipped",
            messages_present=bool(messages),
            destinations_present=bool(destinations),
        )
        return

    ordered_messages = sorted(messages, key=lambda message: message.id)
    source_chat_id = ordered_messages[0].chat_id
    grouped_id = getattr(ordered_messages[0], "grouped_id", None)
    album_uid = st.album_key(source_chat_id, grouped_id)
    for dest in destinations:
        sent_messages = []
        try:
            start_ts = time.perf_counter()
            wide_event = {
                "event": "forward_source_batch",
                "source_chat_id": source_chat_id,
                "grouped_id": grouped_id,
                "is_album": grouped_id is not None,
                "message_count": len(ordered_messages),
                "destination_count": len(destinations),
                "destination_chat_id": dest,
                "first_caption_preview": _message_preview(ordered_messages[0]),
                "album_atomic": bool(CONFIG.live.album_atomic),
                "forward_from_enabled": bool(CONFIG.show_forwarded_from),
            }
            reply_to = None
            updated_event_uids = []
            for source_message in ordered_messages:
                if not getattr(source_message, "is_reply", False):
                    continue
                r_event = st.DummyEvent(
                    source_chat_id, getattr(source_message, "reply_to_msg_id", None)
                )
                r_event_uid = st.EventUid(r_event)
                previous = st.stored.get(r_event_uid, {}).get(dest)
                if previous:
                    reply_to = getattr(previous, "id", None)
                    break
            wide_event["reply_to_message_id"] = reply_to

            async def _forward_with_refetch_retry():
                try:
                    return await forward_batch_with_retry(
                        dest,
                        ordered_messages,
                        reply_to=reply_to,
                    )
                except Exception as err:
                    if not is_file_reference_expired_error(err):
                        raise
                    refreshed_messages = await _refresh_messages_for_forwarding(ordered_messages)
                    if refreshed_messages == ordered_messages:
                        raise
                    logging.warning(
                        "file reference expired while forwarding source=%s recipient=%s; refetching messages and retrying",
                        source_chat_id,
                        dest,
                    )
                    return await forward_batch_with_retry(
                        dest,
                        refreshed_messages,
                        reply_to=reply_to,
                    )

            sent_messages = await _forward_with_refetch_retry()

            if not sent_messages:
                wide_event["outcome"] = "no_outgoing_messages"
                wide_event["sent_count"] = 0
                continue
            if not isinstance(sent_messages, list):
                sent_messages = [sent_messages]

            if album_uid and sent_messages:
                st.stored_albums.setdefault(album_uid, {})[dest] = sent_messages[0]

            if len(sent_messages) != len(ordered_messages):
                logging.warning(
                    "forwarded message count mismatch for chat=%s album=%s: source=%s sent=%s",
                    source_chat_id,
                    grouped_id,
                    len(ordered_messages),
                    len(sent_messages),
                )
                wide_event["message_count_mismatch"] = True
                wide_event["sent_count"] = len(sent_messages)
                wide_event["source_count"] = len(ordered_messages)

            for source_message, sent_message in zip(ordered_messages, sent_messages):
                event = st.DummyEvent(source_chat_id, source_message.id)
                event_uid = st.EventUid(event)
                st.stored.setdefault(event_uid, {})[dest] = sent_message
                updated_event_uids.append(event_uid)
            wide_event["outcome"] = "success"
            wide_event["sent_count"] = len(sent_messages)
        except Exception as err:
            wide_event["outcome"] = "error"
            wide_event["error_type"] = type(err).__name__
            wide_event["error_message"] = str(err)
            if CONFIG.live.album_atomic:
                for sent_message in sent_messages:
                    try:
                        await sent_message.delete()
                    except Exception:
                        logging.exception("failed to rollback sent album message")
                for event_uid in updated_event_uids:
                    stored_for_event = st.stored.get(event_uid)
                    if stored_for_event and dest in stored_for_event:
                        del stored_for_event[dest]
                        if not stored_for_event:
                            del st.stored[event_uid]
                if album_uid and album_uid in st.stored_albums:
                    stored_for_album = st.stored_albums.get(album_uid)
                    if stored_for_album and dest in stored_for_album:
                        del stored_for_album[dest]
                        if not stored_for_album:
                            del st.stored_albums[album_uid]
            raise
        finally:
            wide_event["duration_ms"] = round((time.perf_counter() - start_ts) * 1000, 2)
            log_event(
                LOGGER,
                logging.ERROR if wide_event.get("outcome") == "error" else logging.INFO,
                wide_event.pop("event", "forward_source_batch"),
                **wide_event,
            )


async def forward_batch_with_retry(
    recipient,
    messages: List[Message],
    reply_to: Optional[int] = None,
):
    """Send a batch with flood-wait and transient retry handling."""

    import asyncio
    from tgcf.config import CONFIG
    from tgcf.utils import cleanup

    attempt = 0
    flood_wait_attempt = 0
    max_non_429_retries = CONFIG.live.retry_max_attempts_for_non_429
    max_flood_wait_retries = CONFIG.live.retry_max_attempts_for_flood_wait
    backoff_base_seconds = CONFIG.live.retry_backoff_base_seconds
    downloaded_media_cache: dict[int, str] = {}

    try:
        while True:
            try:
                logging.info(
                    "forward_batch_with_retry attempt=%s flood_wait_attempt=%s recipient=%s message_count=%s",
                    attempt + 1,
                    flood_wait_attempt,
                    recipient,
                    len(messages),
                )
                return await send_batch(
                    recipient,
                    messages,
                    reply_to=reply_to,
                    downloaded_media_cache=downloaded_media_cache,
                    cleanup_downloaded_files=False,
                )
            except Exception as err:
                if is_flood_wait_error(err):
                    if not CONFIG.live.retry_on_429:
                        logging.warning("flood wait retry disabled; raising error for recipient=%s", recipient)
                        raise
                    flood_wait_attempt += 1
                    if flood_wait_attempt > max_flood_wait_retries:
                        logging.warning(
                            "flood wait retries exhausted: recipient=%s attempts=%s",
                            recipient,
                            flood_wait_attempt,
                        )
                        raise
                    logging.warning(
                        "flood wait encountered: recipient=%s wait=%s seconds attempt=%s/%s",
                        recipient,
                        max(flood_wait_seconds(err), 1),
                        flood_wait_attempt,
                        max_flood_wait_retries,
                    )
                    await asyncio.sleep(max(flood_wait_seconds(err), 1))
                    continue

                if is_permission_error(err):
                    logging.warning("permission error for recipient=%s: %s", recipient, err)
                    raise

                if not is_retryable_error(err):
                    logging.warning("non-retryable error for recipient=%s: %s", recipient, err)
                    raise

                attempt += 1
                if attempt > max_non_429_retries:
                    logging.warning(
                        "non-429 retries exhausted: recipient=%s attempts=%s last_error=%s",
                        recipient,
                        attempt,
                        err,
                    )
                    raise
                logging.warning(
                    "retrying after error: recipient=%s attempt=%s/%s wait=%s seconds error=%s",
                    recipient,
                    attempt,
                    max_non_429_retries,
                    backoff_base_seconds * (2 ** (attempt - 1)),
                    err,
                )
                await asyncio.sleep(backoff_base_seconds * (2 ** (attempt - 1)))
    finally:
        if downloaded_media_cache:
            cleanup(*set(downloaded_media_cache.values()))


async def send_batch(
    recipient,
    messages: List[Message],
    reply_to: Optional[int] = None,
    downloaded_media_cache: Optional[dict[int, str]] = None,
    cleanup_downloaded_files: bool = True,
):
    """Forward a batch of messages, falling back to download/upload when needed."""

    from tgcf.config import CONFIG
    from tgcf.plugins import apply_plugins
    from tgcf.utils import cleanup, get_temp_dir, safe_name, send_message

    transformed = []
    fallback_downloaded_files: List[str] = []
    uploaded_temp_files: set[str] = set()
    downloaded_media_cache = downloaded_media_cache or {}

    try:
        for message in messages:
            tm = await apply_plugins(message)
            if not tm:
                return None
            transformed.append(tm)

        client: TelegramClient = transformed[0].client
        source_chat_id = getattr(transformed[0].message, "chat_id", None)
        restricted_pair = (source_chat_id, recipient)
        route_download_session_name, route_upload_session_name = _route_sessions_for_pair(
            source_chat_id,
            recipient,
        )
        download_client = await _get_upload_client(route_download_session_name, client)
        upload_client = await _get_upload_client(route_upload_session_name, client)
        source_session_label = _active_session_label()
        effective_download_session = route_download_session_name or source_session_label
        effective_upload_session = route_upload_session_name or source_session_label
        source_label = await _peer_label(client, source_chat_id)
        recipient_label = await _peer_label(upload_client, recipient)

        log_event(
            LOGGER,
            logging.INFO,
            "transfer_sessions_resolved",
            source_chat_id=source_chat_id,
            source=source_label,
            recipient=recipient,
            recipient_chat_id=recipient,
            recipient_name=recipient_label,
            download_session=effective_download_session,
            upload_session=effective_upload_session,
            download_session_routed=bool(route_download_session_name),
            upload_session_routed=bool(route_upload_session_name),
        )

        def _ensure_msg_prefix(file_path: Optional[str], message_id: int) -> Optional[str]:
            if not file_path:
                return file_path
            try:
                abs_path = os.path.abspath(file_path)
                base = os.path.basename(abs_path)
                prefix = f"{message_id}_"
                if base.startswith(prefix):
                    return abs_path
                target_name = prefix + safe_name(base)
                target_path = os.path.join(temp_root, target_name)
                if abs_path == target_path:
                    return abs_path
                if os.path.exists(target_path):
                    return target_path
                os.replace(abs_path, target_path)
                return target_path
            except Exception:
                return file_path

        async def _get_downloaded_file(tm):
            nonlocal download_client
            message_id = int(getattr(tm.message, "id", 0) or 0)
            cached = downloaded_media_cache.get(message_id)
            if cached and os.path.exists(cached):
                return cached

            if download_client is client:
                file_path = await tm.get_file()
            else:
                log_event(
                    LOGGER,
                    logging.INFO,
                    "routed_download_started",
                    source_chat_id=source_chat_id,
                    source=source_label,
                    recipient=recipient,
                    recipient_chat_id=recipient,
                    recipient_name=recipient_label,
                    message_id=message_id,
                    download_session=effective_download_session,
                )
                source_message = await download_client.get_messages(source_chat_id, ids=message_id)
                if not source_message:
                    raise ValueError(
                        f"Download session could not fetch source message {message_id} from {source_chat_id}"
                    )
                started_at = time.time()
                last_emit_at = started_at
                last_emit_bytes = 0
                download_bar = None

                def _routed_download_progress(current: int, total: int) -> None:
                    nonlocal last_emit_at, last_emit_bytes, download_bar
                    now = time.time()
                    if download_bar is None and total:
                        download_bar = tqdm(
                            total=total,
                            unit="B",
                            unit_scale=True,
                            desc=f"download msg {message_id}",
                            ascii=True,
                        )
                    if download_bar is not None:
                        download_bar.update(max(0, current - download_bar.n))
                    if (
                        current < total
                        and current - last_emit_bytes < 1024 * 1024
                        and now - last_emit_at < 0.5
                    ):
                        return
                    elapsed = max(now - started_at, 1e-6)
                    speed_mb_s = (current / elapsed) / (1024 * 1024)
                    percent = round((current / total) * 100, 2) if total else None
                    if download_bar is not None:
                        download_bar.set_postfix_str(f"{speed_mb_s:.2f} MB/s")
                    log_event(
                        LOGGER,
                        logging.INFO,
                        "transfer_progress",
                        direction="download",
                        label=f"download msg {message_id}",
                        source_chat_id=source_chat_id,
                        destination_chat_id=recipient,
                        message_id=message_id,
                        current_bytes=current,
                        total_bytes=total,
                        percent=percent,
                        speed_mb_s=round(speed_mb_s, 2),
                    )
                    last_emit_at = now
                    last_emit_bytes = current

                try:
                    document = getattr(source_message, "document", None)
                    source_file = getattr(source_message, "file", None)
                    expected_size = getattr(source_file, "size", None) or getattr(document, "size", None)
                    if document is not None and expected_size:
                        file_name = getattr(source_file, "name", None)
                        if not file_name:
                            mime_type = getattr(source_file, "mime_type", None) or ""
                            file_name = f"msg_{message_id}{mimetypes.guess_extension(mime_type) or '.bin'}"
                        target_path = os.path.join(temp_root, f"{message_id}_{safe_name(file_name)}")
                        part_path = target_path + ".part"
                        meta_path = target_path + ".meta"

                        if os.path.exists(target_path) and os.path.getsize(target_path) == expected_size:
                            file_path = target_path
                        else:
                            # Retry loop for transient stalls / connection resets.
                            # Without retries, a single dead TCP connection would freeze the
                            # entire forwarder indefinitely (typical after 2-3h uptime due to
                            # NAT / firewall idle-timeouts).
                            max_download_attempts = 5
                            last_dl_err: Optional[Exception] = None
                            for dl_attempt in range(1, max_download_attempts + 1):
                                resume_from = os.path.getsize(part_path) if os.path.exists(part_path) else 0
                                if resume_from >= expected_size:
                                    resume_from = 0
                                if resume_from > 0 and dl_attempt == 1:
                                    log_event(
                                        LOGGER,
                                        logging.INFO,
                                        "routed_download_resumed",
                                        source_chat_id=source_chat_id,
                                        source=source_label,
                                        recipient=recipient,
                                        recipient_chat_id=recipient,
                                        recipient_name=recipient_label,
                                        message_id=message_id,
                                        download_session=effective_download_session,
                                        offset=resume_from,
                                    )
                                elif dl_attempt > 1:
                                    log_event(
                                        LOGGER,
                                        logging.WARNING,
                                        "routed_download_retry",
                                        source_chat_id=source_chat_id,
                                        recipient_chat_id=recipient,
                                        message_id=message_id,
                                        attempt=dl_attempt,
                                        max_attempts=max_download_attempts,
                                        offset=resume_from,
                                        error=str(last_dl_err) if last_dl_err else None,
                                    )
                                    # After a stall, evict the cached download client so it
                                    # reconnects fresh instead of reusing a dead sender pool.
                                    if download_client is not client:
                                        dl_key = effective_download_session.casefold()
                                        dead = UPLOAD_SESSION_CLIENTS.pop(dl_key, None)
                                        _UPLOAD_SESSION_LAST_USED.pop(dl_key, None)
                                        ping_task = _CLIENT_PING_TASK.pop(dl_key, None)
                                        if ping_task and not ping_task.done():
                                            ping_task.cancel()
                                        if dead:
                                            with suppress(Exception):
                                                await dead.disconnect()
                                        from tgcf.config import get_session_for_name
                                        dl_session_name = route_download_session_name
                                        session = get_session_for_name(dl_session_name, default=f"tgcf_upload_{dl_key}")
                                        download_client = TelegramClient(
                                            session, CONFIG.login.API_ID, CONFIG.login.API_HASH
                                        )
                                        await download_client.connect()
                                        if not await download_client.is_user_authorized():
                                            await download_client.disconnect()
                                            raise ValueError(
                                                f"Download session '{dl_session_name}' is not authorized"
                                            )
                                        download_client = await _ensure_client_alive(
                                            download_client, dl_session_name
                                        )
                                        UPLOAD_SESSION_CLIENTS[dl_key] = download_client
                                mode = "ab" if resume_from > 0 else "wb"
                                try:
                                    # Cap time per attempt to avoid indefinite hang even if the
                                    # underlying chunk-level timeouts misfire.
                                    # Rough estimate: 1MB/s minimum expected speed, plus headroom.
                                    per_attempt_timeout = max(
                                        300, int(expected_size / (1024 * 1024)) * 2 + 300
                                    )
                                    with open(part_path, mode) as fp:
                                        await asyncio.wait_for(
                                            download_file(
                                                download_client,
                                                document,
                                                fp,
                                                progress_callback=_routed_download_progress,
                                                file_size=expected_size,
                                                part_size_kb=CONFIG.live.transfer_part_size_kb,
                                                connection_count=CONFIG.live.transfer_connection_count,
                                                offset=resume_from,
                                            ),
                                            timeout=per_attempt_timeout,
                                        )
                                    final_size = os.path.getsize(part_path)
                                    if final_size != expected_size:
                                        raise IOError(
                                            f"Partial routed download size mismatch: {final_size} != {expected_size}"
                                        )
                                    break
                                except (asyncio.TimeoutError, ConnectionError, OSError) as dl_err:
                                    last_dl_err = dl_err
                                    logging.warning(
                                        "download attempt %s/%s failed for msg %s: %s",
                                        dl_attempt,
                                        max_download_attempts,
                                        message_id,
                                        dl_err,
                                    )
                                    if dl_attempt >= max_download_attempts:
                                        raise
                                    await asyncio.sleep(min(2 ** (dl_attempt - 1), 15))
                                    continue
                            else:
                                if last_dl_err:
                                    raise last_dl_err
                            with open(meta_path, "w", encoding="utf-8") as meta_fp:
                                json.dump(
                                    {
                                        "offset": os.path.getsize(part_path),
                                        "expected_size": expected_size,
                                        "updated_at": int(time.time()),
                                        "file_name": file_name,
                                    },
                                    meta_fp,
                                )
                            os.replace(part_path, target_path)
                            try:
                                os.remove(meta_path)
                            except OSError:
                                pass
                            file_path = target_path
                    else:
                        file_path = await download_client.download_media(
                            source_message,
                            file=get_temp_dir(),
                            progress_callback=_routed_download_progress,
                        )
                finally:
                    if download_bar is not None:
                        download_bar.close()

            if file_path:
                file_path = _ensure_msg_prefix(file_path, message_id)
                fallback_downloaded_files.append(file_path)
                if message_id:
                    downloaded_media_cache[message_id] = file_path
            return file_path

        temp_root = os.path.abspath(get_temp_dir())

        def _remember_temp_upload_file(file_path: Optional[str]) -> None:
            if not file_path:
                return
            try:
                abs_path = os.path.abspath(file_path)
                if os.path.commonpath([abs_path, temp_root]) == temp_root:
                    uploaded_temp_files.add(abs_path)
            except Exception:
                return

        if route_upload_session_name or route_download_session_name:
            log_event(
                LOGGER,
                logging.INFO,
                "connection_session_routing",
                source_chat_id=source_chat_id,
                source=source_label,
                recipient=recipient,
                recipient_chat_id=recipient,
                recipient_name=recipient_label,
                download_session=effective_download_session,
                upload_session=effective_upload_session,
                download_session_routed=bool(route_download_session_name),
                upload_session_routed=bool(route_upload_session_name),
            )

        if (
            CONFIG.show_forwarded_from
            and upload_client is client
            and download_client is client
            and source_chat_id is not None
            and restricted_pair not in FORWARD_RESTRICTED_PAIRS
        ):
            try:
                log_event(
                    LOGGER,
                    logging.INFO,
                    "forward_direct_started",
                    source_chat_id=source_chat_id,
                    destination_chat_id=recipient,
                    message_count=len(transformed),
                )
                forwarded = await client.forward_messages(
                    recipient,
                    [tm.message for tm in transformed],
                    reply_to=reply_to,
                )
                sent_items = forwarded if isinstance(forwarded, list) else [forwarded]
                log_event(
                    LOGGER,
                    logging.INFO,
                    "forward_direct_succeeded",
                    source_chat_id=source_chat_id,
                    destination_chat_id=recipient,
                    message_count=len(transformed),
                    sent_count=len(sent_items),
                )
                return sent_items
            except Exception as err:
                if is_chat_forwards_restricted_error(err):
                    FORWARD_RESTRICTED_PAIRS.add(restricted_pair)
                    logging.warning(
                        "forward restricted source=%s recipient=%s; using download/upload fallback",
                        source_chat_id,
                        recipient,
                    )
                elif not is_cannot_forward_error(err):
                    raise
                logging.warning(
                    "forward_messages blocked by protected chat; switching to copy/re-upload path for recipient=%s",
                    recipient,
                )

        if CONFIG.show_forwarded_from and (upload_client is not client or download_client is not client):
            logging.info(
                "session-routed connection uses copy/re-upload path recipient=%s",
                recipient,
            )

        if CONFIG.show_forwarded_from and restricted_pair in FORWARD_RESTRICTED_PAIRS:
            logging.info(
                "known restricted pair source=%s recipient=%s; skipping forward attempt",
                source_chat_id,
                recipient,
            )

        async def send_with_media_fallback(tm):
            if upload_client is not client:
                if tm.file_type == FileType.NOFILE and not tm.new_file:
                    return await upload_client.send_message(recipient, tm.text, reply_to=reply_to)

                if not CONFIG.live.forward_fallback_to_reupload:
                    raise RuntimeError("routed upload requires media fallback but fallback is disabled")

                file_path = tm.new_file or await _get_downloaded_file(tm)
                _remember_temp_upload_file(file_path)
                thumb_file = getattr(tm, "thumb_file", None)
                ensure_thumb = getattr(tm, "ensure_thumb_file", None)
                if callable(ensure_thumb):
                    thumb_file = await ensure_thumb()
                _remember_temp_upload_file(thumb_file)
                return await _send_file_fast_compatible(
                    upload_client,
                    recipient,
                    file_path,
                    caption=tm.text,
                    reply_to=reply_to,
                    part_size_kb=CONFIG.live.transfer_part_size_kb,
                    source_media_type=tm.file_type,
                    thumb=thumb_file,
                )

            try:
                return await send_message(recipient, tm)
            except Exception as err:
                if not is_chat_forwards_restricted_error(err) and not is_cannot_forward_error(err):
                    raise
                if not CONFIG.live.forward_fallback_to_reupload:
                    raise
                if tm.file_type == FileType.NOFILE and not tm.new_file:
                    raise

                logging.warning(
                    "send_message blocked by protected chat; retrying as send_file recipient=%s caption=%s",
                    recipient,
                    _preview_text(tm.text),
                )
                logging.info("Fallback phase: preparing downloadable media for recipient=%s", recipient)
                file_path = tm.new_file or await _get_downloaded_file(tm)
                _remember_temp_upload_file(file_path)
                try:
                    thumb_file = getattr(tm, "thumb_file", None)
                    ensure_thumb = getattr(tm, "ensure_thumb_file", None)
                    if callable(ensure_thumb):
                        thumb_file = await ensure_thumb()
                    _remember_temp_upload_file(thumb_file)
                    logging.warning(
                        "Fallback phase: uploading media file=%s recipient=%s upload_session=%s caption=%s",
                        file_path,
                        recipient,
                        effective_upload_session,
                        _preview_text(tm.text),
                    )
                    sent = await _send_file_fast_compatible(
                        upload_client,
                        recipient,
                        file_path,
                        caption=tm.text,
                        reply_to=reply_to,
                        part_size_kb=CONFIG.live.transfer_part_size_kb,
                        source_media_type=tm.file_type,
                        thumb=thumb_file,
                    )
                    logging.info(
                        "send_file fallback succeeded: recipient=%s file=%s caption=%s",
                        recipient,
                        file_path,
                        _preview_text(tm.text),
                    )
                    return sent
                except Exception:
                    logging.exception(
                        "send_file fallback failed: recipient=%s file=%s caption=%s",
                        recipient,
                        file_path,
                        _preview_text(tm.text),
                    )
                    raise

        if not CONFIG.live.forward_fallback_to_reupload:
            if upload_client is client:
                return [await send_message(recipient, tm) for tm in transformed]
            sent_items = []
            for tm in transformed:
                if tm.file_type == FileType.NOFILE and not tm.new_file:
                    sent_items.append(await upload_client.send_message(recipient, tm.text, reply_to=reply_to))
                else:
                    file_path = tm.new_file or await _get_downloaded_file(tm)
                    _remember_temp_upload_file(file_path)
                    sent_items.append(
                        await _send_file_fast_compatible(
                            upload_client,
                            recipient,
                            file_path,
                            caption=tm.text,
                            reply_to=reply_to,
                            part_size_kb=CONFIG.live.transfer_part_size_kb,
                            source_media_type=tm.file_type,
                        )
                    )
            return sent_items

        if len(transformed) == 1:
            logging.info("send_batch single message path for recipient=%s", recipient)
            return [await send_with_media_fallback(transformed[0])]

        file_paths = []
        captions = []
        thumb_paths: List[Optional[str]] = []
        has_any_thumb = False
        for tm in transformed:
            captions.append(tm.text)
            # Resolve a thumbnail for items that need one (videos inside an
            # album, otherwise Telegram uploads the video without a thumb and
            # the destination sees a blank tile). We pass the path straight
            # to _send_file_fast_compatible which routes albums through a
            # helper that respects per-file thumbs.
            thumb_path: Optional[str] = None
            if tm.file_type in (FileType.VIDEO, FileType.VIDEO_NOTE, FileType.GIF):
                ensure_thumb = getattr(tm, "ensure_thumb_file", None)
                if callable(ensure_thumb):
                    try:
                        thumb_path = await ensure_thumb()
                    except Exception:
                        logging.exception(
                            "ensure_thumb_file failed for msg=%s; sending without thumb",
                            getattr(tm.message, "id", None),
                        )
                        thumb_path = None
                if thumb_path:
                    _remember_temp_upload_file(thumb_path)
                    has_any_thumb = True
            thumb_paths.append(thumb_path)
            if tm.new_file:
                _remember_temp_upload_file(tm.new_file)
                file_paths.append(tm.new_file)
                continue
            if tm.file_type != FileType.NOFILE:
                file_path = await _get_downloaded_file(tm)
                _remember_temp_upload_file(file_path)
                file_paths.append(file_path)
                continue
            break
        else:
            logging.info(
                "send_batch album upload path: recipient=%s file_count=%s has_thumb=%s",
                recipient,
                len(file_paths),
                has_any_thumb,
            )
            uploaded = await _send_file_fast_compatible(
                upload_client,
                recipient,
                file_paths,
                caption=captions,
                reply_to=reply_to,
                part_size_kb=CONFIG.live.transfer_part_size_kb,
                # Pass per-file media types so the album path can set
                # supports_streaming=True whenever any item is a video.
                # Using only the first file's type caused videos inside
                # mixed albums to arrive as non-streaming documents.
                source_media_type=[tm.file_type for tm in transformed],
                # Per-file thumbnail paths. Items without a thumbnail pass
                # ``None``; items with one route through our album helper so
                # Telethon's _send_album (which drops thumb) is bypassed.
                album_thumbs=thumb_paths if has_any_thumb else None,
            )
            if isinstance(uploaded, list):
                return uploaded
            return [uploaded]

        return [await send_with_media_fallback(tm) for tm in transformed]
    finally:
        for tm in transformed:
            tm.clear()
        if uploaded_temp_files:
            logging.warning(
                "cleaning uploaded temp files source=%s recipient=%s file_count=%s",
                source_chat_id if 'source_chat_id' in locals() else None,
                recipient,
                len(uploaded_temp_files),
            )
            cleanup(*sorted(uploaded_temp_files))
        if cleanup_downloaded_files and fallback_downloaded_files:
            cleanup(*set(fallback_downloaded_files))