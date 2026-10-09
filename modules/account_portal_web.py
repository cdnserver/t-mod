"""Account-owned landing, independent from Senate and Atlas privileges."""
from __future__ import annotations

import asyncio
from pathlib import Path

from aiohttp import web

from modules.consensus_web_auth import resolve_principal
from persistence import profile_repository as profiles
from persistence import web_auth_repository as credentials


def register_account_portal_routes(app: web.Application, bot, *, guild_id: int, asset_dir: Path) -> None:
    async def principal(request):
        identity = await resolve_principal(request, bot, guild_id=guild_id)
        if identity is None:
            if request.path.startswith('/api/'):
                raise web.HTTPUnauthorized(text='account_session_required')
            raise web.HTTPSeeOther(location='/login?next=/account-home')
        return identity

    async def page(request):
        await principal(request)
        return web.FileResponse(asset_dir / 'account-home.html')

    async def overview(request):
        identity = await principal(request)
        credential, profile, characters = await asyncio.gather(
            asyncio.to_thread(credentials.get_web_credential, guild_id, identity.user_id),
            asyncio.to_thread(profiles.get_member_profile, guild_id, identity.user_id),
            asyncio.to_thread(profiles.list_profile_characters, guild_id, identity.user_id),
        )
        if credential is None:
            raise web.HTTPUnauthorized(text='account_session_required')
        return web.json_response({
            'login': credential.login,
            'display_name': (profile.preferred_name if profile else '') or identity.display_name,
            'discord_id': str(identity.user_id),
            'characters': [{'nickname': item.nickname, 'static_id': item.static_id} for item in characters],
        }, headers={'Cache-Control': 'private, no-store'})

    app.router.add_get('/account-home', page)
    app.router.add_get('/api/account/overview', overview)
