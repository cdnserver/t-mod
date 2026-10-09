"""Authenticated call configuration. Shared Atlas stream remains the brain."""
from __future__ import annotations
from typing import Any
from aiohttp import web
from modules.atlas_voice import AtlasCallSpeech, VOICES
from modules.atlas_call_socket import register_call_socket


def register_atlas_voice_routes(app: web.Application, *, principal: Any, require_desktop: Any, require_atlas: Any, require_balance: Any, dashboard: Any, meter: Any, brain: Any, speech: AtlasCallSpeech | None = None) -> None:
    engine = speech or AtlasCallSpeech()

    async def authorize(request: web.Request) -> Any:
        require_desktop(request)
        selected = await principal(request)
        await require_atlas(selected)
        await require_balance(selected)
        return selected

    async def config(request: web.Request) -> web.Response:
        selected = await authorize(request)
        return web.json_response({"available": engine.configured, "csrf_token": selected.csrf_token, "voices": [{"id": key, "label": label} for key, label in VOICES.items()], "default_voice": "george", "max_utterance_seconds": 30, "max_call_seconds": 1800, "transport": "websocket", "realtime_stt": False, "privacy": "Микрофон включается только во время звонка. Аудиозаписи не сохраняются в T-Mod; речь передаётся OpenRouter/ElevenLabs, текст — в историю Atlas.", "tts_model": engine.tts_model, "stt_model": engine.stt_model}, headers={"Cache-Control": "private, no-store"})

    async def cleanup(_: web.Application) -> None:
        await engine.close()
    app.on_cleanup.append(cleanup)
    register_call_socket(app, authorize=authorize, dashboard=dashboard, brain=brain, meter=meter, engine=engine)
    app.router.add_get("/api/atlas/call/config", config)
