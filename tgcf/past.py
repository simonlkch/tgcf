"""The module for running tgcf in past mode.

- past mode can only operate with a user account.
- past mode deals with all existing messages.
"""

import asyncio
import logging
import time
from typing import List, Optional

from telethon import TelegramClient
from telethon.errors.rpcerrorlist import FloodWaitError
from telethon.tl.custom.message import Message
from telethon.tl.patched import MessageService

from tgcf import config
from tgcf.config import CONFIG, get_SESSION, write_config
from tgcf.forwarding import build_forward_batches, forward_source_batch
from tgcf.logging_utils import log_event
from tgcf.plugins import load_async_plugins
from tgcf.utils import clean_session_files


def _message_caption_text(message: Optional[Message]) -> str:
    if not message:
        return ""
    return (getattr(message, "message", "") or "").strip()


async def _last_non_service_message(client: TelegramClient, chat_id: int) -> Optional[Message]:
    async for message in client.iter_messages(chat_id, limit=20):
        if isinstance(message, MessageService):
            continue
        return message
    return None


async def _find_source_message_by_caption(
    client: TelegramClient, source_chat_id: int, caption: str
) -> Optional[Message]:
    async for message in client.iter_messages(source_chat_id, search=caption, limit=100):
        if isinstance(message, MessageService):
            continue
        if _message_caption_text(message) == caption:
            return message
    return None


async def _collect_source_album(
    client: TelegramClient, source_chat_id: int, grouped_id: int, anchor_id: int
) -> List[Message]:
    lower_id = max(0, anchor_id - 200)
    upper_id = anchor_id + 200
    album_messages: List[Message] = []
    async for message in client.iter_messages(
        source_chat_id,
        min_id=lower_id,
        max_id=upper_id,
        reverse=True,
    ):
        if isinstance(message, MessageService):
            continue
        if getattr(message, "grouped_id", None) == grouped_id:
            album_messages.append(message)
    return sorted(album_messages, key=lambda item: item.id)


async def _build_resume_state_from_destination(
    client: TelegramClient, source_chat_id: int, destinations: List[int]
) -> Optional[dict]:
    if not destinations:
        return None

    destination_chat_id = destinations[0]
    destination_last = await _last_non_service_message(client, destination_chat_id)
    if destination_last is None:
        return None

    destination_grouped_id = getattr(destination_last, "grouped_id", None)
    caption = _message_caption_text(destination_last)

    if destination_grouped_id is not None and not caption:
        async for msg in client.iter_messages(destination_chat_id, limit=30):
            if isinstance(msg, MessageService):
                continue
            if getattr(msg, "grouped_id", None) == destination_grouped_id:
                caption = _message_caption_text(msg)
                if caption:
                    break

    if not caption:
        return None

    source_match = await _find_source_message_by_caption(client, source_chat_id, caption)
    if source_match is None:
        return None

    source_last = await client.get_messages(source_chat_id, limit=1)
    source_last_id = source_match.id
    if source_last:
        source_last_id = source_last[0].id

    source_grouped_id = getattr(source_match, "grouped_id", None)
    if destination_grouped_id is None or source_grouped_id is None:
        return {
            "offset": source_match.id,
            "end": source_last_id,
            "album_messages": None,
            "caption": caption,
            "destination_last_id": destination_last.id,
        }

    album_messages = await _collect_source_album(
        client, source_chat_id, source_grouped_id, source_match.id
    )
    if not album_messages:
        return None

    return {
        "offset": album_messages[-1].id,
        "end": source_last_id,
        "album_messages": album_messages,
        "caption": caption,
        "destination_last_id": destination_last.id,
    }


