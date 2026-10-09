"""Administrator-only, preview-then-confirm account removal in Nuclear Reactor."""
import asyncio
import json
from aiohttp import web
from modules.consensus_web_auth import csrf_matches
from modules.global_log_runtime import emit_global_event
from persistence import account_removal_repository as accounts


def register_account_removal_routes(app, *, guild_id, authenticate):
    async def handle(request):
        principal, legacy = await authenticate(request)
        if legacy or principal is None or not principal.administrator:
            raise web.HTTPForbidden(text='administrator_required')
        if request.method == 'GET':
            try:
                result = await asyncio.to_thread(accounts.account_for_removal, guild_id, request.query.get('lookup', '')[:64])
            except ValueError:
                return web.json_response({'error': 'account_lookup_invalid'}, status=400)
            return web.json_response({'account': result}, headers={'Cache-Control': 'private, no-store'})
        if not csrf_matches(request, principal):
            raise web.HTTPForbidden(text='csrf_failed')
        if request.content_type != 'application/json':
            raise web.HTTPBadRequest()
        raw = bytearray()
        try:
            async with asyncio.timeout(10):
                async for chunk in request.content.iter_chunked(2048):
                    raw.extend(chunk)
                    if len(raw) > 4096:
                        raise web.HTTPRequestEntityTooLarge(max_size=4096, actual_size=len(raw))
            body = json.loads(raw)
            if not isinstance(body, dict) or not isinstance(body.get('user_id'), str) or not body['user_id'].isdigit() or not isinstance(body.get('confirmation'), str):
                raise ValueError('account_confirmation_invalid')
            user_id = int(body['user_id'])
            if not 0 < user_id <= 9223372036854775807:
                raise ValueError('account_confirmation_invalid')
            if body.get('acknowledged') is not True:
                raise ValueError('account_confirmation_required')
            if user_id == principal.user_id:
                raise ValueError('account_self_removal_forbidden')
            removed = await asyncio.to_thread(accounts.remove_account, guild_id, user_id, confirmed_login=body['confirmation'])
        except (ValueError, TypeError, UnicodeError) as exc:
            return web.json_response({'error': str(exc)}, status=400)
        except TimeoutError:
            return web.json_response({'error': 'request_timeout'}, status=408)
        if removed:
            emit_global_event({'event_type': 'account.removed', 'guild_id': guild_id,
                              'actor_user_id': principal.user_id, 'subject_user_id': user_id,
                              'summary': 'Аккаунт удалён администратором; документальная история сохранена'})
        return web.json_response({'removed': removed}, headers={'Cache-Control': 'private, no-store'})
    app.router.add_get('/api/admin/members/account-removal', handle)
    app.router.add_post('/api/admin/members/account-removal', handle)
