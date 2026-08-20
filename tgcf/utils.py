"""Utility functions to smoothen your life."""

import logging
import os
import platform
import re
import time
import sys
from datetime import datetime
from typing import TYPE_CHECKING, Any, Iterable, List, Optional

from telethon import functions, types, utils as telethon_utils
from telethon.client import TelegramClient
from telethon.hints import EntityLike
from telethon.tl.custom.message import Message
from tqdm import tqdm

from tgcf import __version__
from tgcf.config import CONFIG
from tgcf.fast_transfer import upload_file as fast_upload_file
from tgcf.logging_utils import log_event
from tgcf.plugin_models import FileType, STYLE_CODES

LOGGER = logging.getLogger(__name__)
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TEMP_DIR = os.path.join(BASE_DIR, "temp")
FAST_SEND_FILE_PART_SIZE_KB = 1024
TELEGRAM_SAFE_MAX_UPLOAD_PART_SIZE_KB = 512


async def _send_file_fast_compatible(client: TelegramClient, *args, **kwargs):
    if args and len(args) >= 2:
        recipient = args[0]
        file = args[1]
        configured_part_size = int(getattr(CONFIG.live, "transfer_part_size_kb", FAST_SEND_FILE_PART_SIZE_KB) or FAST_SEND_FILE_PART_SIZE_KB)
        configured_part_size = max(64, min(configured_part_size, TELEGRAM_SAFE_MAX_UPLOAD_PART_SIZE_KB))
        part_size_kb = kwargs.pop("part_size_kb", configured_part_size)
        configured_connections = int(getattr(CONFIG.live, "transfer_connection_count", 8) or 8)
        connection_count = max(1, min(configured_connections, 20))
        progress_callback = kwargs.pop("progress_callback", None)
        source_media_type = kwargs.pop("source_media_type", None)
        # Optional per-file thumb list for album uploads. When supplied, we
        # route the album through our own ``_send_album_with_thumbs`` helper
        # because Telethon's built-in ``_send_album`` discards ``thumb`` when
        # building each ``InputMediaUploadedDocument``.
        album_thumbs = kwargs.pop("album_thumbs", None)

        if source_media_type == FileType.PHOTO and not isinstance(file, (list, tuple)):
            return await client.send_file(recipient, file, *args[2:], **kwargs)

        # Normalize source_media_type: a single FileType becomes a per-file list
        # so mixed albums (e.g. photo + video) get correct per-file handling.
        is_album = isinstance(file, (list, tuple))
        if isinstance(source_media_type, (list, tuple)):
            media_types = list(source_media_type)
        elif is_album:
            media_types = [source_media_type] * len(file)
        else:
            media_types = [source_media_type]

        if not is_album and isinstance(file, (str, os.PathLike)) and os.path.exists(file):
            voice_note = source_media_type == FileType.AUDIO and False
            video_note = source_media_type == FileType.VIDEO_NOTE
            supports_streaming = source_media_type == FileType.VIDEO
            attributes, mime_type = telethon_utils.get_attributes(
                file,
                force_document=False,
                voice_note=voice_note,
                video_note=video_note,
                supports_streaming=supports_streaming,
            )
            kwargs = {
                **kwargs,
                "attributes": attributes,
                "mime_type": mime_type,
                "force_document": False,
                "voice_note": voice_note,
                "video_note": video_note,
                "supports_streaming": supports_streaming,
            }
        elif is_album:
            # Telethon's _send_album applies a single `supports_streaming` and
            # `force_document` value to every file, and does not accept per-file
            # `attributes` or `mime_type`. So we can only flip the album-level
            # `supports_streaming` flag: if any item is a video/animated file we
            # must set it, otherwise Telegram marks the video as a document that
            # must be downloaded before playback.
            _STREAMING_TYPES = (FileType.VIDEO, FileType.VIDEO_NOTE, FileType.GIF)
            has_streaming_item = any(mt in _STREAMING_TYPES for mt in media_types)
            kwargs = {
                **kwargs,
                "supports_streaming": has_streaming_item,
                "force_document": False,
            }
            # If the caller supplied at least one per-file thumb, we need to
            # route this album through our ``_send_album_with_thumbs`` helper
            # because Telethon's built-in ``_send_album`` drops ``thumb`` when
            # building each ``InputMediaUploadedDocument``. We delay the actual
            # call until the files have been uploaded so we can pass the
            # already-uploaded ``InputFile`` handles (and the thumb paths) to
            # the helper.
            if album_thumbs and any(album_thumbs):
                _route_album_through_helper = True
            else:
                _route_album_through_helper = False
        else:
            _route_album_through_helper = False
        def _is_payload_too_big_error(err: Exception) -> bool:
            text = str(err).lower()
            return "payload is too big" in text or "savebigfilepartrequest is too long" in text

        def _safe_upload_label(item) -> str:
            if isinstance(item, (str, os.PathLike)):
                label = os.path.basename(str(item)) or "file"
            else:
                label = os.path.basename(getattr(item, "name", "")) or "stream"
            label = str(label).replace("\n", " ").replace("\r", " ").strip()
            return (label or "file").encode("ascii", errors="backslashreplace").decode("ascii")

        def _build_upload_progress_callback(label: str):
            progress_bar = None
            uploaded_bytes = 0
            last_draw_bytes = 0
            started_at = time.time()
            last_draw_time = started_at
            min_update_bytes = 2 * 1024 * 1024
            min_update_seconds = 0.25

            async def _callback(current: int, total: int):
                nonlocal progress_bar, uploaded_bytes, last_draw_bytes, last_draw_time
                if progress_bar is None:
                    progress_bar = tqdm(
                        total=total or None,
                        initial=uploaded_bytes,
                        unit="B",
                        unit_scale=True,
                        unit_divisor=1024,
                        desc=f"upload {label}",
                        mininterval=0.25,
                        smoothing=0.1,
                        leave=True,
                    )
                if total and progress_bar.total != total:
                    progress_bar.total = total
                if current < uploaded_bytes:
                    current = uploaded_bytes
                delta = current - uploaded_bytes
                if delta > 0:
                    uploaded_bytes = current
                now = time.time()
                should_draw = (
                    (uploaded_bytes - last_draw_bytes) >= min_update_bytes
                    or (now - last_draw_time) >= min_update_seconds
                    or (total and uploaded_bytes >= total)
                )
                if not should_draw:
                    return

                draw_delta = uploaded_bytes - last_draw_bytes
                if draw_delta > 0:
                    progress_bar.update(draw_delta)

                elapsed = max(now - started_at, 1e-6)
                speed_mbps = (uploaded_bytes / elapsed) / (1024 * 1024)
                percent = round((uploaded_bytes / total) * 100, 2) if total else None
                progress_bar.set_postfix_str(f"{speed_mbps:.2f} MB/s", refresh=False)
                log_event(
                    LOGGER,
                    logging.INFO,
                    "transfer_progress",
                    direction="upload",
                    label=f"upload {label}",
                    recipient=recipient,
                    current_bytes=uploaded_bytes,
                    total_bytes=total,
                    percent=percent,
                    speed_mb_s=round(speed_mbps, 2),
                )
                last_draw_bytes = uploaded_bytes
                last_draw_time = now
                if progress_callback:
                    maybe_awaitable = progress_callback(current, total)
                    if hasattr(maybe_awaitable, "__await__"):
                        await maybe_awaitable

            def _close():
                if progress_bar is None:
                    return
                remaining = uploaded_bytes - last_draw_bytes
                if remaining > 0:
                    progress_bar.update(remaining)
                progress_bar.close()

            return _callback, _close

        async def _fast_upload_with_adaptive_part_size(item):
            current_part_size = max(64, min(int(part_size_kb), TELEGRAM_SAFE_MAX_UPLOAD_PART_SIZE_KB))
            while True:
                upload_progress_callback, close_upload_bar = _build_upload_progress_callback(_safe_upload_label(item))
                try:
                    return await fast_upload_file(
                        client,
                        item,
                        progress_callback=upload_progress_callback,
                        part_size_kb=current_part_size,
                        connection_count=connection_count,
                    )
                except Exception as err:
                    if not _is_payload_too_big_error(err):
                        raise
                    next_part_size = max(64, current_part_size // 2)
                    if next_part_size >= current_part_size:
                        raise
                    logging.warning(
                        "fast upload payload too big; retrying with smaller part_size_kb=%s (previous=%s)",
                        next_part_size,
                        current_part_size,
                    )
                    current_part_size = next_part_size
                finally:
                    close_upload_bar()

        async def _prepare(item):
            if isinstance(item, (str, os.PathLike)) and os.path.exists(item):
                return await _fast_upload_with_adaptive_part_size(item)
            if getattr(item, "read", None):
                return await _fast_upload_with_adaptive_part_size(item)
            return item

        if isinstance(file, (list, tuple)):
            file = [await _prepare(item) for item in file]
        else:
            file = await _prepare(file)

        if is_album and _route_album_through_helper:
            # Telethon's _send_album silently drops the per-file ``thumb``
            # argument, so any video we forward inside an album would land
            # on the destination without a thumbnail. Route through our own
            # helper that threads the thumb into _file_to_media instead.
            supports_streaming_for_helper = bool(kwargs.get("supports_streaming", False))
            captions = kwargs.get("caption") or []
            if not isinstance(captions, (list, tuple)):
                captions = [captions] * len(file)
            return await _send_album_with_thumbs(
                client,
                recipient,
                file,
                album_thumbs,
                captions=captions,
                reply_to=kwargs.get("reply_to"),
                supports_streaming=supports_streaming_for_helper,
                force_document=bool(kwargs.get("force_document", False)),
                silent=kwargs.get("silent"),
                schedule=kwargs.get("schedule"),
                background=kwargs.get("background"),
                clear_draft=kwargs.get("clear_draft"),
                progress_callback=progress_callback,
            )

        try:
            return await client.send_file(recipient, file, *args[2:], **kwargs)
        except TypeError as err:
            if "thumb" not in str(err):
                raise
            kwargs.pop("thumb", None)
            return await client.send_file(recipient, file, *args[2:], **kwargs)

    try:
        return await client.send_file(*args, **kwargs)
    except TypeError as err:
        if "part_size_kb" not in str(err):
            raise
        kwargs.pop("part_size_kb", None)
        return await client.send_file(*args, **kwargs)


async def _send_album_with_thumbs(
    client: TelegramClient,
    entity: EntityLike,
    files: List[Any],
    thumbs: List[Optional[str]],
    *,
    captions: Iterable[str] = (),
    reply_to: Optional[int] = None,
    supports_streaming: bool = False,
    force_document: bool = False,
    silent: Optional[bool] = None,
    schedule: Optional[Any] = None,
    background: Optional[bool] = None,
    clear_draft: Optional[bool] = None,
    progress_callback: Optional[Any] = None,
) -> List[Any]:
    """Album upload that supports per-file thumbnails.

    Telethon's internal ``_send_album`` deliberately drops the ``thumb``
    argument when it builds each ``InputMediaUploadedDocument``, so any video
    inside an album ends up uploaded without a thumbnail and the recipient
    sees a blank tile. This helper re-implements the album upload pipeline
    but threads a per-file ``thumb`` path into ``_file_to_media`` so the
    generated ``InputMediaUploadedDocument`` carries the thumbnail the
    caller asked for.

    The implementation mirrors ``TelegramClient._send_album`` so behavior
    (Photo-vs-Document media resolution, UploadMedia re-encoding step,
    SendMultiMedia request) is identical to what Telethon would do — only
    the missing thumb support is added.
    """

    if not files:
        return []

    entity = await client.get_input_entity(entity)
    captions_list = list(captions)
    if len(captions_list) < len(files):
        captions_list = captions_list + [""] * (len(files) - len(captions_list))

    media_list: List[types.InputSingleMedia] = []
    for index, file in enumerate(files):
        thumb_path = thumbs[index] if index < len(thumbs) else None
        # ``_file_to_media`` will:
        #   * upload ``file`` (or reuse the ``InputFile`` handle we pass in)
        #   * when ``thumb`` is set, upload it as a separate ``InputFile`` and
        #     attach it to the resulting ``InputMediaUploadedDocument.thumb``
        #   * respect ``supports_streaming`` so videos remain streamable
        file_handle, fm, _ = await client._file_to_media(
            file,
            supports_streaming=supports_streaming,
            force_document=force_document,
            thumb=thumb_path,
            progress_callback=None,
            nosound_video=True,
        )
        if fm is None:
            raise ValueError(f"Failed to convert album item #{index} to media")

        if isinstance(fm, (types.InputMediaUploadedPhoto, types.InputMediaPhotoExternal)):
            re_encoded = await client(functions.messages.UploadMediaRequest(entity, media=fm))
            fm = telethon_utils.get_input_media(re_encoded.photo)
        elif isinstance(fm, (types.InputMediaUploadedDocument, types.InputMediaDocumentExternal)):
            re_encoded = await client(functions.messages.UploadMediaRequest(entity, media=fm))
            fm = telethon_utils.get_input_media(
                re_encoded.document, supports_streaming=supports_streaming
            )

        if not isinstance(fm, (types.InputMediaPhoto, types.InputMediaDocument)):
            # Photo must be re-encoded into an InputMediaPhoto; documents stay
            # as InputMediaDocument. Anything else cannot be placed inside an
            # album (Telegram only accepts those two kinds).
            raise TypeError(
                f"Album item #{index} resolved to unsupported media type {type(fm).__name__}"
            )

        media_list.append(
            types.InputSingleMedia(
                media=fm,
                message=captions_list[index] or "",
                entities=None,
                # random_id is autogenerated by Telethon
            )
        )
        if progress_callback is not None:
            try:
                progress_callback(index + 1, len(files))
            except Exception:
                logging.exception("album progress callback raised; continuing")

    reply_to_input = None
    if reply_to is not None:
        reply_to_input = types.InputReplyToMessage(reply_to)

    # ``SendMultiMediaRequest`` takes ``peer`` (not ``entity``) per Telethon's
    # TL schema. The earlier code used ``entity=...`` which raised
    # ``TypeError: SendMultiMediaRequest.__init__() got an unexpected keyword
    # argument 'entity'`` and the entire album upload failed.
    request = functions.messages.SendMultiMediaRequest(
        peer=entity,
        reply_to=reply_to_input,
        multi_media=media_list,
        silent=silent,
        schedule_date=schedule,
        clear_draft=clear_draft,
        background=background,
    )
    result = await client(request)
    random_ids = [m.random_id for m in media_list]
    return client._get_response_message(random_ids, result, entity)


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
    if tm.file_type == FileType.PHOTO and tm.new_file:
        return await client.send_file(recipient, tm.new_file, caption=tm.text, reply_to=tm.reply_to)
    if tm.new_file:
        thumb_file = getattr(tm, "thumb_file", None)
        ensure_thumb = getattr(tm, "ensure_thumb_file", None)
        if callable(ensure_thumb):
            thumb_file = await ensure_thumb()
        message = await _send_file_fast_compatible(
            client,
            recipient,
            tm.new_file,
            caption=tm.text,
            reply_to=tm.reply_to,
            part_size_kb=FAST_SEND_FILE_PART_SIZE_KB,
            source_media_type=tm.file_type,
            thumb=thumb_file,
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
        except PermissionError as err:
            logging.warning(f"File {file} is locked and could not be deleted yet. {err}")
        except OSError as err:
            logging.warning(f"Failed to delete file {file}. {err}")


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
