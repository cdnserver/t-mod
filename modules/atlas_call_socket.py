"""Owned voice sessions over WebSocket. Credentials never enter socket URLs."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import secrets
import time
from collections import OrderedDict, deque
from typing import Any

from aiohttp import web, WSMsgType
from modules.atlas_voice import MAX_WAV, VOICES, validate_call_wav
from modules.consensus_web_auth import csrf_matches

logger = logging.getLogger(__name__)


def register_call_socket(app: web.Application, *, authorize: Any, dashboard: Any,
                         brain: Any, meter: Any, engine: Any) -> None:
    tickets: OrderedDict[str, tuple[float, web.Request, Any, dict]] = OrderedDict()
    users: set[int] = set()
    sockets: set[web.WebSocketResponse] = set()
    accounting: set[asyncio.Task] = set()
    pending = 0

    async def ticket(request: web.Request) -> web.Response:
        selected = await authorize(request)
        if not csrf_matches(request, selected):
            raise web.HTTPForbidden()
        if not engine.configured:
            raise web.HTTPServiceUnavailable(text="voice_not_configured")
        if int(selected.user_id) in users:
            raise web.HTTPConflict(text="call_already_active")
        now = time.monotonic()
        for key, entry in list(tickets.items()):
            if entry[0] < now or int(entry[2].user_id) == int(selected.user_id):
                tickets.pop(key, None)
        while len(tickets) >= 256:
            tickets.popitem(last=False)
        view = await dashboard(request, selected)
        token = secrets.token_urlsafe(32)
        tickets[token] = (now + 30, request, selected, view)
        return web.json_response({"call_token": token}, headers={"Cache-Control": "private, no-store"})

    async def socket(request: web.Request) -> web.WebSocketResponse:
        nonlocal pending
        if request.query_string:
            raise web.HTTPBadRequest(text="call_credentials_must_not_be_in_url")
        if len(sockets) + pending >= 32:
            raise web.HTTPTooManyRequests()
        origin = request.headers.get("Origin", "")
        if origin and origin not in {"https://dash.tvr.lat", "null"}:
            raise web.HTTPForbidden()
        pending += 1
        ws = web.WebSocketResponse(heartbeat=15, max_msg_size=MAX_WAV)
        uid: int | None = None
        task: asyncio.Task | None = None
        watchdog: asyncio.Task | None = None
        turn = 0
        voice = "george"
        thread_id: int | None = None
        rates: deque[float] = deque()
        authenticated = False
        greeted = False
        call_id = secrets.token_hex(16)

        async def emit(event: dict, generation: int) -> None:
            if not ws.closed and turn == generation:
                async with asyncio.timeout(8):
                    await ws.send_json({"callId": call_id, "turn": generation, **event})

        async def cancel() -> None:
            nonlocal turn, task
            turn += 1
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                task = None

        try:
            await ws.prepare(request)
            hello = await ws.receive(timeout=5)
            if hello.type != WSMsgType.TEXT or len(hello.data) > 256:
                await ws.close(code=1008, message=b"authentication_required")
                return ws
            payload = json.loads(hello.data)
            if not isinstance(payload, dict):
                raise ValueError("invalid_hello")
            entry = tickets.pop(str(payload.get("call_token") or ""), None)
            if not entry or entry[0] < time.monotonic():
                await ws.close(code=1008, message=b"ticket_expired")
                return ws
            _, auth_request, selected, view = entry
            await authorize(auth_request)
            uid = int(selected.user_id)
            if uid in users:
                await ws.close(code=1008, message=b"call_already_active")
                uid = None
                return ws
            users.add(uid)
            authenticated = True
            pending -= 1
            sockets.add(ws)
            organization = int(view["organization"]["id"])
            await ws.send_json({"type": "ready", "callId": call_id,
                "voices": [{"id": key, "label": label} for key, label in VOICES.items()],
                "default_voice": voice, "max_call_seconds": 1800,
                "transport": "websocket", "realtime_stt": False})

            async def monitor() -> None:
                deadline = time.monotonic() + 1800
                try:
                    while not ws.closed:
                        await asyncio.sleep(5)
                        await authorize(auth_request)
                        if time.monotonic() > deadline:
                            await ws.close(code=1000, message=b"call_time_limit")
                except web.HTTPException:
                    await ws.close(code=1008, message=b"access_revoked")
                except Exception:
                    await ws.close(code=1011, message=b"access_check_failed")
            watchdog = asyncio.create_task(monitor())

            async def account(result: dict, action: str) -> None:
                # Once a provider result has arrived, interruption must not lose
                # its ledger write. Keep ownership of this bounded task even if
                # the user has already started their next utterance.
                async def write() -> None:
                    async with asyncio.timeout(15):
                        await meter(selected, organization, result, action)
                job = asyncio.create_task(write())
                accounting.add(job)
                def finished(done: asyncio.Task) -> None:
                    accounting.discard(done)
                    if not done.cancelled() and done.exception() is not None:
                        logger.warning("atlas_call_meter_failed call_id=%s action=%s", call_id, action)
                job.add_done_callback(finished)
                await asyncio.shield(job)

            async def speak(text: str, generation: int, chosen: str) -> None:
                await authorize(auth_request)
                result = await engine.speak(text, chosen)
                # Completed computation is accounted before sending any audio.
                await account(result, "speak")
                await authorize(auth_request)
                await emit({"type": "audio", "text": text,
                    "audio_base64": base64.b64encode(result["audio"]).decode("ascii")}, generation)

            async def run_turn(audio: bytes | None, generation: int, chosen: str) -> None:
                nonlocal thread_id
                try:
                    async with asyncio.timeout(120):
                        await authorize(auth_request)
                        if audio is None:
                            await speak("Я на связи. Расскажи, чем могу помочь.", generation, chosen)
                        else:
                            await emit({"type": "stage", "stage": "thinking", "label": "Слушаю вашу реплику"}, generation)
                            result = await engine.transcribe(audio)
                            await account(result, "transcribe")
                            await authorize(auth_request)
                            question = result["text"].strip()
                            if question:
                                await emit({"type": "transcript", "role": "user", "text": question}, generation)
                                # Speech synthesis follows the model stream, not its final essay.
                                speech_queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=12)
                                async def speaker() -> None:
                                    while (phrase := await speech_queue.get()) is not None:
                                        await speak(phrase, generation, chosen)
                                async def add_phrase(phrase: str) -> None:
                                    await speech_queue.put(phrase)
                                async with asyncio.TaskGroup() as group:
                                    group.create_task(speaker())
                                    thread_id = await brain(auth_request, selected, view, question, thread_id,
                                        lambda event: emit(event, generation), add_phrase)
                                    await speech_queue.put(None)
                        await emit({"type": "complete", "threadId": thread_id}, generation)
                except asyncio.CancelledError:
                    raise
                except web.HTTPException as exc:
                    await emit({"type": "error", "fatal": True,
                        "message": "Звонок завершён: проверьте авторизацию, доступ и баланс Atlas."}, generation)
                    await ws.close(code=1008, message=b"access_revoked")
                except Exception as exc:
                    # Never log microphone payloads, questions or credentials.
                    logger.warning("atlas_call_turn_failed call_id=%s error_type=%s", call_id, type(exc).__name__)
                    await emit({"type": "error", "message": "Связь прервалась. Повторите фразу или перезвоните."}, generation)

            async for message in ws:
                if message.type not in {WSMsgType.TEXT, WSMsgType.BINARY}:
                    break
                now = time.monotonic()
                while rates and now - rates[0] > 60:
                    rates.popleft()
                if len(rates) >= 120:
                    await ws.close(code=1008, message=b"rate_limited")
                    break
                rates.append(now)
                if message.type == WSMsgType.TEXT:
                    if len(message.data) > 512:
                        raise ValueError("control_message_size")
                    command = json.loads(message.data)
                    if not isinstance(command, dict):
                        raise ValueError("invalid_command")
                    kind = command.get("type")
                    if kind == "end":
                        break
                    if kind == "interrupt":
                        await cancel()
                        await emit({"type": "stage", "stage": "hearing"}, turn)
                        continue
                    if kind == "voice":
                        if command.get("voice") not in VOICES:
                            raise ValueError("voice_unknown")
                        voice = command["voice"]
                        continue
                    if kind != "greet":
                        raise ValueError("command_unknown")
                    if greeted:  # Greeting is once per connection.
                        continue
                    greeted = True
                    audio = None
                else:
                    audio = bytes(message.data)
                    validate_call_wav(audio)
                await cancel()
                task = asyncio.create_task(run_turn(audio, turn, voice))
        except (ValueError, TypeError, TimeoutError, web.HTTPException):
            await ws.close(code=1008, message=b"invalid_call")
        finally:
            if not authenticated:
                pending -= 1
            if authenticated and uid is not None:
                users.discard(uid)
            sockets.discard(ws)
            if watchdog:
                watchdog.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watchdog
            await cancel()
            if not ws.closed:
                await ws.close()
        return ws

    async def shutdown(_: web.Application) -> None:
        await asyncio.gather(*(ws.close(code=1001, message=b"server_shutdown") for ws in list(sockets)))

    async def finish_accounting(_: web.Application) -> None:
        await asyncio.gather(*list(accounting), return_exceptions=True)

    app.on_shutdown.append(shutdown)
    app.on_cleanup.append(finish_accounting)
    app.router.add_post("/api/atlas/call/ticket", ticket)
    app.router.add_get("/api/atlas/call/ws", socket)
