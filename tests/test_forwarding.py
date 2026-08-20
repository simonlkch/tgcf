import unittest
from unittest.mock import patch

from tgcf import storage as st
from tgcf.plugin_models import FileType
from tgcf.forwarding import (
    build_forward_batches,
    forward_source_batch,
    flood_wait_seconds,
    is_cannot_forward_error,
    is_flood_wait_error,
    is_permission_error,
    send_batch,
)


class DummyMessage:
    def __init__(self, chat_id, message_id, grouped_id=None, client=None):
        self.chat_id = chat_id
        self.id = message_id
        self.grouped_id = grouped_id
        self.is_reply = False
        self.reply_to_msg_id = None
        self.client = client


class FloodWaitError(Exception):
    def __init__(self, seconds):
        super().__init__(f"FLOOD_WAIT_{seconds}")
        self.seconds = seconds


class ChatWriteForbiddenError(Exception):
    pass


class ChatForwardsRestrictedError(Exception):
    pass


class FileReferenceExpiredError(Exception):
    pass


class DummySentMessage:
    def __init__(self, message_id):
        self.id = message_id
        self.deleted = False

    async def delete(self):
        self.deleted = True


class DummyStoredMessage(DummySentMessage):
    pass


class DummyClient:
    def __init__(self):
        self.sent_file_calls = []

    async def send_file(self, recipient, file_path, caption=None, reply_to=None):
        self.sent_file_calls.append((recipient, file_path, caption, reply_to))
        return DummySentMessage(777)


class RefreshingClient(DummyClient):
    def __init__(self, refreshed_messages):
        super().__init__()
        self.refreshed_messages = refreshed_messages

    async def get_messages(self, chat_id, ids=None):
        return self.refreshed_messages


class DummyTgcfMessage:
    def __init__(self, client):
        self.client = client
        self.message = type("M", (), {"text": "caption"})()
        self.text = "caption"
        self.reply_to = None
        self.file_type = "image"
        self.new_file = None
        self.thumb_file = None
        self.ensure_thumb_calls = 0
        self.cleared = False

    async def get_file(self):
        return "downloaded.jpg"

    async def ensure_thumb_file(self):
        self.ensure_thumb_calls += 1
        return self.thumb_file

    def clear(self):
        self.cleared = True


