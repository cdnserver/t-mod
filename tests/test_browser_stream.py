import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web

from modules.browser_stream import (
    BrowserStreamClient,
    BrowserStreamError,
    _color,
    browser_stream_authorized,
    browser_stream_embed,
    normalize_browser_stream_url,
)


ROOT = Path(__file__).resolve().parents[1]


class BrowserStreamDomainTests(unittest.TestCase):
    def test_invalid_embed_color_cannot_break_bot_startup(self) -> None:
        self.assertEqual(_color("MISSING_TEST_COLOR", 0x123456), 0x123456)

    def test_only_http_browser_urls_are_accepted(self) -> None:
        self.assertEqual(
            normalize_browser_stream_url("https://tvr.lat/consensus"),
            "https://tvr.lat/consensus",
        )
        self.assertIsNone(normalize_browser_stream_url(" "))
        for value in (
            "javascript:alert(1)",
            "file:///etc/passwd",
            "ftp://example.org/file",
            "not a URL",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_browser_stream_url(value)

    def test_administrators_manage_guild_and_configured_role_are_allowed(
        self,
    ) -> None:
        administrator = SimpleNamespace(
            guild_permissions=SimpleNamespace(
                administrator=True,
                manage_guild=False,
            ),
            roles=[],
        )
        manager = SimpleNamespace(
            guild_permissions=SimpleNamespace(
                administrator=False,
                manage_guild=True,
            ),
            roles=[],
        )
        configured_role = SimpleNamespace(
            guild_permissions=SimpleNamespace(
                administrator=False,
                manage_guild=False,
            ),
            roles=[SimpleNamespace(id=1488207163879985233)],
        )
        participant = SimpleNamespace(
            guild_permissions=SimpleNamespace(
                administrator=False,
                manage_guild=False,
            ),
            roles=[SimpleNamespace(id=1)],
        )

        self.assertTrue(browser_stream_authorized(administrator))
        self.assertTrue(browser_stream_authorized(manager))
        self.assertTrue(browser_stream_authorized(configured_role))
        self.assertFalse(browser_stream_authorized(participant))

    def test_status_embed_explains_the_emulated_client_boundary(self) -> None:
        embed = browser_stream_embed(
            {
                "state": "streaming",
                "active": True,
                "url": "https://tvr.lat",
                "account": "T-Mod Screen#0001",
                "voice_channel_id": "1488168605798633588",
            }
        )

        self.assertIn("тестовый Discord-клиент", embed.description)
        self.assertIn("user token", embed.footer.text)
        self.assertEqual(embed.fields[0].value, "https://tvr.lat")

        failed = browser_stream_embed(
            {
                "state": "idle",
                "active": False,
                "last_error": "Voice target is unknown",
            }
        )
        self.assertEqual(
            failed.fields[0].name,
            "Последняя ошибка клиента",
        )


class BrowserStreamClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.requests: list[dict[str, object]] = []

        async def status(request: web.Request) -> web.Response:
            self.requests.append(
                {
                    "method": request.method,
                    "authorization": request.headers.get("Authorization"),
                }
            )
            return web.json_response(
                {
                    "ok": True,
                    "state": "idle",
                    "active": False,
                }
            )

        async def command(request: web.Request) -> web.Response:
            body = await request.json()
            self.requests.append(
                {
                    "method": request.method,
                    "authorization": request.headers.get("Authorization"),
                    "body": body,
                }
            )
            return web.json_response(
                {
                    "ok": True,
                    "state": "starting",
                    "active": True,
                    **body,
                },
                status=202,
            )

        async def unavailable(_request: web.Request) -> web.Response:
            return web.json_response(
                {"ok": False, "error": "voice_target_required"},
                status=400,
            )

        app = web.Application()
        app.router.add_get("/status", status)
        app.router.add_post("/command", command)
        app.router.add_get("/unavailable", unavailable)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        sockets = self.site._server.sockets
        port = sockets[0].getsockname()[1]
        self.client = BrowserStreamClient(
            base_url=f"http://127.0.0.1:{port}",
            control_token="private-test-token",
            timeout_seconds=2,
        )

    async def asyncTearDown(self) -> None:
        await self.runner.cleanup()

    async def test_status_and_command_use_authenticated_internal_api(self) -> None:
        status = await self.client.status()
        command = await self.client.command(
            "start",
            url="https://tvr.lat",
            guild_id=1488166642985730188,
            voice_channel_id=1488168605798633588,
        )

        self.assertEqual(status["state"], "idle")
        self.assertEqual(command["state"], "starting")
        self.assertEqual(
            self.requests[0]["authorization"],
            "Bearer private-test-token",
        )
        body = self.requests[1]["body"]
        self.assertEqual(
            body,
            {
                "action": "start",
                "url": "https://tvr.lat",
                "guild_id": "1488166642985730188",
                "voice_channel_id": "1488168605798633588",
            },
        )

    async def test_missing_control_token_fails_before_network_request(
        self,
    ) -> None:
        client = BrowserStreamClient(
            base_url=self.client.base_url,
            control_token="",
            timeout_seconds=2,
        )

        with self.assertRaisesRegex(
            BrowserStreamError,
            "Ключ управления",
        ):
            await client.status()


class BrowserStreamPackagingTests(unittest.TestCase):
    def test_root_compose_keeps_the_emulator_optional_and_private(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        bot_service, emulator_service = compose.split(
            "  discord-browser-stream:",
            maxsplit=1,
        )

        self.assertIn("discord-browser-stream:", compose)
        self.assertIn("- browser-stream", compose)
        self.assertIn('context: ./discord-browser-stream', compose)
        self.assertIn('expose:\n      - "8790"', compose)
        self.assertNotIn('"8790:8790"', compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertIn("/health", compose)
        self.assertNotIn("browser-stream.env", bot_service)
        self.assertIn("browser-stream.env", emulator_service)
        self.assertNotIn("init: true", emulator_service)

    def test_windows_launcher_validates_separate_user_credentials(self) -> None:
        launcher = (ROOT / "run_windows.bat").read_text(encoding="utf-8")
        configurator = (
            ROOT / "configure_browser_stream_windows.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("COMPOSE_PROFILES=browser-stream", launcher)
        self.assertIn("configure_browser_stream_windows.ps1", launcher)
        self.assertIn("browser-stream.env", launcher)
        self.assertIn("BROWSER_STREAM_DISCORD_TOKEN", configurator)
        self.assertIn("$userToken -eq $botToken", configurator)
        self.assertIn("New-ControlToken", configurator)
        self.assertNotIn("PrivateKey", configurator)

    def test_node_client_keeps_browser_and_user_emulation_stack(self) -> None:
        package = json.loads(
            (ROOT / "discord-browser-stream/package.json").read_text(
                encoding="utf-8"
            )
        )
        source = (
            ROOT / "discord-browser-stream/src/index.js"
        ).read_text(encoding="utf-8")
        lifecycle = (
            ROOT / "discord-browser-stream/src/lifecycle.js"
        ).read_text(encoding="utf-8")
        lifecycle_tests = (
            ROOT / "discord-browser-stream/test/lifecycle.test.js"
        ).read_text(encoding="utf-8")
        entrypoint = (
            ROOT / "discord-browser-stream/docker-entrypoint.sh"
        ).read_text(encoding="utf-8")
        dockerfile = (
            ROOT / "discord-browser-stream/Dockerfile"
        ).read_text(encoding="utf-8")
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")

        dependencies = package["dependencies"]
        self.assertIn("discord.js-selfbot-v13", dependencies)
        self.assertIn("puppeteer-stream", dependencies)
        self.assertIn("@dank074/discord-video-stream", dependencies)
        self.assertIn('type: "go-live"', source)
        self.assertIn("launch({", source)
        self.assertIn("getStream(page", source)
        self.assertIn("--allowlisted-extension-id=", source)
        self.assertIn("BROWSER_STREAM_CAPTURE_STARTUP_DELAY_MS", source)
        self.assertIn("BROWSER_STREAM_CAPTURE_FOCUS_DELAY_MS", source)
        self.assertIn("BROWSER_STREAM_VOICE_CONNECT_TIMEOUT_MS", source)
        self.assertIn("await session.browser.newPage()", source)
        self.assertNotIn("pages[0]", source)
        self.assertIn("createLifecycleQueue", source)
        self.assertIn("Reusing voice connection", source)
        self.assertIn("session.cleanupPromise", source)
        self.assertIn("browserMedia.stop()", source)
        self.assertIn("voice_connection_timeout", lifecycle)
        self.assertIn("survives a rejection", lifecycle_tests)
        self.assertIn("BROWSER_STREAM_DISCORD_TOKEN", source)
        self.assertIn("timingSafeEqual", source)
        self.assertIn("BROWSER_STREAM_WIDTH", entrypoint)
        self.assertIn("-noreset", entrypoint)
        self.assertIn("Timed out waiting for Xvfb", entrypoint)
        self.assertIn("Xvfb stopped", entrypoint)
        self.assertIn("*.sh text eol=lf", attributes)
        self.assertIn("sed -i 's/\\r$//'", dockerfile)
        self.assertIn("mkdir -p /tmp/.X11-unix", dockerfile)
        self.assertIn("chmod 1777 /tmp/.X11-unix", dockerfile)
        self.assertIn('ENTRYPOINT ["/usr/bin/tini"', dockerfile)

    def test_pnpm_build_scripts_use_an_explicit_reviewed_allowlist(
        self,
    ) -> None:
        workspace = (
            ROOT / "discord-browser-stream/pnpm-workspace.yaml"
        ).read_text(encoding="utf-8")

        self.assertIn("allowBuilds:", workspace)
        for dependency in (
            "@lng2004/node-datachannel",
            "node-av",
            "puppeteer",
            "sharp",
            "zeromq",
        ):
            self.assertIn(dependency, workspace)
        self.assertNotIn("onlyBuiltDependencies", workspace)
        self.assertNotIn("dangerouslyAllowAllBuilds", workspace)

    def test_secrets_and_browser_state_are_excluded_from_git_and_build(
        self,
    ) -> None:
        root_dockerignore = (ROOT / ".dockerignore").read_text(
            encoding="utf-8"
        )
        gitignore = (
            ROOT / "discord-browser-stream/.gitignore"
        ).read_text(encoding="utf-8")
        dockerignore = (
            ROOT / "discord-browser-stream/.dockerignore"
        ).read_text(encoding="utf-8")

        for required in (".env", "data", "node_modules"):
            self.assertIn(required, gitignore)
            self.assertIn(required, dockerignore)
        self.assertIn("discord-browser-stream", root_dockerignore)
        self.assertIn(".env", root_dockerignore)

        common_env = (ROOT / ".env.persistent.example").read_text(
            encoding="utf-8"
        )
        emulator_env = (
            ROOT / "discord-browser-stream/.env.example"
        ).read_text(encoding="utf-8")
        self.assertNotIn("BROWSER_STREAM_DISCORD_TOKEN", common_env)
        self.assertIn("BROWSER_STREAM_DISCORD_TOKEN", emulator_env)


if __name__ == "__main__":
    unittest.main()
