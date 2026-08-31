from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from modules import global_log_web
from persistence import global_log_repository as repository


class GlobalLogWebSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def _client(self):
        from aiohttp import web

        app = web.Application()
        global_log_web.register_global_log_web_routes(
            app,
            guild_id=10,
            asset_dir=Path("/tmp"),
        )
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def test_client_event_requires_authenticated_session(self) -> None:
        client = await self._client()
        with patch.object(repository, "resolve_session", return_value=None), patch.object(
            global_log_web, "emit_global_event"
        ) as emit:
            response = await client.post(
                "/api/global-log/client",
                headers={"Origin": "https://tvr.lat"},
                json={"event": "button_clicked"},
            )
        self.assertEqual(response.status, 401)
        emit.assert_not_called()

    async def test_global_ban_blocks_global_log_api_even_with_valid_session(self) -> None:
        client = await self._client()
        with patch.object(
            repository,
            "resolve_session",
            return_value={"session_id": 1, "user_id": 20},
        ), patch.object(
            global_log_web.global_ban_storage,
            "is_globally_banned",
            return_value=True,
        ):
            response = await client.get(
                "/api/global-log/events",
                cookies={"tmod_global_log_session": "valid"},
            )
        self.assertEqual(response.status, 423)
        self.assertEqual((await response.json())["error"], "global_log_access_blocked")

    def test_forwarded_for_is_ignored_from_untrusted_peer(self) -> None:
        request = SimpleNamespace(
            remote="8.8.8.8",
            headers={"X-Forwarded-For": "1.1.1.1"},
        )
        self.assertEqual(global_log_web._remote(request), "8.8.8.8")

    def test_forwarded_for_is_used_only_from_configured_proxy(self) -> None:
        request = SimpleNamespace(
            remote="127.0.0.1",
            headers={"X-Forwarded-For": "1.1.1.1"},
        )
        self.assertEqual(global_log_web._remote(request), "1.1.1.1")
