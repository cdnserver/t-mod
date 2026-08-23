import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import storage
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from modules.atlas_tts import (
    AtlasTTSConfig,
    AtlasTTSResult,
    AtlasTTSService,
    atlas_tts_spoken_text,
)
from modules.atlas_web import register_atlas_web_routes
from modules.consensus_web_auth import ConsensusWebPrincipal


def tts_config(*, configured: bool = True) -> AtlasTTSConfig:
    return AtlasTTSConfig(
        enabled=configured,
        api_key="test-key" if configured else "",
        api_url="https://openrouter.test/api/v1/audio/speech",
        model="x-ai/grok-voice-tts-1.0",
        voices=("ara", "eve"),
        default_voice="ara",
        timeout_seconds=4,
        max_text_chars=240,
        max_audio_bytes=1024 * 1024,
        cache_ttl_seconds=300,
        cache_max_items=8,
        cache_max_bytes=2 * 1024 * 1024,
        concurrency=2,
    )


class AtlasTTSServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_is_coalesced_cached_and_markdown_is_spoken_cleanly(self) -> None:
        calls: list[tuple[str, str, float]] = []

        async def provider(text: str, voice: str, speed: float):
            calls.append((text, voice, speed))
            await asyncio.sleep(0.01)
            return b"ID3-audio", "audio/mpeg", "generation-1"

        service = AtlasTTSService(tts_config(), provider_request=provider)
        try:
            first, concurrent = await asyncio.gather(
                service.synthesize("**Стойте.** Проверьте статью [1, статья 2.6]."),
                service.synthesize("**Стойте.** Проверьте статью [1, статья 2.6]."),
            )
            cached = await service.synthesize(
                "**Стойте.** Проверьте статью [1, статья 2.6]."
            )
        finally:
            await service.close()

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "Стойте. Проверьте статью.")
        self.assertEqual(first.audio, b"ID3-audio")
        self.assertEqual(concurrent.audio, first.audio)
        self.assertTrue(cached.cache_hit)
        self.assertEqual(cached.generation_id, "generation-1")

    async def test_provider_failure_and_circuit_breaker_return_system_fallback(self) -> None:
        calls = 0

        async def provider(_text: str, _voice: str, _speed: float):
            nonlocal calls
            calls += 1
            raise aiohttp.ClientConnectionError("offline")

        service = AtlasTTSService(tts_config(), provider_request=provider)
        try:
            results = [
                await service.synthesize(f"Ответ номер {index}")
                for index in range(4)
            ]
        finally:
            await service.close()

        self.assertEqual(calls, 3)
        self.assertTrue(all(item.fallback for item in results))
        self.assertEqual(results[-1].provider, "system")
        self.assertEqual(results[-1].reason, "provider_recovering")

    async def test_unconfigured_provider_and_invalid_voice_are_safe(self) -> None:
        service = AtlasTTSService(tts_config(configured=False))
        try:
            fallback = await service.synthesize("Короткий ответ")
            with self.assertRaisesRegex(ValueError, "atlas_tts_voice_invalid"):
                await service.synthesize("Короткий ответ", voice="unknown")
        finally:
            await service.close()

        self.assertTrue(fallback.fallback)
        self.assertEqual(fallback.reason, "not_configured")
        self.assertEqual(service.voices_payload()["provider"], "system")
        self.assertEqual(service.voices_payload()["voices"], [])

    def test_spoken_text_has_hard_bound_without_losing_link_label(self) -> None:
        source = "[Правило](https://example.test) " + "слово " * 100
        spoken = atlas_tts_spoken_text(source, max_chars=90)
        self.assertTrue(spoken.startswith("Правило"))
        self.assertNotIn("https://", spoken)
        self.assertLessEqual(len(spoken), 91)
        self.assertTrue(spoken.endswith("…"))


class AtlasTTSWebTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.old_data_dir = storage.DATA_DIR
        self.old_database_file = storage.DATABASE_FILE
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp_dir.name)
        storage.DATABASE_FILE = storage.DATA_DIR / "atlas-tts-test.db"
        storage.init_db()

    def tearDown(self) -> None:
        storage.DATA_DIR = self.old_data_dir
        storage.DATABASE_FILE = self.old_database_file
        self.temp_dir.cleanup()

    async def test_voice_catalog_audio_and_explicit_system_fallback_contract(self) -> None:
        selected = ConsensusWebPrincipal(
            user_id=42,
            guild_id=77,
            display_name="Администратор",
            csrf_token="tts-csrf",
            member=SimpleNamespace(
                id=42,
                display_name="Администратор",
                guild_permissions=SimpleNamespace(administrator=True),
                roles=[],
            ),
        )

        async def authenticate(_request):
            return selected, False

        fake = SimpleNamespace(
            voices_payload=lambda: {
                "configured": True,
                "provider": "openrouter",
                "default_voice": "ara",
                "voices": [{"id": "ara", "name": "Ara"}],
                "formats": ["mp3"],
                "fallback": {"provider": "system", "client_side": True},
            },
            synthesize=AsyncMock(
                return_value=AtlasTTSResult(
                    audio=b"ID3-test",
                    content_type="audio/mpeg",
                    provider="openrouter",
                    model="x-ai/grok-voice-tts-1.0",
                    voice="ara",
                    generation_id="gen-web",
                )
            ),
            preview=AsyncMock(
                return_value=AtlasTTSResult(
                    audio=None,
                    content_type="",
                    provider="system",
                    model="x-ai/grok-voice-tts-1.0",
                    voice="ara",
                    fallback=True,
                    reason="provider_unavailable",
                )
            ),
            close=AsyncMock(),
        )
        app = web.Application()
        with patch("modules.atlas_web.AtlasTTSService", return_value=fake):
            register_atlas_web_routes(
                app,
                SimpleNamespace(get_guild=lambda _guild_id: None),
                guild_id=77,
                asset_dir=Path(__file__).resolve().parents[1] / "web" / "atlas",
                authenticate=authenticate,
            )

        async with TestClient(TestServer(app)) as client:
            voices = await client.get("/api/atlas/overlay/tts/voices")
            rejected = await client.post(
                "/api/atlas/overlay/tts/synthesize",
                json={"text": "Что делать?"},
            )
            audio = await client.post(
                "/api/atlas/overlay/tts/synthesize",
                json={"text": "Что делать?", "voice": "ara", "speed": 1.05},
                headers={"X-CSRF-Token": "tts-csrf"},
            )
            preview = await client.post(
                "/api/atlas/overlay/tts/preview",
                json={"voice": "ara"},
                headers={"X-CSRF-Token": "tts-csrf"},
            )
            voices_payload = await voices.json()
            audio_payload = await audio.read()

        self.assertEqual(voices.status, 200)
        self.assertEqual(voices_payload["default_voice"], "ara")
        self.assertEqual(rejected.status, 403)
        self.assertEqual(audio.status, 200)
        self.assertEqual(audio_payload, b"ID3-test")
        self.assertEqual(audio.headers["X-Atlas-TTS-Generation"], "gen-web")
        self.assertEqual(preview.status, 204)
        self.assertEqual(preview.headers["X-Atlas-TTS-Fallback"], "system")
        fake.synthesize.assert_awaited_once()
        fake.preview.assert_awaited_once_with("ara")
        fake.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
