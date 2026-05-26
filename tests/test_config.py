import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from tgcf.config import Forward, load_from_to


class LoadFromToTest(unittest.TestCase):
    def test_keeps_integer_ids_unchanged(self):
        forwards = [
            Forward(
                use_this=True,
                source=-1003881527625,
                dest=[-1003802732882],
                offset=0,
                end=0,
                con_name="",
            )
        ]

        fake_client = object()
        mapping = asyncio.run(load_from_to(fake_client, forwards))

        self.assertIn(-1003881527625, mapping)
        self.assertEqual(mapping[-1003881527625], [-1003802732882])

    def test_resolves_non_integer_peers_via_get_id(self):
        forwards = [
            Forward(
                use_this=True,
                source="source_username",
                dest=["dest_username"],
                offset=0,
                end=0,
                con_name="",
            )
        ]

        fake_client = object()
        resolver = AsyncMock(side_effect=[-100100, -100200])

        with patch("tgcf.config.get_id", resolver):
            mapping = asyncio.run(load_from_to(fake_client, forwards))

        self.assertIn(-100100, mapping)
        self.assertEqual(mapping[-100100], [-100200])
        self.assertEqual(resolver.await_count, 2)


if __name__ == "__main__":
    unittest.main()