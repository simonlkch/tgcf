"""Utility functions to smoothen your life."""

import logging
import os
import platform
import re
import sys
from datetime import datetime
from typing import TYPE_CHECKING

from telethon.client import TelegramClient
from telethon.hints import EntityLike
from telethon.tl.custom.message import Message

from tgcf import __version__
from tgcf.config import CONFIG
from tgcf.fast_transfer import upload_file as fast_upload_file
from tgcf.plugin_models import STYLE_CODES

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TEMP_DIR = os.path.join(BASE_DIR, "temp")
FAST_SEND_FILE_PART_SIZE_KB = 1024


async def _send_file_fast_compatible(client: TelegramClient, *args, **kwargs):
    if args and len(args) >= 2:
        recipient = args[0]
        file = args[1]
        configured_part_size = int(getattr(CONFIG.live, "transfer_part_size_kb", FAST_SEND_FILE_PART_SIZE_KB) or FAST_SEND_FILE_PART_SIZE_KB)
        configured_part_size = max(64, min(configured_part_size, 4096))
        part_size_kb = kwargs.pop("part_size_kb", configured_part_size)
        configured_connections = int(getattr(CONFIG.live, "transfer_connection_count", 8) or 8)
        connection_count = max(1, min(configured_connections, 20))
        progress_callback = kwargs.pop("progress_callback", None)

        async def _prepare(item):
            if isinstance(item, (str, os.PathLike)) and os.path.exists(item):
                return await fast_upload_file(
                    client,
                    item,
                    progress_callback=progress_callback,
                    part_size_kb=part_size_kb,
                    connection_count=connection_count,
                )
            if getattr(item, "read", None):
                return await fast_upload_file(
                    client,
                    item,
                    progress_callback=progress_callback,
                    part_size_kb=part_size_kb,
                    connection_count=connection_count,
                )
            return item

        if isinstance(file, (list, tuple)):
            file = [await _prepare(item) for item in file]
        else:
            file = await _prepare(file)

        return await client.send_file(recipient, file, *args[2:], **kwargs)

    try:
        return await client.send_file(*args, **kwargs)
    except TypeError as err:
        if "part_size_kb" not in str(err):
            raise
        kwargs.pop("part_size_kb", None)
        return await client.send_file(*args, **kwargs)

if TYPE_CHECKING:
    from tgcf.plugins import TgcfMessage


def platform_info():
    nl = "\n"
    return f"""Running tgcf {__version__}\
    \nPython {sys.version.replace(nl,"")}\
    \nOS {os.name}\
    \nPlatform {platform.system()} {platform.release()}\
    \n{platform.architecture()} {platform.processor()}"""


def get_temp_dir() -> str:
    os.makedirs(TEMP_DIR, exist_ok=True)
    return TEMP_DIR


async def send_message(recipient: EntityLike, tm: "TgcfMessage") -> Message:
    """Forward or send a copy, depending on config."""
    client: TelegramClient = tm.client
    if CONFIG.show_forwarded_from:
        return await client.forward_messages(recipient, tm.message)
    if tm.new_file:
        message = await _send_file_fast_compatible(
            client,
            recipient,
            tm.new_file,
            caption=tm.text,
            reply_to=tm.reply_to,
            part_size_kb=FAST_SEND_FILE_PART_SIZE_KB,
        )
        return message
    tm.message.text = tm.text
    return await client.send_message(recipient, tm.message, reply_to=tm.reply_to)


def cleanup(*files: str) -> None:
    """Delete the file names passed as args."""
    for file in files:
        try:
            os.remove(file)
        except FileNotFoundError:
            logging.info(f"File {file} does not exist, so cant delete it.")


def stamp(file: str, user: str) -> str:
    """Stamp the filename with the datetime, and user info."""
    now = str(datetime.now())
    folder = os.path.dirname(file) or get_temp_dir()
    base_name = os.path.basename(file)
    outf = os.path.join(folder, safe_name(f"{user} {now} {base_name}"))
    try:
        os.rename(file, outf)
        return outf
    except Exception as err:
        logging.warning(f"Stamping file name failed for {file} to {outf}. \n {err}")


def safe_name(string: str) -> str:
    """Return safe file name.

    Certain characters in the file name can cause potential problems in rare scenarios.
    """
    return re.sub(pattern=r"[-!@#$%^&*()\s]", repl="_", string=string)


def match(pattern: str, string: str, regex: bool) -> bool:
    if regex:
        return bool(re.findall(pattern, string))
    return pattern in string


def replace(pattern: str, new: str, string: str, regex: bool) -> str:
    def fmt_repl(matched):
        style = new
        s = STYLE_CODES.get(style)
        return f"{s}{matched.group(0)}{s}"

    if regex:
        if new in STYLE_CODES:
            compliled_pattern = re.compile(pattern)
            return compliled_pattern.sub(repl=fmt_repl, string=string)
        return re.sub(pattern, new, string)
    else:
        return string.replace(pattern, new)


def clean_session_files():
    for item in os.listdir():
        if item.endswith(".session") or item.endswith(".session-journal"):
            os.remove(item)
