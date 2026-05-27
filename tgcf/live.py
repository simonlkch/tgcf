"""The module responsible for operating tgcf in live mode."""

import asyncio
import logging
import sys
from typing import Dict, List, Tuple, Union

from telethon import TelegramClient, events, functions, types
from telethon.tl.custom.message import Message

from tgcf import config, const
from tgcf import storage as st
from tgcf.bot import get_events
from tgcf.config import CONFIG, get_SESSION
from tgcf.forwarding import forward_source_batch
from tgcf.logging_utils import log_event
from tgcf.plugins import apply_plugins, load_async_plugins
from tgcf.utils import clean_session_files


album_buffers: Dict[Tuple[int, int], List[Message]] = {}
album_tasks: Dict[Tuple[int, int], asyncio.Task] = {}
LOGGER = logging.getLogger(__name__)


async def _flush_album_later(album_uid: Tuple[int, int]) -> None:
    try:
        await asyncio.sleep(CONFIG.live.album_debounce_ms / 1000)
        messages = album_buffers.pop(album_uid, [])
        album_tasks.pop(album_uid, None)
        if not messages:
            return
        await forward_source_batch(messages, config.from_to.get(album_uid[0], []))
    except asyncio.CancelledError:
        return
    except Exception as err:
        LOGGER.exception(
            {
                "event": "album_flush_failed",
                "album_uid": str(album_uid),
                "error_type": type(err).__name__,
                "error_message": str(err),
            }
        )


async def _queue_album_message(message: Message) -> None:
    album_uid = st.album_key(message.chat_id, getattr(message, "grouped_id", None))
    if album_uid is None:
        return

    album_buffers.setdefault(album_uid, []).append(message)
    album_buffers[album_uid].sort(key=lambda item: item.id)

    task = album_tasks.get(album_uid)
    if task and not task.done():
        task.cancel()

    album_tasks[album_uid] = asyncio.create_task(_flush_album_later(album_uid))


async def _forward_single_message(message: Message) -> None:
    destinations = config.from_to.get(message.chat_id)
    if not destinations:
        return
    await forward_source_batch([message], destinations)


async def new_message_handler(event: Union[Message, events.NewMessage]) -> None:
    """Process new incoming messages."""
    chat_id = event.chat_id

    if chat_id not in config.from_to:
        return
    log_event(
        LOGGER,
        logging.INFO,
        "live_new_message_received",
        source_chat_id=chat_id,
        message_id=getattr(event.message, "id", None),
        grouped_id=getattr(event.message, "grouped_id", None),
    )
    message = event.message

    length = len(st.stored)
    exceeding = length - const.KEEP_LAST_MANY

    if exceeding > 0:
        for key in st.stored:
            del st.stored[key]
            break

    if getattr(message, "grouped_id", None) is None:
        await _forward_single_message(message)
        return

    await _queue_album_message(message)


async def edited_message_handler(event) -> None:
    """Handle message edits."""
    message = event.message

    chat_id = event.chat_id

    if chat_id not in config.from_to:
        return

    log_event(
        LOGGER,
        logging.INFO,
        "live_message_edited",
        source_chat_id=chat_id,
        message_id=getattr(message, "id", None),
    )

    event_uid = st.EventUid(event)
    fwded_msgs = st.stored.get(event_uid)

    if fwded_msgs:
        tm = await apply_plugins(message)
        if not tm:
            return
        try:
            for _, msg in fwded_msgs.items():
                if config.CONFIG.live.delete_on_edit == message.text:
                    await msg.delete()
                    await message.delete()
                else:
                    await msg.edit(tm.text)
        finally:
            tm.clear()
        return

    await _forward_single_message(message)


async def deleted_message_handler(event):
    """Handle message deletes."""
    chat_id = event.chat_id
    if chat_id not in config.from_to:
        return

    log_event(
        LOGGER,
        logging.INFO,
        "live_message_deleted",
        source_chat_id=chat_id,
    )

    event_uid = st.EventUid(event)
    fwded_msgs = st.stored.get(event_uid)
    if fwded_msgs:
        for _, msg in fwded_msgs.items():
            await msg.delete()
        return


ALL_EVENTS = {
    "new": (new_message_handler, events.NewMessage()),
    "edited": (edited_message_handler, events.MessageEdited()),
    "deleted": (deleted_message_handler, events.MessageDeleted()),
}


async def start_sync() -> None:
    """Start tgcf live sync."""
    # clear past session files
    clean_session_files()

    # load async plugins defined in plugin_models
    await load_async_plugins()

    SESSION = get_SESSION()
    client = TelegramClient(
        SESSION,
        CONFIG.login.API_ID,
        CONFIG.login.API_HASH,
        sequential_updates=CONFIG.live.sequential_updates,
    )
    if CONFIG.login.user_type == 0:
        if CONFIG.login.BOT_TOKEN == "":
            log_event(
                LOGGER,
                logging.WARNING,
                "bot_token_missing",
                outcome="aborted",
            )
            sys.exit()
        await client.start(bot_token=CONFIG.login.BOT_TOKEN)
    else:
        await client.start()
    config.is_bot = await client.is_bot()
    log_event(LOGGER, logging.INFO, "live_client_started", is_bot=config.is_bot)
    command_events = get_events()

    await config.load_admins(client)

    ALL_EVENTS.update(command_events)

    for key, val in ALL_EVENTS.items():
        if config.CONFIG.live.delete_sync is False and key == "deleted":
            continue
        client.add_event_handler(*val)
        log_event(LOGGER, logging.INFO, "event_handler_registered", handler=key)

    if config.is_bot and const.REGISTER_COMMANDS:
        await client(
            functions.bots.SetBotCommandsRequest(
                scope=types.BotCommandScopeDefault(),
                lang_code="en",
                commands=[
                    types.BotCommand(command=key, description=value)
                    for key, value in const.COMMANDS.items()
                ],
            )
        )
    config.from_to = await config.load_from_to(client, config.CONFIG.forwards)
    await client.run_until_disconnected()
