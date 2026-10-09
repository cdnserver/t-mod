import asyncio
import io
import wave
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web, WSMsgType, WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer
from modules.atlas_call_socket import register_call_socket
from modules.atlas_voice import call_agent_name, cost_microusd, pop_voice_phrase, validate_call_wav


def wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 16000)
    return output.getvalue()


class CallSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.revoked = False
        self.preload_body = False
        self.selected = SimpleNamespace(user_id=72, csrf_token="secret")
        async def authorize(request):
            if self.revoked or request.headers.get("X-Test-Account") != "yes":
                raise web.HTTPUnauthorized()
            return self.selected
        self.engine = SimpleNamespace(configured=True,
            transcribe=AsyncMock(return_value={"text":"Как дела?","model":"stt","usage":{}}),
            speak=AsyncMock(return_value={"audio":b"ID3"+b"a"*100,"model":"tts","usage":{}}))
        self.meter = AsyncMock()
        async def brain(request, selected, view, question, thread, emit, speak):
            await emit({"type":"transcript","role":"assistant","text":"Я рядом.","partial":False})
            await speak("Я рядом.")
            return 123
        self.brain = AsyncMock(side_effect=brain)
        @web.middleware
        async def observability(request, handler):
            if self.preload_body:
                await request.read()
            return await handler(request)
        app = web.Application(middlewares=[observability])
        register_call_socket(app, authorize=authorize, dashboard=AsyncMock(return_value={"organization":{"id":7}}),
            brain=self.brain, meter=self.meter, engine=self.engine)
        self.client = TestClient(TestServer(app)); await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def token(self):
        response = await self.client.post("/api/atlas/call/ticket", headers={"X-Test-Account":"yes","X-CSRF-Token":"secret"})
        self.assertEqual(response.status, 200)
        return (await response.json())["call_token"]

    async def connect(self):
        token = await self.token()
        ws = await self.client.ws_connect("/api/atlas/call/ws")
        await ws.send_json({"call_token":token})
        ready = await ws.receive_json(timeout=2)
        self.assertEqual(ready["type"], "ready")
        self.assertEqual(ready["transport"], "websocket")
        return ws, token

    async def until(self, ws, kind):
        events = []
        while True:
            event = await ws.receive_json(timeout=2); events.append(event)
            if event["type"] == kind:
                return events

    async def test_ticket_requires_account_and_csrf(self):
        self.assertEqual((await self.client.post("/api/atlas/call/ticket")).status, 401)
        self.assertEqual((await self.client.post("/api/atlas/call/ticket",headers={"X-Test-Account":"yes"})).status,403)

    async def test_custom_name_is_authenticated_validated_and_returned(self):
        headers={"X-Test-Account":"yes","X-CSRF-Token":"secret"}
        for name in ("<script>","","123","a"*41):
            response=await self.client.post("/api/atlas/call/ticket",headers=headers,json={"agent_name":name})
            self.assertEqual(response.status,400)
        response=await self.client.post("/api/atlas/call/ticket",headers=headers,json={"agent_name":"  Алиса  "})
        ws=await self.client.ws_connect("/api/atlas/call/ws")
        await ws.send_json(await response.json())
        self.assertEqual((await ws.receive_json(timeout=2))["agent_name"],"Алиса")
        await ws.send_json({"type":"greet"});await self.until(ws,"complete")
        self.assertIn("Алиса",self.engine.speak.call_args.args[0])
        await ws.close()

    async def test_ticket_is_single_use(self):
        ws, token = await self.connect()
        second = await self.client.ws_connect("/api/atlas/call/ws")
        await second.send_json({"call_token":token})
        self.assertEqual((await second.receive(timeout=2)).type, WSMsgType.CLOSE)
        await ws.close()

    async def test_name_survives_pre_read_request_and_cached_size_remains_bounded(self):
        self.preload_body = True
        headers = {"X-Test-Account":"yes", "X-CSRF-Token":"secret"}
        invalid = await self.client.post("/api/atlas/call/ticket", headers=headers, json={"agent_name":"<bad>"})
        self.assertEqual(invalid.status, 400)
        oversized = await self.client.post("/api/atlas/call/ticket", headers=headers, data=b" "*513)
        self.assertEqual(oversized.status, 400)
        response = await self.client.post("/api/atlas/call/ticket", headers=headers, json={"agent_name":"Алиса"})
        self.assertEqual(response.status, 200)
        ws = await self.client.ws_connect("/api/atlas/call/ws")
        await ws.send_json(await response.json())
        self.assertEqual((await ws.receive_json(timeout=2))["agent_name"], "Алиса")
        await ws.close()

    async def test_ticket_credentials_are_omitted_from_observability_payloads(self):
        from modules.global_log_runtime import _request_payload, _response_payload
        from aiohttp.test_utils import make_mocked_request
        request = make_mocked_request("POST", "/api/atlas/call/ticket")
        self.assertEqual(await _request_payload(request), {"omitted":"authentication_secrets"})
        self.assertEqual(_response_payload(web.json_response({"call_token":"private"}), request.path), {"omitted":"authentication_secrets"})

    async def test_arbitrary_origin_is_rejected_before_upgrade(self):
        with self.assertRaises(WSServerHandshakeError) as error:
            await self.client.ws_connect("/api/atlas/call/ws", headers={"Origin":"https://malicious.example"})
        self.assertEqual(error.exception.status, 403)

    async def test_only_one_socket_is_allowed_for_an_account(self):
        ws, _ = await self.connect()
        response = await self.client.post("/api/atlas/call/ticket", headers={"X-Test-Account":"yes","X-CSRF-Token":"secret"})
        self.assertEqual(response.status, 409)
        await ws.close()

    async def test_voice_switch_and_greeting_are_validated_and_greeting_is_once(self):
        ws, _ = await self.connect()
        await ws.send_json({"type":"greet"})
        await self.until(ws,"complete")
        await ws.send_json({"type":"greet"})
        await ws.send_json({"type":"voice","voice":"untrusted-clone"})
        self.assertEqual((await ws.receive(timeout=2)).type,WSMsgType.CLOSE)
        self.engine.speak.assert_awaited_once()

    async def test_voice_question_runs_scoped_brain_and_meters_both_audio_requests(self):
        ws, _ = await self.connect()
        await ws.send_json({"type":"voice","voice":"sarah"})
        await ws.send_bytes(wav())
        events = await self.until(ws, "complete")
        self.assertTrue(any(event["type"]=="audio" for event in events))
        self.assertEqual(events[-1]["threadId"], 123)
        self.assertEqual(self.brain.call_args.args[3], "Как дела?")
        self.engine.speak.assert_awaited_once_with("Я рядом.","sarah")
        self.assertEqual(self.meter.await_count,2)
        await ws.close()

    async def test_interruption_cancels_provider_and_drops_old_turn_output(self):
        started = asyncio.Event(); cancelled = asyncio.Event()
        async def slow(audio):
            started.set()
            try: await asyncio.sleep(20)
            finally: cancelled.set()
        self.engine.transcribe.side_effect = slow
        ws, _ = await self.connect()
        await ws.send_bytes(wav()); await started.wait()
        await ws.send_json({"type":"interrupt"})
        event = (await self.until(ws,"stage"))[-1]
        if event["stage"] != "hearing": event = (await self.until(ws,"stage"))[-1]
        self.assertEqual(event["stage"],"hearing")
        await asyncio.wait_for(cancelled.wait(),2)
        self.brain.assert_not_awaited()
        self.meter.assert_not_awaited()
        await ws.close()

    async def test_access_revocation_prevents_further_provider_work(self):
        ws, _ = await self.connect(); self.revoked = True
        await ws.send_bytes(wav())
        event = (await self.until(ws,"error"))[-1]
        self.assertTrue(event["fatal"])
        self.engine.transcribe.assert_not_awaited()
        await ws.close()

    async def test_interrupt_does_not_lose_completed_provider_accounting(self):
        entered=asyncio.Event();release=asyncio.Event();recorded=asyncio.Event()
        async def meter(*args):
            entered.set();await release.wait();recorded.set()
        self.meter.side_effect=meter
        ws,_=await self.connect();await ws.send_bytes(wav())
        await asyncio.wait_for(entered.wait(),2)
        await ws.send_json({"type":"interrupt"})
        event=(await self.until(ws,"stage"))[-1]
        if event["stage"] != "hearing":event=(await self.until(ws,"stage"))[-1]
        self.assertEqual(event["stage"],"hearing")
        release.set();await asyncio.wait_for(recorded.wait(),2)
        self.meter.assert_awaited_once();self.brain.assert_not_awaited()
        await ws.close()

    async def test_malformed_control_and_url_credentials_are_rejected(self):
        with self.assertRaises(WSServerHandshakeError) as error:
            await self.client.ws_connect("/api/atlas/call/ws?call_token=forbidden")
        self.assertEqual(error.exception.status,400)
        ws,_=await self.connect();await ws.send_json(["not-a-command"])
        self.assertEqual((await ws.receive(timeout=2)).type,WSMsgType.CLOSE)
        self.engine.speak.assert_not_awaited()

    async def test_malformed_audio_is_closed_without_provider_calls(self):
        ws, _ = await self.connect(); await ws.send_bytes(b"not an audio file")
        self.assertEqual((await ws.receive(timeout=2)).type,WSMsgType.CLOSE)
        self.engine.transcribe.assert_not_awaited()

    def test_audio_and_cost_are_validated(self):
        self.assertEqual(call_agent_name(" Алиса   Норд "),"Алиса Норд")
        self.assertEqual(validate_call_wav(wav()),1)
        self.assertEqual(cost_microusd("0.0000001"),1)
        for value in (None,"NaN","Infinity",-1,11):
            with self.assertRaises(ValueError): cost_microusd(value)

    def test_phrase_boundaries_preserve_legal_references_and_streamed_punctuation(self):
        self.assertIsNone(pop_voice_phrase("Сначала сверьте требования статьи."))
        self.assertIsNone(pop_voice_phrase("Проверяем нарушение по ст. "))
        self.assertIsNone(pop_voice_phrase("Проверяем нарушение по ст. 2.6 и п. "))
        self.assertIsNone(pop_voice_phrase("Сначала сверьте [1, статья 2. "))
        segment=pop_voice_phrase("Проверяем нарушение по ст. 2.6 и п. 1.4. Затем уточняем факты.")
        self.assertEqual(segment,("Проверяем нарушение по ст. 2.6 и п. 1.4.","Затем уточняем факты."))