async def forward_job() -> None:
    """Forward all existing messages in the concerned chats."""
    logger = logging.getLogger(__name__)
    job_start = time.perf_counter()
    log_event(logger, logging.INFO, "past_forward_job_started")
    clean_session_files()

    # load async plugins defined in plugin_models
    await load_async_plugins()

    if CONFIG.login.user_type != 1:
        log_event(
            logger,
            logging.WARNING,
            "past_mode_requires_user_account",
            outcome="skipped",
        )
        return
    SESSION = get_SESSION()
    async with TelegramClient(
        SESSION, CONFIG.login.API_ID, CONFIG.login.API_HASH
    ) as client:
        config.from_to = await config.load_from_to(client, config.CONFIG.forwards)

        def _is_enabled_forward(forward: config.Forward) -> bool:
            if not forward.use_this:
                return False
            source = forward.source
            if isinstance(source, int):
                return True
            if isinstance(source, str) and source.strip() != "":
                return True
            return False

        # Keep forward metadata aligned with load_from_to() filtering logic.
        active_forwards = [f for f in config.CONFIG.forwards if _is_enabled_forward(f)]

        client: TelegramClient
        for from_to, forward in zip(config.from_to.items(), active_forwards):
            src, dest = from_to

            resume_state = None
            if CONFIG.past.resume_from_destination_caption:
                resume_state = await _build_resume_state_from_destination(client, src, dest)

            if resume_state is not None:
                forward.offset = int(resume_state["offset"])
                forward.end = int(resume_state["end"])
                log_event(
                    logger,
                    logging.INFO,
                    "past_resume_state_updated",
                    source_chat_id=src,
                    destination_chat_id=dest[0] if dest else None,
                    destination_last_id=resume_state["destination_last_id"],
                    matched_caption=resume_state["caption"],
                    new_offset=forward.offset,
                    new_end=forward.end,
                    album_bootstrap=bool(resume_state["album_messages"]),
                )

                album_messages = resume_state["album_messages"]
                if album_messages:
                    await forward_source_batch(album_messages, dest)
                    log_event(
                        logger,
                        logging.INFO,
                        "past_album_bootstrap_forwarded",
                        source_chat_id=src,
                        destination_count=len(dest),
                        album_message_count=len(album_messages),
                        last_forwarded_id=album_messages[-1].id,
                    )
            elif CONFIG.past.resume_from_destination_caption:
                log_event(
                    logger,
                    logging.INFO,
                    "past_resume_state_not_found",
                    source_chat_id=src,
                    destination_chat_id=dest[0] if dest else None,
                )

            last_id = 0
            scanned_count = 0
            accepted_count = 0
            previous_grouped_id = None
            exact_id_mode = bool(
                forward.offset
                and forward.end
                and int(forward.offset) == int(forward.end)
            )
            forward: config.Forward
            source_event = {
                "source_chat_id": src,
                "destination_count": len(dest),
                "offset": forward.offset,
                "end": forward.end,
            }
            source_start = time.perf_counter()
            log_event(logger, logging.INFO, "past_source_scan_started", **source_event)
            if exact_id_mode:
                log_event(
                    logger,
                    logging.INFO,
                    "past_exact_id_mode_enabled",
                    source_chat_id=src,
                    message_id=forward.offset,
                )

            current_batch = []

            async def flush_batch():
                nonlocal last_id, current_batch
                if not current_batch:
                    return
                for batch in build_forward_batches(current_batch):
                    await forward_source_batch(batch.messages, dest)
                    last_id = batch.messages[-1].id
                    log_event(
                        logger,
                        logging.INFO,
                        "past_batch_forwarded",
                        source_chat_id=src,
                        last_forwarded_id=last_id,
                        batch_size=len(batch.messages),
                    )
                    forward.offset = last_id
                    write_config(CONFIG, persist=False)
                    await asyncio.sleep(CONFIG.past.delay)
                    log_event(
                        logger,
                        logging.INFO,
                        "past_delay_applied",
                        delay_seconds=CONFIG.past.delay,
                    )
                current_batch = []

            async def handle_message(message: Message):
                nonlocal scanned_count, accepted_count, previous_grouped_id
                scanned_count += 1

                if scanned_count % 200 == 0:
                    log_event(
                        logger,
                        logging.INFO,
                        "past_scan_progress",
                        source_chat_id=src,
                        scanned_count=scanned_count,
                        accepted_count=accepted_count,
                        last_forwarded_id=last_id,
                    )

                if forward.end and message.id > forward.end:
                    return
                if isinstance(message, MessageService):
                    return

                current_grouped_id = getattr(message, "grouped_id", None)
                if current_batch and previous_grouped_id != current_grouped_id:
                    await flush_batch()
                current_batch.append(message)
                accepted_count += 1
                previous_grouped_id = current_grouped_id

                if current_grouped_id is None:
                    await flush_batch()

            if exact_id_mode:
                try:
                    message = await client.get_messages(src, ids=forward.offset)
                    if message:
                        await handle_message(message)
                except FloodWaitError as fwe:
                    log_event(
                        logger,
                        logging.WARNING,
                        "past_flood_wait",
                        source_chat_id=src,
                        wait_seconds=fwe.seconds,
                        mode="exact_id",
                    )
                    await asyncio.sleep(delay=fwe.seconds)
                except Exception as err:
                    logger.exception(
                        {
                            "event": "past_exact_id_forward_failed",
                            "source_chat_id": src,
                            "error_type": type(err).__name__,
                            "error_message": str(err),
                        }
                    )
            else:
                async for message in client.iter_messages(
                    src, reverse=True, offset_id=forward.offset
                ):
                    message: Message
                    try:
                        await handle_message(message)
                    except FloodWaitError as fwe:
                        log_event(
                            logger,
                            logging.WARNING,
                            "past_flood_wait",
                            source_chat_id=src,
                            wait_seconds=fwe.seconds,
                            mode="iter_messages",
                        )
                        await asyncio.sleep(delay=fwe.seconds)
                    except Exception as err:
                        logger.exception(
                            {
                                "event": "past_iter_forward_failed",
                                "source_chat_id": src,
                                "error_type": type(err).__name__,
                                "error_message": str(err),
                            }
                        )

            try:
                await flush_batch()
                log_event(
                    logger,
                    logging.INFO,
                    "past_source_scan_completed",
                    source_chat_id=src,
                    scanned_count=scanned_count,
                    accepted_count=accepted_count,
                    last_forwarded_id=last_id,
                    duration_ms=round((time.perf_counter() - source_start) * 1000, 2),
                )
                if accepted_count == 0:
                    log_event(
                        logger,
                        logging.WARNING,
                        "past_no_eligible_messages",
                        source_chat_id=src,
                        offset=forward.offset,
                        end=forward.end,
                    )
            except Exception as err:
                logger.exception(
                    {
                        "event": "past_source_scan_failed",
                        "source_chat_id": src,
                        "error_type": type(err).__name__,
                        "error_message": str(err),
                        "duration_ms": round((time.perf_counter() - source_start) * 1000, 2),
                    }
                )

    log_event(
        logger,
        logging.INFO,
        "past_forward_job_completed",
        duration_ms=round((time.perf_counter() - job_start) * 1000, 2),
    )
