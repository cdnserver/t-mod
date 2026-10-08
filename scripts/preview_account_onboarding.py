"""Loopback-only onboarding preview; never uses a real account or Discord.

Run with the project's Python. /__lab simulates /master for user 501 in a
disposable SQLite database. This file is not imported by the production app.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import secrets
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def main():
    with tempfile.TemporaryDirectory(prefix="tvr-onboarding-preview-") as directory:
        os.environ["TMOD_DATABASE_BACKEND"] = "sqlite"
        os.environ.pop("DATABASE_URL", None)
        os.environ["DATA_DIR"] = directory
        os.environ["DATABASE_FILE"] = str(Path(directory) / "preview.sqlite3")
        os.environ["GLOBAL_LOG_ENABLED"] = "0"
        from aiohttp import web
        import storage
        from modules.consensus_web import create_consensus_web_app
        from persistence import account_registration_repository as registration
        from persistence.postgres_compat import postgres_enabled
        if postgres_enabled():
            raise RuntimeError("Preview refuses PostgreSQL")
        storage.init_db()
        member = SimpleNamespace(id=501, display_name="Локальный участник", name="preview", roles=[],
            guild_permissions=SimpleNamespace(administrator=False), bot=False)
        async def fetch_member(user_id):
            if user_id != 501:
                raise ValueError("Only the synthetic preview user is available")
            return member
        guild = SimpleNamespace(id=77, get_member=lambda user: member if user == 501 else None, fetch_member=fetch_member)
        bot = SimpleNamespace(get_guild=lambda gid: guild if gid == 77 else None, guilds=[guild], get_user=lambda uid: member if uid == 501 else None)
        app = create_consensus_web_app(bot, guild_id=77)
        csrf = secrets.token_urlsafe(24)

        async def lab(request):
            if request.host != "127.0.0.1:5183":
                raise web.HTTPForbidden()
            result = ""
            if request.method == "POST":
                if request.headers.get("Origin") != "http://127.0.0.1:5183":
                    raise web.HTTPForbidden()
                data = await request.json()
                if not secrets.compare_digest(str(data.get("csrf", "")), csrf):
                    raise web.HTTPForbidden()
                try:
                    registration.claim_registration(77, 501, str(data.get("code", "")), member.display_name)
                    result = "Тестовый Discord подтверждён. Вернитесь в мастер."
                except ValueError as exc:
                    result = str(exc)
                return web.json_response({"message": result})
            return web.Response(content_type="text/html", text=f'''<!doctype html><html lang="ru"><meta charset="utf-8"><title>Локальная проверка /master</title><body><h1>Локальная проверка</h1><p>Только временная база и вымышленный пользователь 501. Настоящий Discord не подключён.</p><form method="post" action="/__lab"><input type="hidden" name="csrf" value="{csrf}"><label>Код из мастера <input name="code" required></label><button>Подтвердить тестовый Discord</button></form><p id="result"></p><a href="/register">Мастер</a> · <a href="/admission">Phoenix</a> · <a href="/login?next=/admission">Вход</a><script src="/__lab.js"></script></body></html>''')
        async def lab_script(_):
            return web.Response(content_type="application/javascript", text='''document.querySelector('form').addEventListener('submit',async event=>{event.preventDefault();const body=Object.fromEntries(new FormData(event.target));const response=await fetch('/__lab',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const result=await response.json();document.querySelector('#result').textContent=result.message;});''')
        app.router.add_get("/__lab", lab)
        app.router.add_post("/__lab", lab)
        app.router.add_get("/__lab.js", lab_script)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 5183).start()
        print("Disposable onboarding preview: http://127.0.0.1:5183/admission · Discord simulation: /__lab", flush=True)
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
