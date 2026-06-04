"""Subpackage of tgcf: plugins.

Contains all the first-party tgcf plugins.
"""


import inspect
import json
import logging
import mimetypes
import os
import re
import time
from enum import Enum
from importlib import import_module
from typing import Any, Dict

from hachoir.metadata import extractMetadata
from hachoir.parser import createParser
from telethon.tl.custom.message import Message
from tqdm import tqdm
from tgcf.config import CONFIG
from tgcf.fast_transfer import download_file
from tgcf.plugin_models import FileType, ASYNC_PLUGIN_IDS
from tgcf.utils import cleanup, get_temp_dir, safe_name


TRANSFER_PART_SIZE_KB = 1024
PROGRESS_MIN_UPDATE_SECONDS = 0.25
PROGRESS_MIN_UPDATE_BYTES = 1024 * 1024

PLUGINS = CONFIG.plugins


class TgcfMessage:
    def __init__(self, message: Message) -> None:
        self.message = message
        self.text = self.message.text
        self.raw_text = self.message.raw_text
        self.sender_id = self.message.sender_id
        self.file_type = self.guess_file_type()
        self.new_file = None
        self.thumb_file = None
        self.cleanup = False
        self.reply_to = None
        self.client = self.message.client

    def _has_valid_video_duration(self, file_path: str) -> bool:
        try:
            parser = createParser(file_path)
        except Exception:
            return False
        if not parser:
            return False
        try:
            metadata = extractMetadata(parser)
        except Exception:
            return False
        finally:
            try:
                parser.close()
            except Exception:
                pass

        if not metadata or not metadata.has("duration"):
            return False

        duration = metadata.get("duration")
        if hasattr(duration, "total_seconds"):
            return duration.total_seconds() > 0
        if isinstance(duration, (int, float)):
            return duration > 0
        return False

    def _source_video_has_duration(self) -> bool:
        document = getattr(self.message, "document", None)
        if document is None:
            return False
        for attr in getattr(document, "attributes", []):
            duration = getattr(attr, "duration", None)
            if isinstance(duration, (int, float)) and duration > 0:
                return True
        return False

    def _safe_log_path(self, file_path: str) -> str:
        raw = os.path.basename(file_path or "")
        safe = safe_name(raw)
        return safe.encode("ascii", errors="backslashreplace").decode("ascii")

    def _suggest_file_name(self) -> str:
        file_meta = getattr(self.message, "file", None)
        file_name = getattr(file_meta, "name", None)
        if file_name:
            return file_name

        mime_type = getattr(file_meta, "mime_type", None) or ""
        if self.file_type == FileType.PHOTO:
            return f"msg_{getattr(self.message, 'id', 'unknown')}.jpg"
        if self.file_type in (FileType.VIDEO, FileType.VIDEO_NOTE):
            ext = mimetypes.guess_extension(mime_type) or ".mp4"
            if ext == ".jpe":
                ext = ".jpg"
            return f"msg_{getattr(self.message, 'id', 'unknown')}{ext}"
        if self.file_type == FileType.AUDIO:
            ext = mimetypes.guess_extension(mime_type) or ".mp3"
            if ext == ".jpe":
                ext = ".jpg"
            return f"msg_{getattr(self.message, 'id', 'unknown')}{ext}"
        if self.file_type == FileType.GIF:
            return f"msg_{getattr(self.message, 'id', 'unknown')}.gif"
        if self.file_type == FileType.STICKER:
            return f"msg_{getattr(self.message, 'id', 'unknown')}.webp"
        return f"msg_{getattr(self.message, 'id', 'unknown')}.bin"

    def _suggest_thumb_name(self) -> str:
        return f"msg_{getattr(self.message, 'id', 'unknown')}_thumb.jpg"

    async def _download_source_thumb(self, temp_dir: str) -> str | None:
        if self.file_type not in (FileType.VIDEO, FileType.VIDEO_NOTE, FileType.GIF):
            return None

        document = getattr(self.message, "document", None)
        thumbs = getattr(document, "thumbs", None)
        if not document or not thumbs:
            return None

        thumb_path = os.path.join(temp_dir, safe_name(self._suggest_thumb_name()))
        downloaded_thumb = await self.client.download_media(document, file=thumb_path, thumb=-1)
        if downloaded_thumb and os.path.exists(downloaded_thumb):
            logging.info("Prepared temp media thumb=%s", self._safe_log_path(downloaded_thumb))
            return downloaded_thumb
        return None

    async def ensure_thumb_file(self) -> str | None:
        if self.file_type not in (FileType.VIDEO, FileType.VIDEO_NOTE, FileType.GIF):
            return None

        if self.thumb_file and os.path.exists(self.thumb_file):
            return self.thumb_file

        self.thumb_file = await self._download_source_thumb(get_temp_dir())
        return self.thumb_file

    def _is_valid_media_file(self, file_path: str, expected_size: int) -> bool:
        if not file_path or not os.path.exists(file_path):
            return False

        size = os.path.getsize(file_path)
        if size <= 0:
            return False

        if expected_size is not None and size != expected_size:
            return False

        if self.file_type in (FileType.VIDEO, FileType.VIDEO_NOTE):
            if not self._has_valid_video_duration(file_path):
                if not self._source_video_has_duration():
                    logging.warning(
                        "Invalid video duration for file=%s",
                        self._safe_log_path(file_path),
                    )
                    return False
                logging.info(
                    "Video parser check skipped; source has duration, file=%s",
                    self._safe_log_path(file_path),
                )

        return True

    async def get_file(self) -> str:
        """Downloads the file in the message and returns the path where its saved."""
        if self.file_type == FileType.NOFILE:
            raise FileNotFoundError("No file exists in this message.")
        downloaded = None
        temp_dir = get_temp_dir()
        configured_part_size = int(getattr(CONFIG.live, "transfer_part_size_kb", TRANSFER_PART_SIZE_KB) or TRANSFER_PART_SIZE_KB)
        part_size_kb = max(64, min(configured_part_size, 4096))
        configured_connections = int(getattr(CONFIG.live, "transfer_connection_count", 8) or 8)
        connection_count = max(1, min(configured_connections, 20))
        file_meta = getattr(self.message, "file", None)
        expected_size = getattr(file_meta, "size", None)
        file_name = self._suggest_file_name()
        cache_name = f"{getattr(self.message, 'id', 'unknown')}_{safe_name(file_name)}"
        target_path = os.path.join(temp_dir, cache_name)
        part_path = target_path + ".part"
        meta_path = target_path + ".meta"
        legacy_raw_base_name = os.path.basename(file_name)
        legacy_safe_base_name = safe_name(file_name)
        legacy_part_path = os.path.join(temp_dir, f"{legacy_safe_base_name}.part")
        legacy_meta_path = os.path.join(temp_dir, f"{legacy_safe_base_name}.meta")

        def _legacy_resume_paths() -> tuple[str, str]:
            """Find older resume files that were stored without the message-id prefix."""

            candidates = []

            def _matches_legacy_base(stem: str) -> bool:
                if not stem:
                    return False
                normalized_stem = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", stem.lower())
                raw_norm = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", legacy_raw_base_name.lower())
                safe_norm = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", legacy_safe_base_name.lower())
                return (
                    stem == legacy_raw_base_name
                    or stem == legacy_safe_base_name
                    or stem.endswith(f"_{legacy_raw_base_name}")
                    or stem.endswith(f"_{legacy_safe_base_name}")
                    or normalized_stem == raw_norm
                    or normalized_stem == safe_norm
                    or normalized_stem.endswith(raw_norm)
                    or normalized_stem.endswith(safe_norm)
                )

            def _push_candidate(candidate_part: str, candidate_meta: str) -> None:
                if os.path.exists(candidate_part):
                    candidates.append((candidate_part, candidate_meta))

            _push_candidate(part_path, meta_path)
            _push_candidate(legacy_part_path, legacy_meta_path)

            for entry in os.scandir(temp_dir):
                if not entry.is_file() or not entry.name.endswith(".part"):
                    continue
                stem = entry.name[:-5]
                if _matches_legacy_base(stem):
                    _push_candidate(entry.path, os.path.join(temp_dir, f"{stem}.meta"))

            if not candidates:
                return part_path, meta_path

            chosen_part, chosen_meta = max(candidates, key=lambda item: os.path.getmtime(item[0]))
            return chosen_part, chosen_meta

        def _normalize_reused_blob(file_path: str) -> str:
            """Move a reused blob to the new prefixed cache path if needed."""

            if not file_path:
                return file_path
            abs_path = os.path.abspath(file_path)
            target_abs = os.path.abspath(target_path)
            if abs_path == target_abs:
                return target_path
            try:
                os.replace(abs_path, target_abs)
                return target_path
            except OSError:
                return file_path

        def _write_resume_meta(offset: int) -> None:
            payload = {
                "offset": offset,
                "expected_size": expected_size,
                "updated_at": int(time.time()),
                "file_name": file_name,
            }
            try:
                with open(meta_path, "w", encoding="utf-8") as fp:
                    json.dump(payload, fp)
            except OSError:
                pass

        def _read_resume_offset() -> int:
            resume_part_path, resume_meta_path = _legacy_resume_paths()
            if resume_part_path != part_path and os.path.exists(resume_part_path):
                try:
                    os.replace(resume_part_path, part_path)
                    if os.path.exists(resume_meta_path):
                        os.replace(resume_meta_path, meta_path)
                except OSError:
                    pass

            offset = os.path.getsize(part_path) if os.path.exists(part_path) else 0
            if offset <= 0:
                return 0
            if expected_size is not None and offset >= expected_size:
                return 0

            if not os.path.exists(meta_path):
                return offset

            try:
                with open(meta_path, "r", encoding="utf-8") as fp:
                    payload = json.load(fp)
            except Exception:
                return offset

            meta_expected = payload.get("expected_size")
            if expected_size is not None and meta_expected not in (None, expected_size):
                return 0

            meta_offset = payload.get("offset")
            if isinstance(meta_offset, int) and 0 < meta_offset <= offset:
                return meta_offset

            return offset

        def _normalize_name(value: str) -> str:
            value = re.sub(r"\(\d+\)(?=\.[^.]+$)", "", value)
            return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", value.lower())

        expected_norm = _normalize_name(file_name)
        expected_raw_norm = _normalize_name(legacy_raw_base_name)
        expected_safe_norm = _normalize_name(legacy_safe_base_name)

        # Reuse any valid pre-existing blob in temp, including files downloaded by older naming schemes.
        candidates = []
        for entry in os.scandir(temp_dir):
            if not entry.is_file():
                continue
            candidate_name = entry.name
            if expected_size is not None and os.path.getsize(entry.path) != expected_size:
                continue
            candidate_norm = _normalize_name(candidate_name)
            if expected_norm and expected_norm not in candidate_norm:
                if expected_raw_norm not in candidate_norm and expected_safe_norm not in candidate_norm:
                    continue
            if self._is_valid_media_file(entry.path, expected_size):
                candidates.append(entry.path)

        # If strict name matching misses legacy blobs, reuse any valid same-size file.
        if not candidates and expected_size is not None:
            for entry in os.scandir(temp_dir):
                if not entry.is_file():
                    continue
                if os.path.getsize(entry.path) != expected_size:
                    continue
                if self._is_valid_media_file(entry.path, expected_size):
                    candidates.append(entry.path)

        if candidates:
            chosen = max(candidates, key=os.path.getmtime)
            logging.info("Reusing existing temp media=%s", self._safe_log_path(chosen))
            self.new_file = _normalize_reused_blob(chosen)
            await self.ensure_thumb_file()
            self.cleanup = True
            return self.new_file

        if os.path.exists(target_path):
            if self._is_valid_media_file(target_path, expected_size):
                size = os.path.getsize(target_path)
                logging.info(
                    "Using cached media file=%s size=%s bytes",
                    self._safe_log_path(target_path),
                    size,
                )
                self.new_file = target_path
                await self.ensure_thumb_file()
                self.cleanup = True
                return self.new_file

            # Keep partial files for resume instead of always restarting from zero.
            try:
                existing_size = os.path.getsize(target_path)
            except OSError:
                existing_size = 0
            if (
                expected_size is not None
                and existing_size > 0
                and existing_size < expected_size
            ):
                try:
                    os.replace(target_path, part_path)
                    _write_resume_meta(existing_size)
                    logging.info(
                        "Moved partial cache to resume buffer file=%s bytes=%s",
                        self._safe_log_path(part_path),
                        existing_size,
                    )
                except OSError:
                    pass

            logging.warning(
                "Cached media is invalid, re-downloading: %s",
                self._safe_log_path(target_path),
            )
            try:
                os.remove(target_path)
            except OSError:
                pass

        for attempt in range(1, 4):
            progress_bar = None
            downloaded_bytes = 0
            last_draw_bytes = 0
            start_time = time.time()
            last_draw_time = start_time
            downloaded = None

            def progress_callback(current: int, total: int):
                nonlocal downloaded_bytes, last_draw_bytes, start_time, last_draw_time, progress_bar
                if progress_bar is None:
                    progress_bar = tqdm(
                        total=total or None,
                        initial=downloaded_bytes,
                        unit="B",
                        unit_scale=True,
                        unit_divisor=1024,
                        desc=f"download msg {getattr(self.message, 'id', None)}",
                        mininterval=PROGRESS_MIN_UPDATE_SECONDS,
                        smoothing=0.1,
                        leave=True,
                    )
                if total and progress_bar.total != total:
                    progress_bar.total = total
                delta = current - downloaded_bytes
                if delta > 0:
                    downloaded_bytes = current

                now = time.time()
                should_draw = (
                    (downloaded_bytes - last_draw_bytes) >= PROGRESS_MIN_UPDATE_BYTES
                    or (now - last_draw_time) >= PROGRESS_MIN_UPDATE_SECONDS
                    or (total and downloaded_bytes >= total)
                )
                if not should_draw:
                    return

                draw_delta = downloaded_bytes - last_draw_bytes
                if draw_delta > 0:
                    progress_bar.update(draw_delta)

                elapsed = max(now - start_time, 1e-6)
                speed_bps = downloaded_bytes / elapsed
                speed_mbps = speed_bps / (1024 * 1024)
                if total and speed_bps > 0:
                    eta_seconds = max(total - downloaded_bytes, 0) / speed_bps
                    progress_bar.set_postfix_str(
                        f"{speed_mbps:.2f} MB/s | ETA {eta_seconds:.1f}s",
                        refresh=False,
                    )
                else:
                    progress_bar.set_postfix_str(
                        f"{speed_mbps:.2f} MB/s",
                        refresh=False,
                    )

                last_draw_bytes = downloaded_bytes
                last_draw_time = now

            logging.info(
                "Downloading media for message_id=%s sender_id=%s into temp directory (attempt %s)",
                getattr(self.message, "id", None),
                self.sender_id,
                attempt,
            )
            try:
                document = getattr(self.message, "document", None)
                if document is not None:
                    resume_from = _read_resume_offset()
                    if expected_size is not None and resume_from >= expected_size:
                        resume_from = 0

                    mode = "ab" if resume_from > 0 else "wb"
                    if resume_from > 0:
                        logging.info(
                            "Resuming media download from offset=%s file=%s",
                            resume_from,
                            self._safe_log_path(part_path),
                        )

                    with open(part_path, mode) as fp:
                        current = resume_from
                        downloaded_bytes = resume_from
                        last_draw_bytes = resume_from
                        start_time = time.time()
                        last_draw_time = start_time

                        await download_file(
                            self.client,
                            document,
                            fp,
                            progress_callback=progress_callback,
                            file_size=expected_size,
                            part_size_kb=part_size_kb,
                            connection_count=connection_count,
                            offset=resume_from,
                        )

                    _write_resume_meta(os.path.getsize(part_path))
                    if expected_size is not None and os.path.getsize(part_path) != expected_size:
                        raise IOError(
                            "Partial download size mismatch: "
                            f"{os.path.getsize(part_path)} != {expected_size}"
                        )

                    os.replace(part_path, target_path)
                    try:
                        os.remove(meta_path)
                    except OSError:
                        pass
                    downloaded = target_path
                else:
                    downloaded = await self.message.download_media(
                        temp_dir, progress_callback=progress_callback
                    )
                    if downloaded and os.path.exists(downloaded) and downloaded != target_path:
                        os.replace(downloaded, target_path)
                        downloaded = target_path
            except Exception as err:
                err_name = err.__class__.__name__
                err_text = str(err).upper()
                if err_name == "FileReferenceExpiredError" or "FILE_REFERENCE_EXPIRED" in err_text:
                    logging.warning(
                        "File reference expired for message_id=%s; refetching and retrying attempt=%s",
                        getattr(self.message, "id", None),
                        attempt,
                    )
                    try:
                        refreshed = await self.client.get_messages(
                            self.message.chat_id,
                            ids=self.message.id,
                        )
                        if refreshed:
                            self.message = refreshed
                            self.client = self.message.client
                    except Exception:
                        logging.exception(
                            "Failed to refresh message reference for message_id=%s",
                            getattr(self.message, "id", None),
                        )
                    continue
                if attempt < 3:
                    logging.warning(
                        "Download attempt failed for message_id=%s attempt=%s error=%s",
                        getattr(self.message, "id", None),
                        attempt,
                        err,
                    )
                    continue
                raise
            finally:
                if progress_bar is not None:
                    remaining = downloaded_bytes - last_draw_bytes
                    if remaining > 0:
                        progress_bar.update(remaining)
                    progress_bar.close()
            if downloaded and os.path.exists(downloaded):
                size = os.path.getsize(downloaded)
                logging.info(
                    "Downloaded media path=%s size=%s bytes",
                    self._safe_log_path(downloaded),
                    size,
                )
                if self._is_valid_media_file(downloaded, expected_size):
                    break
                logging.warning(
                    "Downloaded media failed validation, retrying: %s",
                    self._safe_log_path(downloaded),
                )
                try:
                    os.remove(downloaded)
                except OSError:
                    pass
        if not self._is_valid_media_file(downloaded, expected_size):
            raise FileNotFoundError("Failed to download a valid media file.")
        self.new_file = downloaded
        logging.info("Prepared temp media file=%s", self._safe_log_path(self.new_file))
        self.thumb_file = await self.ensure_thumb_file()
        # Remove temp media artifacts after upload to avoid stale temp growth.
        self.cleanup = True
        return self.new_file

    def guess_file_type(self) -> FileType:
        for i in FileType:
            if i == FileType.NOFILE:
                return i
            obj = getattr(self.message, i.value)
            if obj:
                return i

    def clear(self) -> None:
        if self.new_file and self.cleanup:
            cleanup(self.new_file)
            self.new_file = None
        if self.thumb_file and self.cleanup:
            cleanup(self.thumb_file)
            self.thumb_file = None


