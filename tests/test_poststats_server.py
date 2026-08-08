"""The standalone /poststats owner-setup entry point."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

import poststats_server


class PostStatsSetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp.name) / "poststats_owner.json"
        self.config_patch = patch.object(poststats_server, "CONFIG_PATH", self.config_path)
        self.data_patch = patch.object(poststats_server, "DATA_DIR", Path(self.temp.name))
        self.config_patch.start()
        self.data_patch.start()
        self.addAsyncCleanup(self._cleanup)
        app = await poststats_server.create_app()
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def _cleanup(self):
        await self.client.close()
        self.data_patch.stop()
        self.config_patch.stop()
        self.temp.cleanup()

    async def test_fresh_deployment_redirects_poststats_to_setup_page(self):
        response = await self.client.get("/poststats", allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["Location"], "/poststats/setup")

        setup = await self.client.get("/poststats/setup")
        self.assertEqual(setup.status, 200)
        self.assertIn("Set up Post Stats", await setup.text())

    async def test_setup_api_rejects_requests_without_log_only_code(self):
        response = await self.client.post(
            "/poststats/setup/send-code",
            json={"api_id": "1", "api_hash": "hash", "phone": "+441234567890"},
        )
        self.assertEqual(response.status, 403)
        self.assertEqual((await response.json())["error"], "Incorrect setup code.")

    async def test_setup_api_rejects_a_wrong_code_without_contacting_telegram(self):
        response = await self.client.post(
            "/poststats/setup/send-code",
            headers={"X-PostStats-Setup-Code": "wrong"},
            json={"api_id": "1", "api_hash": "hash", "phone": "+441234567890"},
        )
        self.assertEqual(response.status, 403)


if __name__ == "__main__":
    unittest.main()
