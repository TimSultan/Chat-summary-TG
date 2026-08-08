"""Tests for the standalone Post Stats launcher only."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

import poststats_server


class StandalonePostStatsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.owner_path = Path(self.temporary.name) / "poststats_owner.json"
        self.owner_patch = patch.object(poststats_server, "OWNER_PATH", self.owner_path)
        self.data_patch = patch.object(poststats_server, "DATA_DIR", Path(self.temporary.name))
        self.owner_patch.start()
        self.data_patch.start()
        self.addAsyncCleanup(self._cleanup)
        self.client = TestClient(TestServer(await poststats_server.create_app()))
        await self.client.start_server()

    async def _cleanup(self):
        await self.client.close()
        self.data_patch.stop()
        self.owner_patch.stop()
        self.temporary.cleanup()

    async def test_fresh_service_has_a_setup_page(self):
        response = await self.client.get("/setup")
        self.assertEqual(response.status, 200)
        self.assertIn("Set up Post Stats", await response.text())

    async def test_fresh_poststats_ui_remains_unconfigured_until_setup(self):
        response = await self.client.get("/poststats")
        self.assertEqual(response.status, 200)
        self.assertIn("Post Stats", await response.text())

    async def test_setup_rejects_requests_without_the_log_only_code(self):
        response = await self.client.post(
            "/setup/send-code",
            json={"api_id": "1", "api_hash": "hash", "phone": "+441234567890"},
        )
        self.assertEqual(response.status, 403)
        self.assertEqual((await response.json())["error"], "Incorrect setup code.")


if __name__ == "__main__":
    unittest.main()
