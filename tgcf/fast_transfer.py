"""Parallel Telegram file transfers using raw MTProto senders."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import math
import os
from contextlib import suppress
from typing import Any, AsyncGenerator, BinaryIO, Callable, Iterable, List, Optional, Tuple, Union

from telethon import TelegramClient, helpers, utils as telethon_utils
from telethon.crypto import AuthKey
from telethon.network.mtprotosender import MTProtoSender
from telethon.tl.alltlobjects import LAYER
from telethon.tl.functions import InvokeWithLayerRequest
from telethon.tl.functions.auth import ExportAuthorizationRequest, ImportAuthorizationRequest
from telethon.tl.functions.upload import GetFileRequest, SaveBigFilePartRequest, SaveFilePartRequest
from telethon.tl.types import (
    Document,
    InputDocumentFileLocation,
    InputFile,
    InputFileBig,
    InputFileLocation,
    InputPeerPhotoFileLocation,
    InputPhotoFileLocation,
)

log = logging.getLogger(__name__)

TypeLocation = Union[
    Document,
    InputDocumentFileLocation,
    InputPeerPhotoFileLocation,
    InputFileLocation,
    InputPhotoFileLocation,
]
UploadInput = Union[str, os.PathLike[str], BinaryIO]
ProgressCallback = Optional[Callable[[int, int], Any]]


async def _maybe_await(result: Any) -> None:
    if inspect.isawaitable(result):
        await result


def _stream_file(file_to_stream: BinaryIO, chunk_size: int) -> Iterable[bytes]:
    while True:
        data_read = file_to_stream.read(chunk_size)
        if not data_read:
            break
        yield data_read


def _get_file_size(file: UploadInput) -> int:
    if isinstance(file, (str, os.PathLike)):
        return os.path.getsize(file)

    if getattr(file, "name", None):
        with suppress(OSError, TypeError):
            return os.path.getsize(file.name)

    if hasattr(file, "fileno"):
        with suppress(OSError, TypeError, ValueError):
            return os.fstat(file.fileno()).st_size

    current = file.tell()
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(current)
    return size


def _ensure_binary_file(file: UploadInput) -> Tuple[BinaryIO, bool]:
    if isinstance(file, (str, os.PathLike)):
        return open(file, "rb"), True
    return file, False


class DownloadSender:
    client: TelegramClient
    sender: MTProtoSender
    request: GetFileRequest
    remaining: int
    stride: int
    retries: int

    def __init__(
        self,
        client: TelegramClient,
        sender: MTProtoSender,
        file: TypeLocation,
        offset: int,
        limit: int,
        stride: int,
        count: int,
        retries: int = 3,
    ) -> None:
        self.client = client
        self.sender = sender
        self.request = GetFileRequest(file, offset=offset, limit=limit)
        self.stride = stride
        self.remaining = count
        self.retries = retries

    async def next(self) -> Optional[bytes]:
        if not self.remaining:
            return None

        for attempt in range(1, self.retries + 1):
            try:
                result = await self.client._call(self.sender, self.request)
                self.remaining -= 1
                self.request.offset += self.stride
                return result.bytes or None
            except Exception as err:
                if attempt >= self.retries:
                    raise
                log.warning(
                    "Download chunk retry attempt=%s/%s offset=%s limit=%s error=%s",
                    attempt,
                    self.retries,
                    self.request.offset,
                    self.request.limit,
                    err,
                )
                await asyncio.sleep(2 ** (attempt - 1))

        return None

    def disconnect(self):
        return self.sender.disconnect()


class UploadSender:
    client: TelegramClient
    sender: MTProtoSender
    request: Union[SaveFilePartRequest, SaveBigFilePartRequest]
    part_count: int
    stride: int
    previous: Optional[asyncio.Task]
    loop: asyncio.AbstractEventLoop
    retries: int

    def __init__(
        self,
        client: TelegramClient,
        sender: MTProtoSender,
        file_id: int,
        part_count: int,
        big: bool,
        index: int,
        stride: int,
        loop: asyncio.AbstractEventLoop,
        retries: int = 3,
    ) -> None:
        self.client = client
        self.sender = sender
        self.part_count = part_count
        self.stride = stride
        self.previous = None
        self.loop = loop
        self.retries = retries
        if big:
            self.request = SaveBigFilePartRequest(file_id, index, part_count, b"")
        else:
            self.request = SaveFilePartRequest(file_id, index, b"")

    async def next(self, data: bytes) -> None:
        if self.previous:
            await self.previous
        self.previous = self.loop.create_task(self._next(data))

    async def _next(self, data: bytes) -> None:
        self.request.bytes = data
        for attempt in range(1, self.retries + 1):
            try:
                await self.client._call(self.sender, self.request)
                self.request.file_part += self.stride
                return
            except Exception as err:
                if attempt >= self.retries:
                    raise
                log.warning(
                    "Upload chunk retry attempt=%s/%s file_part=%s error=%s",
                    attempt,
                    self.retries,
                    self.request.file_part,
                    err,
                )
                await asyncio.sleep(2 ** (attempt - 1))

    async def disconnect(self) -> None:
        if self.previous:
            await self.previous
        await self.sender.disconnect()


class ParallelTransferrer:
    client: TelegramClient
    loop: asyncio.AbstractEventLoop
    dc_id: int
    senders: Optional[List[Union[DownloadSender, UploadSender]]]
    auth_key: Optional[AuthKey]
    upload_ticker: int

    def __init__(self, client: TelegramClient, dc_id: Optional[int] = None) -> None:
        self.client = client
        self.loop = self.client.loop
        self.dc_id = dc_id or self.client.session.dc_id
        self.auth_key = (
            None
            if dc_id and self.client.session.dc_id != dc_id
            else self.client.session.auth_key
        )
        self.senders = None
        self.upload_ticker = 0

    async def _cleanup(self) -> None:
        if not self.senders:
            return
        await asyncio.gather(
            *(sender.disconnect() for sender in self.senders),
            return_exceptions=True,
        )
        self.senders = None

    @staticmethod
    def _get_connection_count(
        file_size: int,
        max_count: int = 20,
        full_size: int = 100 * 1024 * 1024,
    ) -> int:
        if file_size > full_size:
            return max_count
        return max(1, math.ceil((file_size / full_size) * max_count))

    async def _create_sender(self) -> MTProtoSender:
        dc = await self.client._get_dc(self.dc_id)
        sender = MTProtoSender(self.auth_key, loggers=self.client._log)
        await sender.connect(
            self.client._connection(
                dc.ip_address,
                dc.port,
                dc.id,
                loggers=self.client._log,
                proxy=self.client._proxy,
            )
        )
        if self.auth_key is None:
            auth = await self.client(ExportAuthorizationRequest(self.dc_id))
            self.client._init_request.query = ImportAuthorizationRequest(
                id=auth.id,
                bytes=auth.bytes,
            )
            req = InvokeWithLayerRequest(LAYER, self.client._init_request)
            await sender.send(req)
            self.auth_key = sender.auth_key
        return sender

    async def _create_download_sender(
        self,
        file: TypeLocation,
        index: int,
        part_size: int,
        stride: int,
        part_count: int,
        offset: int = 0,
    ) -> DownloadSender:
        return DownloadSender(
            self.client,
            await self._create_sender(),
            file,
            offset + index * part_size,
            part_size,
            stride,
            part_count,
        )

    async def _create_upload_sender(
        self,
        file_id: int,
        part_count: int,
        big: bool,
        index: int,
        stride: int,
    ) -> UploadSender:
        return UploadSender(
            self.client,
            await self._create_sender(),
            file_id,
            part_count,
            big,
            index,
            stride,
            loop=self.loop,
        )

    async def _init_download(
        self,
        connections: int,
        file: TypeLocation,
        part_count: int,
        part_size: int,
        offset: int,
    ) -> None:
        connections = max(1, min(connections, part_count or 1))
        minimum, remainder = divmod(part_count, connections)

        def get_part_count() -> int:
            nonlocal remainder
            if remainder > 0:
                remainder -= 1
                return minimum + 1
            return minimum

        senders: List[DownloadSender] = [
            await self._create_download_sender(
                file,
                0,
                part_size,
                connections * part_size,
                get_part_count(),
                offset=offset,
            )
        ]
        try:
            senders.extend(
                await asyncio.gather(
                    *[
                        self._create_download_sender(
                            file,
                            i,
                            part_size,
                            connections * part_size,
                            get_part_count(),
                            offset=offset,
                        )
                        for i in range(1, connections)
                    ]
                )
            )
        except Exception:
            await self._cleanup_sender_list(senders)
            raise
        self.senders = senders

    async def _create_upload_sender_list(
        self,
        connections: int,
        file_id: int,
        part_count: int,
        big: bool,
    ) -> None:
        connections = max(1, min(connections, part_count or 1))
        senders: List[UploadSender] = [
            await self._create_upload_sender(file_id, part_count, big, 0, connections)
        ]
        try:
            senders.extend(
                await asyncio.gather(
                    *[
                        self._create_upload_sender(file_id, part_count, big, i, connections)
                        for i in range(1, connections)
                    ]
                )
            )
        except Exception:
            await self._cleanup_sender_list(senders)
            raise
        self.senders = senders

    async def _cleanup_sender_list(
        self, senders: Optional[List[Union[DownloadSender, UploadSender]]]
    ) -> None:
        if not senders:
            return
        await asyncio.gather(
            *(sender.disconnect() for sender in senders),
            return_exceptions=True,
        )

    async def init_upload(
        self,
        file_id: int,
        file_size: int,
        part_size_kb: Optional[float] = None,
        connection_count: Optional[int] = None,
    ) -> Tuple[int, int, bool]:
        connection_count = connection_count or self._get_connection_count(file_size)
        part_size = int((part_size_kb or telethon_utils.get_appropriated_part_size(file_size)) * 1024)
        part_count = (file_size + part_size - 1) // part_size
        is_large = file_size > 10 * 1024 * 1024
        await self._create_upload_sender_list(connection_count, file_id, part_count, is_large)
        return part_size, part_count, is_large

    async def upload(self, part: bytes) -> None:
        if not self.senders:
            raise RuntimeError("upload senders are not initialized")
        await self.senders[self.upload_ticker].next(part)
        self.upload_ticker = (self.upload_ticker + 1) % len(self.senders)

    async def finish_upload(self) -> None:
        await self._cleanup()

    async def download(
        self,
        file: TypeLocation,
        file_size: int,
        part_size_kb: Optional[float] = None,
        connection_count: Optional[int] = None,
        offset: int = 0,
    ) -> AsyncGenerator[bytes, None]:
        remaining_size = max(file_size - offset, 0)
        if remaining_size == 0:
            return

        connection_count = connection_count or self._get_connection_count(remaining_size)
        part_size = int((part_size_kb or telethon_utils.get_appropriated_part_size(remaining_size)) * 1024)
        part_count = math.ceil(remaining_size / part_size)
        log.info(
            "Parallel download start dc_id=%s size=%s remaining=%s part_size=%s workers=%s offset=%s",
            self.dc_id,
            file_size,
            remaining_size,
            part_size,
            connection_count,
            offset,
        )
        await self._init_download(connection_count, file, part_count, part_size, offset)

        part = 0
        try:
            while part < part_count:
                tasks = [self.loop.create_task(sender.next()) for sender in self.senders or []]
                try:
                    results = await asyncio.gather(*tasks)
                    for data in results:
                        if not data:
                            return
                        yield data
                        part += 1
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await self._cleanup()


async def _internal_transfer_to_telegram(
    client: TelegramClient,
    response: UploadInput,
    progress_callback: ProgressCallback,
    *,
    part_size_kb: Optional[float] = None,
    connection_count: Optional[int] = None,
) -> Tuple[Union[InputFileBig, InputFile], int]:
    file, should_close = _ensure_binary_file(response)
    try:
        file_size = _get_file_size(response)
        with suppress(Exception):
            file.seek(0)

        file_id = helpers.generate_random_long()
        hash_md5 = hashlib.md5()
        uploader = ParallelTransferrer(client)
        part_size, part_count, is_large = await uploader.init_upload(
            file_id,
            file_size,
            part_size_kb=part_size_kb,
            connection_count=connection_count,
        )
        log.info(
            "Parallel upload start dc_id=%s size=%s part_size=%s part_count=%s workers=%s large=%s",
            uploader.dc_id,
            file_size,
            part_size,
            part_count,
            connection_count or uploader._get_connection_count(file_size),
            is_large,
        )
        buffer = bytearray()
        try:
            for data in _stream_file(file, part_size):
                if progress_callback:
                    await _maybe_await(progress_callback(file.tell(), file_size))
                if not is_large:
                    hash_md5.update(data)
                if len(buffer) == 0 and len(data) == part_size:
                    await uploader.upload(data)
                    continue
                new_len = len(buffer) + len(data)
                if new_len >= part_size:
                    cutoff = part_size - len(buffer)
                    buffer.extend(data[:cutoff])
                    await uploader.upload(bytes(buffer))
                    buffer.clear()
                    buffer.extend(data[cutoff:])
                else:
                    buffer.extend(data)
            if len(buffer) > 0:
                await uploader.upload(bytes(buffer))
        finally:
            await uploader.finish_upload()
        if is_large:
            return InputFileBig(file_id, part_count, "upload"), file_size
        return InputFile(file_id, part_count, "upload", hash_md5.hexdigest()), file_size
    finally:
        if should_close:
            file.close()


async def download_file(
    client: TelegramClient,
    location: TypeLocation,
    out: BinaryIO,
    progress_callback: ProgressCallback = None,
    *,
    file_size: Optional[int] = None,
    part_size_kb: Optional[float] = None,
    connection_count: Optional[int] = None,
    offset: int = 0,
) -> BinaryIO:
    size = file_size if file_size is not None else getattr(location, "size", None)
    if size is None:
        raise ValueError("file_size is required when the media size is not available")

    dc_id, input_location = telethon_utils.get_input_location(location)
    downloader = ParallelTransferrer(client, dc_id)
    async for chunk in downloader.download(
        input_location,
        size,
        part_size_kb=part_size_kb,
        connection_count=connection_count,
        offset=offset,
    ):
        out.write(chunk)
        if progress_callback:
            await _maybe_await(progress_callback(out.tell(), size))
    return out


async def upload_file(
    client: TelegramClient,
    file: UploadInput,
    progress_callback: ProgressCallback = None,
    *,
    part_size_kb: Optional[float] = None,
    connection_count: Optional[int] = None,
) -> Union[InputFileBig, InputFile]:
    return (
        await _internal_transfer_to_telegram(
            client,
            file,
            progress_callback,
            part_size_kb=part_size_kb,
            connection_count=connection_count,
        )
    )[0]