class TgcfPlugin:
    id_ = "plugin"

    def __init__(self, data: Dict[str, Any]) -> None:  # TODO data type has changed
        self.data = data

    async def __ainit__(self) -> None:
        """Asynchronous initialization here."""

    def modify(self, tm: TgcfMessage) -> TgcfMessage:
        """Modify the message here."""
        return tm


def load_plugins() -> Dict[str, TgcfPlugin]:
    """Load the plugins specified in config."""
    _plugins = {}
    for item in PLUGINS:
        plugin_id = item[0]
        if item[1].check == False:
            continue

        plugin_class_name = f"Tgcf{plugin_id.title()}"

        try:  # try to load first party plugin
            plugin_module = import_module("tgcf.plugins." + plugin_id)
        except ModuleNotFoundError:
            logging.error(
                f"{plugin_id} is not a first party plugin. Third party plugins are not supported."
            )
        else:
            logging.info(f"First party plugin {plugin_id} loaded!")

        try:
            plugin_class = getattr(plugin_module, plugin_class_name)
            if not issubclass(plugin_class, TgcfPlugin):
                logging.error(
                    f"Plugin class {plugin_class_name} does not inherit TgcfPlugin"
                )
                continue
            plugin: TgcfPlugin = plugin_class(item[1])
            if not plugin.id_ == plugin_id:
                logging.error(f"Plugin id for {plugin_id} does not match expected id.")
                continue
        except AttributeError:
            logging.error(f"Found plugin {plugin_id}, but plugin class not found.")
        else:
            logging.info(f"Loaded plugin {plugin_id}")
            _plugins.update({plugin.id_: plugin})
    return _plugins


async def load_async_plugins() -> None:
    """Load async plugins specified plugin_models."""
    if plugins:
        for id in ASYNC_PLUGIN_IDS:
            if id in plugins:
                await plugins[id].__ainit__()
                logging.info(f"Plugin {id} asynchronously loaded")


async def apply_plugins(message: Message) -> TgcfMessage:
    """Apply all loaded plugins to a message."""
    tm = TgcfMessage(message)

    for _id, plugin in plugins.items():
        try:
            if inspect.iscoroutinefunction(plugin.modify):
                ntm = await plugin.modify(tm)
            else:
                ntm = plugin.modify(tm)
        except Exception as err:
            logging.error(f"Failed to apply plugin {_id}. \n {err} ")
        else:
            logging.info(f"Applied plugin {_id}")
            if not ntm:
                tm.clear()
                return None
    return tm


plugins = load_plugins()
