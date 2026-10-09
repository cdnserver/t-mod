"""Exercise the real Atlas web wiring/history with stubbed paid providers."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import storage
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from modules.atlas_web import register_atlas_web_routes
from modules.consensus_web_auth import ConsensusWebPrincipal
from tests.test_atlas_call_socket import wav


class AtlasVoiceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_socket_uses_real_atlas_brain_scoped_history_and_ledger(self):
        old_dir, old_file = storage.DATA_DIR, storage.DATABASE_FILE
        with tempfile.TemporaryDirectory() as directory:
            storage.DATA_DIR = Path(directory)
            storage.DATABASE_FILE = storage.DATA_DIR / "voice.db"
            storage.init_db()
            try:
                selected = ConsensusWebPrincipal(user_id=42, guild_id=77,
                    display_name="Test user", csrf_token="voice-csrf",
                    member=SimpleNamespace(id=42, display_name="Test user", roles=[],
                        guild_permissions=SimpleNamespace(administrator=True)))
                async def authenticate(request):
                    return selected, False
                engine = SimpleNamespace(configured=True, tts_model="test-tts", stt_model="test-stt",
                    close=AsyncMock(), transcribe=AsyncMock(return_value={"text":"Как подать жалобу?", "model":"test-stt","usage":{}}),
                    speak=AsyncMock(return_value={"audio":b"ID3"+b"a"*100,"model":"test-tts","usage":{}}))
                histories = []
                async def answer(organization, question, **kwargs):
                    self.assertEqual(kwargs["conversation_mode"],"voice")
                    self.assertEqual(kwargs["server_code"],"phoenix-15")
                    histories.append(kwargs["history"])
                    await kwargs["on_progress"]({"phase":"retrieval"})
                    await kwargs["on_delta"]("Сначала сверьте правила форума. ")
                    await kwargs["on_delta"]("Затем заполните форму жалобы.")
                    return {"answer":"Сначала сверьте правила форума. Затем заполните форму жалобы.",
                        "project_code":"majestic", "server_code":"phoenix-15", "faction_code":"lspd",
                        "citations":[],"model":"test-model","model_provider":"openrouter",
                        "model_release":"test", "latency_ms":1,"usage":{}}
                app=web.Application()
                with patch("modules.atlas_voice_web.AtlasCallSpeech",return_value=engine), \
                     patch("modules.atlas_web.atlas_answer_stream",side_effect=answer), \
                     patch("modules.atlas_web.billing_storage.atlas_ai_entitlement",return_value={"allowed":True}), \
                     patch("modules.atlas_web.billing_storage.atlas_record_ai_usage",return_value={}) as ledger:
                    register_atlas_web_routes(app,SimpleNamespace(get_guild=lambda _:None),guild_id=77,
                        asset_dir=Path(__file__).resolve().parents[1]/"web"/"atlas",authenticate=authenticate)
                    async with TestClient(TestServer(app)) as client:
                        config=await client.get("/api/atlas/call/config")
                        self.assertEqual((await config.json())["transport"],"websocket")
                        ticket=await client.post("/api/atlas/call/ticket",headers={"X-CSRF-Token":"voice-csrf"})
                        self.assertEqual(ticket.status,200)
                        ws=await client.ws_connect("/api/atlas/call/ws")
                        await ws.send_json(await ticket.json());await ws.receive_json(timeout=3)
                        thread_ids=[]
                        for _ in range(2):
                            await ws.send_bytes(wav())
                            while True:
                                event=await ws.receive_json(timeout=5)
                                self.assertNotEqual(event["type"],"error",event)
                                if event["type"] == "complete":
                                    thread_ids.append(event["threadId"]);break
                        self.assertEqual(thread_ids[0],thread_ids[1])
                        self.assertEqual(histories[0],[])
                        self.assertEqual([line["role"] for line in histories[1]],["user","assistant"])
                        sources=[call.kwargs["source"] for call in ledger.call_args_list]
                        self.assertEqual(sources.count("blackbird-call"),2)
                        self.assertEqual(sources.count("blackbird-call-transcribe"),2)
                        self.assertEqual(sources.count("blackbird-call-speak"),4)
                        await ws.close()
                engine.close.assert_awaited_once()
            finally:
                storage.DATA_DIR, storage.DATABASE_FILE = old_dir, old_file
