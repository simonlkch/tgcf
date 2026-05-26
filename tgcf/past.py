"""The module for running tgcf in past mode.

- past mode can only operate with a user account.
- past mode deals with all existing messages.
"""

import asyncio
import logging

from telethon import TelegramClient
from telethon.errors.rpcerrorlist import FloodWaitError
from telethon.tl.custom.message import Message
from telethon.tl.patched import MessageService

from tgcf import config
from tgcf.config import CONFIG, get_SESSION, write_config
from tgcf.forwarding import build_forward_batches, forward_source_batch
from tgcf.plugins import load_async_plugins
from tgcf.utils import clean_session_files


async def forward_job() -> None:
    """Forward all existing messages in the concerned chats."""
    logging.info("past forward_job started")
    clean_session_files()

    # load async plugins defined in plugin_models
    await load_async_plugins()

    if CONFIG.login.user_type != 1:
        logging.warning(
            "You cannot use bot account for tgcf past mode. Telegram does not allow bots to access chat history."
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
            logging.info(
                "Forwarding messages from %s to %s (offset=%s, end=%s)",
                src,
                dest,
                forward.offset,
                forward.end,
            )
            if exact_id_mode:
                logging.info(
                    "Exact-id mode enabled for source=%s id=%s",
                    src,
                    forward.offset,
                )

            current_batch = []

            async def flush_batch():
                nonlocal last_id, current_batch
                if not current_batch:
                    return
                for batch in build_forward_batches(current_batch):
                    await forward_source_batch(batch.messages, dest)
                    last_id = batch.messages[-1].id
                    logging.info(f"forwarding message with id = {last_id}")
                    forward.offset = last_id
                    write_config(CONFIG, persist=False)
                    await asyncio.sleep(CONFIG.past.delay)
                    logging.info(f"slept for {CONFIG.past.delay} seconds")
                current_batch = []

            async def handle_message(message: Message):
                nonlocal scanned_count, accepted_count, previous_grouped_id
                scanned_count += 1

                if scanned_count % 200 == 0:
                    logging.info(
                        "Scanning progress for %s: scanned=%s accepted=%s last_forwarded_id=%s",
                        src,
                        scanned_count,
                        accepted_count,
                        last_id,
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
                    logging.info(f"Sleeping for {fwe}")
                    await asyncio.sleep(delay=fwe.seconds)
                except Exception as err:
                    logging.exception(err)
            else:
                async for message in client.iter_messages(
                    src, reverse=True, offset_id=forward.offset
                ):
                    message: Message
                    try:
                        await handle_message(message)
                    except FloodWaitError as fwe:
                        logging.info(f"Sleeping for {fwe}")
                        await asyncio.sleep(delay=fwe.seconds)
                    except Exception as err:
                        logging.exception(err)

            try:
                await flush_batch()
                logging.info(
                    "Completed source %s: scanned=%s accepted=%s last_forwarded_id=%s",
                    src,
                    scanned_count,
                    accepted_count,
                    last_id,
                )
                if accepted_count == 0:
                    logging.warning(
                        "No eligible messages found for source=%s with offset=%s end=%s",
                        src,
                        forward.offset,
                        forward.end,
                    )
            except Exception as err:
                logging.exception(err)
