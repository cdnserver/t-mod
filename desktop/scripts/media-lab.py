"""Loopback-only Media/Communicate smoke lab with real routes and disposable DB.

Run from the repository root with the project's Python environment. This is
NOT a production authentication server and never reads the installed database.
Ctrl+C deletes its temporary data. The renderer page is a development entry
point, deliberately absent from electron.vite.config.ts's release inputs.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import secrets
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
# Select the disposable SQLite backend before importing any storage module.
os.environ["TMOD_DATABASE_BACKEND"] = "sqlite"
os.environ.pop("DATABASE_URL", None)

from aiohttp import web
import storage
from modules.consensus_web_auth import ConsensusWebPrincipal, TModAccountIdentity
from modules.reactor_web import register_reactor_web_routes
from persistence import blackbird_media_repository as media
from persistence import web_auth_repository
from persistence import tvrs_repository, blackbird_communicate_repository as communicate
from persistence.postgres_compat import postgres_enabled


async def main() -> None:
    if postgres_enabled():
        raise RuntimeError("Media lab refuses to run with PostgreSQL enabled")
    token = secrets.token_hex(24)
    origin = "http://127.0.0.1:5181"
    actors = {100: "Ирина Тестовая", 200: "Алексей Тестовый"}
    with tempfile.TemporaryDirectory(prefix="blackbird-media-lab-") as directory:
        storage.DATA_DIR = Path(directory)
        storage.DATABASE_FILE = storage.DATA_DIR / "lab.sqlite3"
        storage.init_db()
        for actor in actors:
            web_auth_repository.configure_web_credential(77, actor, f"lab{actor}", secrets.token_urlsafe(20), kind="password")
        media.save_profile(77, 200, actors[200], "Тестовый участник. Только локальная база.", "night", True)
        media.create_post(77, 200, "post", "Проверяем реальные публикации, поиск и приватность профилей.")
        bill = tvrs_repository.tvrs_create_bill(guild_id=77, channel_id=123, author_id=100,
            author_display=actors[100], title="Транспортная доступность Phoenix",
            summary="Предлагается организовать регулярные поездки для новых участников и согласовать общий график. Только локальная тестовая инициатива.", materials=None)
        tvrs_repository.tvrs_set_bill_message(bill.id, 456)
        communicate.send_message(77, 100, 200, f"Привет! Обсудим предложение? https://consensus.tvr.lat/bills/{bill.id}")
        communicate.send_message(77, 200, 100, "Привет! Да, карточку вижу. Давай посмотрим детали.")

        @web.middleware
        async def isolated_lab(request, handler):
            if request.headers.get("Origin") not in (None, origin):
                raise web.HTTPForbidden()
            if request.method == "OPTIONS":
                response = web.Response(status=204)
            elif not secrets.compare_digest(request.headers.get("X-Blackbird-Lab", ""), token):
                response = web.json_response({"error": "lab_token_required"}, status=403)
            else:
                try:
                    response = await handler(request)
                except web.HTTPException as error:
                    response = error
            response.headers.update({
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type,X-CSRF-Token,X-Lab-Actor,X-Blackbird-Lab,X-Blackbird-Partner,X-Blackbird-Filename,X-Blackbird-Caption,X-Blackbird-Nonce",
                "Access-Control-Expose-Headers": "ETag,Content-Type,X-Blackbird-Filename",
                "Cache-Control": "no-store",
                "Vary": "Origin",
            })
            return response

        async def authenticate(request):
            try:
                actor = int(request.headers.get("X-Lab-Actor", "0"))
            except ValueError:
                return None, False
            if actor not in actors:
                return None, False
            # Actor 100 deliberately has no Senate membership. Social features
            # must work for ordinary registered Blackbird accounts as well.
            member = (TModAccountIdentity(actor, actors[actor]) if actor == 100 else
                      SimpleNamespace(id=actor, roles=[], guild_permissions=SimpleNamespace(administrator=False)))
            return ConsensusWebPrincipal(actor, 77, actors[actor], "csrf-lab-only", member), False

        application = web.Application(middlewares=[isolated_lab])
        register_reactor_web_routes(application, SimpleNamespace(), guild_id=77,
                                   asset_dir=ROOT / "web" / "consensus", authenticate=authenticate)
        runner = web.AppRunner(application, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", 5182).start()
            print(f"LAB_READY {origin}/media-lab.html?token={token}", flush=True)
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
