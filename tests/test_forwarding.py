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
    def __init__(self, chat_id, message_id, grouped_id=None):
        self.chat_id = chat_id
        self.id = message_id
        self.grouped_id = grouped_id
        self.is_reply = False
        self.reply_to_msg_id = None


class FloodWaitError(Exception):
    def __init__(self, seconds):
        super().__init__(f"FLOOD_WAIT_{seconds}")
        self.seconds = seconds


class ChatWriteForbiddenError(Exception):
    pass


class ChatForwardsRestrictedError(Exception):
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


if __name__ == "__main__":
    unittest.main()