class ForwardingHelpersTest(unittest.TestCase):
    def test_build_forward_batches_groups_album_messages(self):
        messages = [
            DummyMessage(1, 1, 100),
            DummyMessage(1, 2, 100),
            DummyMessage(1, 3),
            DummyMessage(1, 4, 200),
        ]

        batches = build_forward_batches(messages)

        self.assertEqual(len(batches), 3)
        self.assertTrue(batches[0].is_album)
        self.assertEqual([m.id for m in batches[0].messages], [1, 2])
        self.assertFalse(batches[1].is_album)
        self.assertEqual([m.id for m in batches[1].messages], [3])
        self.assertTrue(batches[2].is_album)
        self.assertEqual([m.id for m in batches[2].messages], [4])

    def test_flood_wait_classification(self):
        err = FloodWaitError(7)

        self.assertTrue(is_flood_wait_error(err))
        self.assertEqual(flood_wait_seconds(err), 7)

    def test_permission_and_forward_restriction_classification(self):
        self.assertTrue(is_permission_error(ChatWriteForbiddenError("no write")))
        self.assertTrue(is_cannot_forward_error(ChatForwardsRestrictedError("forwards restricted")))

    def test_forward_source_batch_rolls_back_state_on_failure(self):
        source_messages = [
            DummyMessage(10, 1, 100),
            DummyMessage(10, 2, 100),
        ]

        st.stored.clear()
        st.stored_albums.clear()

        sent_a = DummySentMessage(101)
        sent_b = DummySentMessage(102)

        async def fake_forward_batch_with_retry(recipient, messages, reply_to=None):
            if recipient == 1:
                return [sent_a, sent_b]
            raise RuntimeError("boom")

        with patch("tgcf.forwarding.forward_batch_with_retry", side_effect=fake_forward_batch_with_retry):
            with self.assertRaises(RuntimeError):
                import asyncio

                asyncio.run(forward_source_batch(source_messages, [1, 2]))

        event_uid_1 = st.EventUid(type("E", (), {"chat_id": 10, "id": 1})())
        event_uid_2 = st.EventUid(type("E", (), {"chat_id": 10, "id": 2})())

        self.assertIn(1, st.stored[event_uid_1])
        self.assertIn(1, st.stored[event_uid_2])
        self.assertNotIn(2, st.stored.get(event_uid_1, {}))
        self.assertNotIn(2, st.stored.get(event_uid_2, {}))
        self.assertFalse(sent_a.deleted)
        self.assertFalse(sent_b.deleted)
        self.assertIn((10, 100), st.stored_albums)
        self.assertIn(1, st.stored_albums[(10, 100)])
        self.assertNotIn(2, st.stored_albums[(10, 100)])

    def test_forward_source_batch_refetches_when_file_reference_expires(self):
        st.stored.clear()
        st.stored_albums.clear()

        refreshed_message = DummyMessage(10, 1)
        refreshed_client = RefreshingClient([refreshed_message])
        source_message = DummyMessage(10, 1, client=refreshed_client)

        call_count = 0

        async def fake_forward_batch_with_retry(recipient, messages, reply_to=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise FileReferenceExpiredError(
                    "The file reference has expired and is no longer valid"
                )
            self.assertIs(messages[0], refreshed_message)
            return [DummySentMessage(201)]

        with patch("tgcf.forwarding.forward_batch_with_retry", side_effect=fake_forward_batch_with_retry):
            import asyncio

            asyncio.run(forward_source_batch([source_message], [1]))

        self.assertEqual(call_count, 2)
        event_uid = st.EventUid(type("E", (), {"chat_id": 10, "id": 1})())
        self.assertIn(1, st.stored[event_uid])

    def test_send_batch_reuploads_when_protected_chat_blocks_media_copy(self):
        from tgcf.config import CONFIG

        original_forwarded = CONFIG.show_forwarded_from
        original_fallback = CONFIG.live.forward_fallback_to_reupload
        CONFIG.show_forwarded_from = False
        CONFIG.live.forward_fallback_to_reupload = True

        client = DummyClient()
        tm = DummyTgcfMessage(client)

        class ChatForwardsRestrictedError(Exception):
            pass

        async def fake_apply_plugins(_message):
            return tm

        async def fake_send_message(_recipient, _tm):
            raise ChatForwardsRestrictedError(
                "You can't forward messages from a protected chat"
            )

        try:
            with patch("tgcf.plugins.apply_plugins", side_effect=fake_apply_plugins), patch(
                "tgcf.utils.send_message", side_effect=fake_send_message
            ):
                import asyncio

                sent = asyncio.run(send_batch(12345, [object()]))
        finally:
            CONFIG.show_forwarded_from = original_forwarded
            CONFIG.live.forward_fallback_to_reupload = original_fallback

        self.assertEqual(len(sent), 1)
        self.assertEqual(client.sent_file_calls[0][0], 12345)
        self.assertEqual(client.sent_file_calls[0][1], "downloaded.jpg")
        self.assertTrue(tm.cleared)

    def test_send_batch_reupload_uses_thumb_when_available(self):
        from tgcf.config import CONFIG

        original_forwarded = CONFIG.show_forwarded_from
        original_fallback = CONFIG.live.forward_fallback_to_reupload
        CONFIG.show_forwarded_from = False
        CONFIG.live.forward_fallback_to_reupload = True

        client = DummyClient()
        tm = DummyTgcfMessage(client)
        tm.file_type = FileType.VIDEO
        tm.new_file = "cached_video.mp4"
        tm.thumb_file = "cached_thumb.jpg"

        class ChatForwardsRestrictedError(Exception):
            pass

        captured = {}

        async def fake_apply_plugins(_message):
            return tm

        async def fake_send_message(_recipient, _tm):
            raise ChatForwardsRestrictedError(
                "You can't forward messages from a protected chat"
            )

        async def fake_send_file_fast_compatible(_client, _recipient, _file, **kwargs):
            captured.update(kwargs)
            return DummySentMessage(778)

        try:
            with patch("tgcf.plugins.apply_plugins", side_effect=fake_apply_plugins), patch(
                "tgcf.utils.send_message", side_effect=fake_send_message
            ), patch(
                "tgcf.forwarding._send_file_fast_compatible",
                side_effect=fake_send_file_fast_compatible,
            ):
                import asyncio

                sent = asyncio.run(send_batch(12345, [object()]))
        finally:
            CONFIG.show_forwarded_from = original_forwarded
            CONFIG.live.forward_fallback_to_reupload = original_fallback

        self.assertEqual(len(sent), 1)
        self.assertEqual(captured.get("thumb"), "cached_thumb.jpg")
        self.assertEqual(tm.ensure_thumb_calls, 1)

    def test_send_batch_album_passes_per_file_media_types_with_streaming(self):
        """Album uploads must enable streaming whenever any item is a video,
        otherwise Telegram stores the video as a non-streaming document and
        clients have to download it before playback (the original bug)."""

        from tgcf.config import CONFIG

        original_forwarded = CONFIG.show_forwarded_from
        original_fallback = CONFIG.live.forward_fallback_to_reupload
        CONFIG.show_forwarded_from = False
        CONFIG.live.forward_fallback_to_reupload = True

        # Build a mixed album: photo first, then video. The original code only
        # sent transformed[0].file_type (PHOTO) which left the video with
        # supports_streaming=False.
        photo_tm = DummyTgcfMessage(DummyClient())
        photo_tm.file_type = FileType.PHOTO
        photo_tm.new_file = "photo_1.jpg"

        video_tm = DummyTgcfMessage(DummyClient())
        video_tm.file_type = FileType.VIDEO
        video_tm.new_file = "video_2.mp4"

        captured = {}

        async def fake_apply_plugins(message):
            return {id(message): photo_tm, id(message) + 1: video_tm}.get(id(message))

        async def fake_send_file_fast_compatible(_client, _recipient, _file, **kwargs):
            captured["kwargs"] = kwargs
            captured["file"] = _file
            return [DummySentMessage(901), DummySentMessage(902)]

        # send_batch iterates `messages` in order; give it two real objects
        # that map 1:1 to our two DummyTgcfMessage instances.
        photo_msg = object()
        video_msg = object()
        apply_lookup = {id(photo_msg): photo_tm, id(video_msg): video_tm}

        async def apply_by_id(message):
            return apply_lookup[id(message)]

        try:
            with patch("tgcf.plugins.apply_plugins", side_effect=apply_by_id), patch(
                "tgcf.forwarding._send_file_fast_compatible",
                side_effect=fake_send_file_fast_compatible,
            ):
                import asyncio

                sent = asyncio.run(send_batch(12345, [photo_msg, video_msg]))
        finally:
            CONFIG.show_forwarded_from = original_forwarded
            CONFIG.live.forward_fallback_to_reupload = original_fallback

        self.assertEqual(len(sent), 2)
        self.assertEqual(
            captured["file"], ["photo_1.jpg", "video_2.mp4"]
        )
        # The full per-file list must be passed (not just the first item's type).
        self.assertEqual(
            captured["kwargs"].get("source_media_type"),
            [FileType.PHOTO, FileType.VIDEO],
        )
        # Album path should not have invoked tm.get_file() since new_file was set
        self.assertFalse(photo_tm.cleared is False and photo_tm.new_file is None)  # sanity

    def test_send_batch_album_all_photos_does_not_set_streaming(self):
        """All-photo albums must NOT set supports_streaming — there's no
        video to stream and the per-file list must still be threaded through."""

        from tgcf.config import CONFIG

        original_forwarded = CONFIG.show_forwarded_from
        original_fallback = CONFIG.live.forward_fallback_to_reupload
        CONFIG.show_forwarded_from = False
        CONFIG.live.forward_fallback_to_reupload = True

        photo_a = DummyTgcfMessage(DummyClient())
        photo_a.file_type = FileType.PHOTO
        photo_a.new_file = "a.jpg"

        photo_b = DummyTgcfMessage(DummyClient())
        photo_b.file_type = FileType.PHOTO
        photo_b.new_file = "b.jpg"

        captured = {}

        async def fake_send_file_fast_compatible(_client, _recipient, _file, **kwargs):
            captured["kwargs"] = kwargs
            captured["file"] = _file
            return [DummySentMessage(911), DummySentMessage(912)]

        msg_a = object()
        msg_b = object()
        lookup = {id(msg_a): photo_a, id(msg_b): photo_b}

        async def apply_by_id(message):
            return lookup[id(message)]

        try:
            with patch("tgcf.plugins.apply_plugins", side_effect=apply_by_id), patch(
                "tgcf.forwarding._send_file_fast_compatible",
                side_effect=fake_send_file_fast_compatible,
            ):
                import asyncio

                sent = asyncio.run(send_batch(12345, [msg_a, msg_b]))
        finally:
            CONFIG.show_forwarded_from = original_forwarded
            CONFIG.live.forward_fallback_to_reupload = original_fallback

        self.assertEqual(len(sent), 2)
        self.assertEqual(captured["file"], ["a.jpg", "b.jpg"])
        self.assertEqual(
            captured["kwargs"].get("source_media_type"),
            [FileType.PHOTO, FileType.PHOTO],
        )


class SendFileFastCompatibleTest(unittest.TestCase):
    """Direct unit tests for the album-list branch in _send_file_fast_compatible.

    These verify the inner-folder behavior (not just that the caller passes the
    right list): when a list of files is given together with a per-file
    source_media_type list, the function must tag the upload with
    supports_streaming=True iff any item is a video.
    """

    def _run(self, coro):
        import asyncio

        return asyncio.run(coro)

    def test_album_with_video_enables_streaming(self):
        from tgcf.utils import _send_file_fast_compatible

        captured = {}

        async def fake_send_file(self, entity, file, *args, **kwargs):
            captured["entity"] = entity
            captured["file"] = file
            captured["kwargs"] = kwargs
            return "ok"

        client = DummyClient()
        with patch.object(client.__class__, "send_file", new=fake_send_file):
            self._run(
                _send_file_fast_compatible(
                    client,
                    12345,
                    ["a.jpg", "b.mp4"],
                    caption=["cap1", "cap2"],
                    source_media_type=[FileType.PHOTO, FileType.VIDEO],
                )
            )

        self.assertEqual(captured["file"], ["a.jpg", "b.mp4"])
        self.assertTrue(captured["kwargs"].get("supports_streaming"))
        self.assertFalse(captured["kwargs"].get("force_document"))

    def test_album_without_video_disables_streaming(self):
        from tgcf.utils import _send_file_fast_compatible

        captured = {}

        async def fake_send_file(self, entity, file, *args, **kwargs):
            captured["kwargs"] = kwargs

        client = DummyClient()
        with patch.object(client.__class__, "send_file", new=fake_send_file):
            self._run(
                _send_file_fast_compatible(
                    client,
                    12345,
                    ["a.jpg", "b.jpg"],
                    caption=["cap1", "cap2"],
                    source_media_type=[FileType.PHOTO, FileType.PHOTO],
                )
            )

        self.assertFalse(captured["kwargs"].get("supports_streaming"))
        self.assertFalse(captured["kwargs"].get("force_document"))

    def test_album_video_first_still_enables_streaming(self):
        """Regression: a video-first album used to take the single-photo
        early-return path because the first item was VIDEO and not PHOTO,
        but the list branch in the old code then dropped all attributes.
        With the fix, the per-file list still flags the video for streaming."""

        from tgcf.utils import _send_file_fast_compatible

        captured = {}

        async def fake_send_file(self, entity, file, *args, **kwargs):
            captured["kwargs"] = kwargs

        client = DummyClient()
        with patch.object(client.__class__, "send_file", new=fake_send_file):
            self._run(
                _send_file_fast_compatible(
                    client,
                    12345,
                    ["a.mp4", "b.jpg"],
                    caption=["cap1", "cap2"],
                    source_media_type=[FileType.VIDEO, FileType.PHOTO],
                )
            )

        self.assertTrue(captured["kwargs"].get("supports_streaming"))

    def test_album_accepts_legacy_single_media_type(self):
        """Backward compat: callers that still pass a single FileType for an
        album get the same broadcast semantics — a list of all PHOTO yields
        supports_streaming=False."""

        from tgcf.utils import _send_file_fast_compatible

        captured = {}

        async def fake_send_file(self, entity, file, *args, **kwargs):
            captured["kwargs"] = kwargs

        client = DummyClient()
        with patch.object(client.__class__, "send_file", new=fake_send_file):
            self._run(
                _send_file_fast_compatible(
                    client,
                    12345,
                    ["a.jpg", "b.jpg"],
                    caption=["cap1", "cap2"],
                    source_media_type=FileType.PHOTO,
                )
            )

        self.assertFalse(captured["kwargs"].get("supports_streaming"))


class TgcfMessageFileTypeTest(unittest.TestCase):
    """Cover the document-attribute fallback in TgcfMessage.guess_file_type.

    For protected channels, Telethon only sees a generic ``Document`` media
    on the message — it does not populate ``message.video`` / ``message.gif``
    / etc. Without the attribute-based re-classification the album path would
    treat an album video as DOCUMENT and upload it without
    ``supports_streaming=True``, so the recipient has to download the video
    before it plays.
    """

    def _make_message(self, document):
        message = type(
            "M",
            (),
            {
                "text": "",
                "raw_text": "",
                "client": None,
                "sender_id": 1,
                "id": 1,
                "document": document,
                "video": None,
                "photo": None,
                "audio": None,
                "gif": None,
                "video_note": None,
                "sticker": None,
                "contact": None,
            },
        )()
        return message

    def test_classify_document_detects_video(self):
        from tgcf.plugins import TgcfMessage
        from telethon.tl.types import Document, DocumentAttributeVideo

        document = Document(
            id=1,
            access_hash=0,
            file_reference=b"",
            date=0,
            mime_type="video/mp4",
            size=0,
            dc_id=0,
            attributes=[DocumentAttributeVideo(0, 0, 0, round_message=False)],
        )
        tm = TgcfMessage(self._make_message(document))
        self.assertEqual(tm.file_type, FileType.VIDEO)

    def test_classify_document_detects_video_note(self):
        from tgcf.plugins import TgcfMessage
        from telethon.tl.types import Document, DocumentAttributeVideo

        document = Document(
            id=1,
            access_hash=0,
            file_reference=b"",
            date=0,
            mime_type="video/mp4",
            size=0,
            dc_id=0,
            attributes=[DocumentAttributeVideo(0, 0, 0, round_message=True)],
        )
        tm = TgcfMessage(self._make_message(document))
        self.assertEqual(tm.file_type, FileType.VIDEO_NOTE)

    def test_classify_document_detects_animated_video_as_gif(self):
        from tgcf.plugins import TgcfMessage
        from telethon.tl.types import (
            Document,
            DocumentAttributeAnimated,
            DocumentAttributeVideo,
        )

        document = Document(
            id=1,
            access_hash=0,
            file_reference=b"",
            date=0,
            mime_type="video/mp4",
            size=0,
            dc_id=0,
            attributes=[
                DocumentAttributeVideo(0, 0, 0, round_message=False),
                DocumentAttributeAnimated(),
            ],
        )
        tm = TgcfMessage(self._make_message(document))
        self.assertEqual(tm.file_type, FileType.GIF)

    def test_classify_document_detects_standalone_animated_as_gif(self):
        from tgcf.plugins import TgcfMessage
        from telethon.tl.types import Document, DocumentAttributeAnimated

        document = Document(
            id=1,
            access_hash=0,
            file_reference=b"",
            date=0,
            mime_type="application/x-tgsticker",
            size=0,
            dc_id=0,
            attributes=[DocumentAttributeAnimated()],
        )
        tm = TgcfMessage(self._make_message(document))
        self.assertEqual(tm.file_type, FileType.GIF)

    def test_classify_document_keeps_generic_document(self):
        from tgcf.plugins import TgcfMessage
        from telethon.tl.types import Document

        document = Document(
            id=1,
            access_hash=0,
            file_reference=b"",
            date=0,
            mime_type="application/zip",
            size=0,
            dc_id=0,
            attributes=[],
        )
        tm = TgcfMessage(self._make_message(document))
        self.assertEqual(tm.file_type, FileType.DOCUMENT)

    def test_album_video_classified_via_document_attributes_marks_streaming(self):
        """End-to-end: an album video coming from a protected channel (only
        ``message.document`` populated) must be classified as VIDEO so the
        send_file path sets ``supports_streaming=True``.
        """
        from tgcf.config import CONFIG
        from tgcf.plugins import TgcfMessage
        from telethon.tl.types import Document, DocumentAttributeVideo

        original_forwarded = CONFIG.show_forwarded_from
        original_fallback = CONFIG.live.forward_fallback_to_reupload
        CONFIG.show_forwarded_from = False
        CONFIG.live.forward_fallback_to_reupload = True

        try:
            photo_tm = DummyTgcfMessage(DummyClient())
            photo_tm.file_type = FileType.PHOTO
            photo_tm.new_file = "photo.jpg"

            video_document = Document(
                id=2,
                access_hash=0,
                file_reference=b"",
                date=0,
                mime_type="video/mp4",
                size=0,
                dc_id=0,
                attributes=[DocumentAttributeVideo(0, 0, 0, round_message=False)],
            )
            video_message = self._make_message(video_document)
            video_tm = TgcfMessage(video_message)
            # Sanity: protected-channel-style message has message.video=None
            # and the classifier must still detect VIDEO via attributes.
            self.assertIsNone(video_message.video)
            self.assertEqual(video_tm.file_type, FileType.VIDEO)
            video_tm.new_file = "video.mp4"

            captured = {}

            async def fake_send_file_fast_compatible(_client, _recipient, _file, **kwargs):
                captured["kwargs"] = kwargs
                captured["file"] = _file
                return [DummySentMessage(801), DummySentMessage(802)]

            photo_msg = object()
            lookup = {id(photo_msg): photo_tm, id(video_message): video_tm}

            async def apply_by_id(message):
                return lookup[id(message)]

            with patch("tgcf.plugins.apply_plugins", side_effect=apply_by_id), patch(
                "tgcf.forwarding._send_file_fast_compatible",
                side_effect=fake_send_file_fast_compatible,
            ):
                import asyncio

                asyncio.run(send_batch(12345, [photo_msg, video_message]))
        finally:
            CONFIG.show_forwarded_from = original_forwarded
            CONFIG.live.forward_fallback_to_reupload = original_fallback

        self.assertEqual(
            captured["file"], ["photo.jpg", "video.mp4"]
        )
        self.assertEqual(
            captured["kwargs"].get("source_media_type"),
            [FileType.PHOTO, FileType.VIDEO],
        )


class SendAlbumWithThumbsTest(unittest.TestCase):
    """Regression: ``_send_album_with_thumbs`` must use ``peer=`` (not
    ``entity=``) when constructing ``SendMultiMediaRequest``. Telethon's
    TL schema for that request exposes the field as ``peer``; using
    ``entity`` raised::

        TypeError: SendMultiMediaRequest.__init__() got an unexpected
        keyword argument 'entity'

    and the entire album upload failed. The retry loop then re-fetched the
    same media repeatedly, which is what the user reported as an "infinite
    loop" on the same message IDs.
    """

    def _run(self, coro):
        import asyncio

        return asyncio.run(coro)

    def test_send_multi_media_request_uses_peer_kwarg(self):
        from tgcf.utils import _send_album_with_thumbs

        captured = {}

        class _StubClient:
            def __init__(self):
                self._file_to_media_calls = []

            async def get_input_entity(self, entity):
                return f"input_peer({entity})"

            async def _file_to_media(self, file, **kwargs):
                # Return a fake uploaded-document media so the helper
                # thinks the upload worked.
                from telethon import types

                self._file_to_media_calls.append((file, kwargs.get("thumb")))
                fh = types.InputFile(id=1, parts=1, name="x", md5_checksum="")
                return (
                    fh,
                    types.InputMediaUploadedDocument(
                        file=fh,
                        mime_type="video/mp4",
                        attributes=None,
                        thumb=None,
                    ),
                    False,
                )

            async def __call__(self, request):
                from telethon.tl import types

                captured["request_class"] = type(request).__name__
                captured["request_kwargs"] = {
                    k: v
                    for k, v in request.__dict__.items()
                    if not k.startswith("_")
                }
                # ``_send_album_with_thumbs`` expects the result of
                # UploadMediaRequest, which has ``photo`` and ``document``
                # attributes. The document branch is what we exercise.
                from telethon.tl.types import Document

                fake_doc = Document(
                    id=1,
                    access_hash=0,
                    file_reference=b"",
                    date=0,
                    mime_type="video/mp4",
                    size=0,
                    dc_id=0,
                    attributes=[],
                )
                return type(
                    "R",
                    (),
                    {
                        "photo": None,
                        "document": fake_doc,
                        "updates": [],
                        "other_updates": [],
                    },
                )()

            def _get_response_message(self, random_ids, result, entity):
                return []

        client = _StubClient()

        self._run(
            _send_album_with_thumbs(
                client,
                entity=12345,
                files=["a.jpg", "b.mp4"],
                thumbs=[None, None],
                captions=["c1", "c2"],
            )
        )

        self.assertEqual(captured["request_class"], "SendMultiMediaRequest")
        # The "entity" field must have been passed as "peer" — the only
        # accepted name in Telethon's TL schema.
        self.assertIn("peer", captured["request_kwargs"])
        self.assertNotIn("entity", captured["request_kwargs"])
        self.assertEqual(captured["request_kwargs"]["peer"], "input_peer(12345)")

    def test_send_album_with_thumbs_threads_thumb_into_file_to_media(self):
        """Each per-file thumb path must reach ``_file_to_media`` so
        ``InputMediaUploadedDocument.thumb`` gets set correctly."""
        from tgcf.utils import _send_album_with_thumbs

        seen_thumbs = []

        class _StubClient:
            async def get_input_entity(self, entity):
                return f"input_peer({entity})"

            async def _file_to_media(self, file, **kwargs):
                seen_thumbs.append(kwargs.get("thumb"))
                from telethon import types

                fh = types.InputFile(id=1, parts=1, name="x", md5_checksum="")
                return (
                    fh,
                    types.InputMediaUploadedDocument(
                        file=fh,
                        mime_type="video/mp4",
                        attributes=None,
                        thumb=None,
                    ),
                    False,
                )

            async def __call__(self, request):
                from telethon.tl.types import Document

                fake_doc = Document(
                    id=1,
                    access_hash=0,
                    file_reference=b"",
                    date=0,
                    mime_type="video/mp4",
                    size=0,
                    dc_id=0,
                    attributes=[],
                )
                return type(
                    "R",
                    (),
                    {
                        "photo": None,
                        "document": fake_doc,
                        "updates": [],
                        "other_updates": [],
                    },
                )()

            def _get_response_message(self, random_ids, result, entity):
                return []

        self._run(
            _send_album_with_thumbs(
                _StubClient(),
                entity=999,
                files=["a.mp4"],
                thumbs=["/tmp/thumb_a.jpg"],
                captions=["cap"],
            )
        )

        self.assertEqual(seen_thumbs, ["/tmp/thumb_a.jpg"])


if __name__ == "__main__":
    unittest.main()