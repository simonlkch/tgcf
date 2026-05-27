"""Shared helpers for forwarding single messages and albums."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import time
from typing import Iterable, List, Optional

from telethon import TelegramClient
from telethon.tl.custom.message import Message

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

        sent_messages = []
        try:
            sent_messages = await forward_batch_with_retry(
                dest,
                ordered_messages,
                reply_to=reply_to,
            )
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
    from tgcf.utils import cleanup, send_message

    transformed = []
    fallback_downloaded_files: List[str] = []
    downloaded_media_cache = downloaded_media_cache or {}

    async def _get_downloaded_file(tm):
        message_id = int(getattr(tm.message, "id", 0) or 0)
        cached = downloaded_media_cache.get(message_id)
        if cached and os.path.exists(cached):
            return cached

        file_path = await tm.get_file()
        if file_path:
            fallback_downloaded_files.append(file_path)
            if message_id:
                downloaded_media_cache[message_id] = file_path
        return file_path

    try:
        for message in messages:
            tm = await apply_plugins(message)
            if not tm:
                return None
            transformed.append(tm)

        client: TelegramClient = transformed[0].client
        source_chat_id = getattr(transformed[0].message, "chat_id", None)
        restricted_pair = (source_chat_id, recipient)

        if CONFIG.show_forwarded_from and restricted_pair not in FORWARD_RESTRICTED_PAIRS:
            try:
                logging.info("send_batch using forward_messages: recipient=%s count=%s", recipient, len(transformed))
                forwarded = await client.forward_messages(
                    recipient,
                    [tm.message for tm in transformed],
                    reply_to=reply_to,
                )
                if isinstance(forwarded, list):
                    return forwarded
                return [forwarded]
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

        if CONFIG.show_forwarded_from and restricted_pair in FORWARD_RESTRICTED_PAIRS:
            logging.info(
                "known restricted pair source=%s recipient=%s; skipping forward attempt",
                source_chat_id,
                recipient,
            )

        async def send_with_media_fallback(tm):
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
                try:
                    logging.info(
                        "Fallback phase: uploading media file=%s recipient=%s caption=%s",
                        file_path,
                        recipient,
                        _preview_text(tm.text),
                    )
                    sent = await _send_file_fast_compatible(
                        client,
                        recipient,
                        file_path,
                        caption=tm.text,
                        reply_to=reply_to,
                        part_size_kb=CONFIG.live.transfer_part_size_kb,
                        source_media_type=tm.file_type,
                        thumb=getattr(tm, "thumb_file", None),
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
            return [await send_message(recipient, tm) for tm in transformed]

        if len(transformed) == 1:
            logging.info("send_batch single message path for recipient=%s", recipient)
            return [await send_with_media_fallback(transformed[0])]

        file_paths = []
        captions = []
        for tm in transformed:
            captions.append(tm.text)
            if tm.new_file:
                file_paths.append(tm.new_file)
                continue
            if tm.file_type != FileType.NOFILE:
                file_paths.append(await _get_downloaded_file(tm))
                continue
            break
        else:
            logging.info(
                "send_batch album upload path: recipient=%s file_count=%s",
                recipient,
                len(file_paths),
            )
            uploaded = await _send_file_fast_compatible(
                client,
                recipient,
                file_paths,
                caption=captions,
                reply_to=reply_to,
                part_size_kb=CONFIG.live.transfer_part_size_kb,
                source_media_type=transformed[0].file_type,
            )
            if isinstance(uploaded, list):
                return uploaded
            return [uploaded]

        return [await send_with_media_fallback(tm) for tm in transformed]
    finally:
        for tm in transformed:
            tm.clear()
        if cleanup_downloaded_files and fallback_downloaded_files:
            cleanup(*set(fallback_downloaded_files))