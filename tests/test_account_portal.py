import tempfile
import unittest
from collections import defaultdict, deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import storage
from modules.consensus_web import _canonical_surface_location, create_consensus_web_app
from modules.consensus_web_auth import ConsensusWebPrincipal, account_cookie_domain, create_session_token, _verify
from modules.account_removal_web import register_account_removal_routes
from modules.account_auth_web import register_account_auth_routes
from modules.account_portal_web import register_account_portal_routes
from persistence import account_removal_repository as removal
from persistence import account_security_repository as security
from persistence import web_auth_repository as credentials
from persistence import blackbird_media_repository as media


class AccountPortalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.previous = storage.DATA_DIR, storage.DATABASE_FILE
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        storage.DATA_DIR = Path(self.temp.name)
        storage.DATABASE_FILE = storage.DATA_DIR / 'accounts.db'
        storage.init_db()
        credentials.configure_web_credential(77, 501, 'sample.account', '12345678')
        self.member = SimpleNamespace(id=501, display_name='Тестовый участник', name='test', roles=[], guild_permissions=SimpleNamespace(administrator=False))
        self.guild = SimpleNamespace(id=77, get_member=lambda user: self.member if user == 501 else None, fetch_member=AsyncMock(return_value=self.member))
        self.bot = SimpleNamespace(get_guild=lambda gid: self.guild, guilds=[self.guild])

    def tearDown(self):
        storage.DATA_DIR, storage.DATABASE_FILE = self.previous
        self.temp.cleanup()

    def test_all_public_auth_pages_have_one_canonical_host(self):
        for path in ['/login', '/register', '/auth/ticket']:
            req = SimpleNamespace(path=path, query={'next': '/admission'}, host='phx.tvr.lat', headers={}, remote='127.0.0.1', rel_url=path+'?next=/admission')
            self.assertEqual(_canonical_surface_location(req), 'https://account.tvr.lat'+req.rel_url)
            req.host = 'account.tvr.lat'
            self.assertIsNone(_canonical_surface_location(req))
        self.assertEqual(account_cookie_domain('account.tvr.lat'), '.tvr.lat')
        self.assertIsNone(account_cookie_domain('account.tvr.lat.evil.example'))

    async def test_browser_login_finishes_at_account_not_consensus_and_rejects_external_next(self):
        client = TestClient(TestServer(create_consensus_web_app(self.bot, guild_id=77)))
        await client.start_server()
        try:
            for next_path in ['/account-home', 'https://evil.example', '//evil.example']:
                response = await client.post('/auth/login', params={'client': 'browser', 'next': next_path}, data={'login': 'sample.account', 'pin': '12345678'}, headers={'Origin': str(client.make_url('/')).rstrip('/')})
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())['destination'], '/account-home')
                self.assertIn('tmod_account_session', response.cookies)
            state = await client.get('/api/account/overview')
            self.assertEqual(state.status, 200)
            data = await state.json()
            self.assertEqual(data['discord_id'], '501')
            self.assertEqual(data['login'], 'sample.account')
            self.assertNotIn('pin_hash', data)
            page = await client.get('/account-home')
            self.assertEqual(page.status, 200)
            self.assertIn('единый аккаунт', await page.text())
            rejected = await client.post('/auth/login?client=browser', data={'login': 'sample.account', 'pin': '12345678'}, headers={'Origin': 'https://evil.example'})
            self.assertEqual(rejected.status, 403)
            self.assertNotIn('tmod_account_session', rejected.cookies)
        finally:
            await client.close()

    async def test_portal_does_not_expose_identity_without_a_session(self):
        client = TestClient(TestServer(create_consensus_web_app(self.bot, guild_id=77)))
        await client.start_server()
        try:
            response = await client.get('/api/account/overview')
            self.assertEqual(response.status, 401)
            response = await client.get('/account-home', allow_redirects=False)
            self.assertEqual(response.status, 303)
            self.assertEqual(response.headers['Location'], '/login?next=/account-home')
            for filename in ['account-home.js', 'onboarding-light.css', 'onboarding-sound.js']:
                response = await client.get('/assets/' + filename)
                self.assertEqual(response.status, 200, filename)
        finally:
            await client.close()

    async def test_account_home_login_does_not_require_senate_or_guild_membership(self):
        bot = SimpleNamespace(get_guild=lambda gid: None, guilds=[])
        client = TestClient(TestServer(create_consensus_web_app(bot, guild_id=77)))
        await client.start_server()
        try:
            response = await client.post('/auth/login?client=browser&next=/account-home',
                data={'login': 'sample.account', 'pin': '12345678'},
                headers={'Origin': str(client.make_url('/')).rstrip('/')})
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())['destination'], '/account-home')
            self.assertEqual((await client.get('/api/account/overview')).status, 200)
        finally:
            await client.close()

    async def test_account_routes_work_without_consensus_controller_and_logout_revokes_session(self):
        app = web.Application()
        assets = Path(__file__).resolve().parents[1] / 'web' / 'consensus'
        register_account_auth_routes(app, self.bot, guild_id=77, asset_dir=assets,
            request_remote=lambda request: request.remote or 'test',
            login_failures=defaultdict(lambda: deque(maxlen=30)))
        register_account_portal_routes(app, self.bot, guild_id=77, asset_dir=assets)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post('/auth/login?client=browser',
                data={'login': 'sample.account', 'pin': '12345678'},
                headers={'Origin': str(client.make_url('/')).rstrip('/')})
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())['destination'], '/account-home')
            self.assertEqual((await client.get('/api/account/overview')).status, 200)
            response = await client.get('/auth/logout', allow_redirects=False, headers={'Host': 'account.tvr.lat'})
            self.assertEqual(response.headers['Location'], '/login?next=/account-home')
            self.assertEqual((await client.get('/api/account/overview')).status, 401)
        finally:
            await client.close()

    def test_removal_is_atomic_and_old_sessions_stay_revoked_after_recreation(self):
        token, _ = create_session_token(guild_id=77, user_id=501, session_version=1)
        old = _verify(token, purpose='session')
        media.save_profile(77, 501, 'Участник', 'Описание', 'orbit', True)
        media.create_post(77, 501, 'post', 'Тестовая публикация')
        with self.assertRaisesRegex(ValueError, 'account_confirmation_mismatch'):
            removal.remove_account(77, 501, confirmed_login='wrong')
        self.assertIsNotNone(credentials.get_web_credential(77, 501))
        self.assertTrue(removal.remove_account(77, 501, confirmed_login='sample.account'))
        self.assertIsNone(credentials.get_web_credential(77, 501))
        self.assertFalse(security.session_allowed(77, 501, old, require_mfa=False))
        self.assertEqual(media.search_profiles(77, 501, 'Участник'), [])
        credentials.configure_web_credential(77, 501, 'another.account', '12345678')
        self.assertFalse(security.session_allowed(77, 501, old, require_mfa=False))
        token, _ = create_session_token(guild_id=77, user_id=501, session_version=1)
        self.assertTrue(security.session_allowed(77, 501, _verify(token, purpose='session'), require_mfa=False))

    async def test_removal_requires_admin_csrf_and_exact_confirmation(self):
        identity = ConsensusWebPrincipal(user_id=1, guild_id=77, display_name='Администратор', csrf_token='csrf-test', member=SimpleNamespace(guild_permissions=SimpleNamespace(administrator=True)))
        auth = AsyncMock(return_value=(identity, False))
        app = web.Application()
        register_account_removal_routes(app, guild_id=77, authenticate=auth)
        client = TestClient(TestServer(app)); await client.start_server()
        try:
            lookup = await client.get('/api/admin/members/account-removal?lookup=sample.account')
            self.assertEqual((await lookup.json())['account']['user_id'], '501')
            body = {'user_id': '501', 'confirmation': 'sample.account', 'acknowledged': True}
            response = await client.post('/api/admin/members/account-removal', json=body)
            self.assertEqual(response.status, 403)
            response = await client.post('/api/admin/members/account-removal', json={**body, 'confirmation': 'wrong'}, headers={'X-CSRF-Token': 'csrf-test'})
            self.assertEqual(response.status, 400)
            auth.return_value = (identity, True)
            response = await client.post('/api/admin/members/account-removal', json=body, headers={'X-CSRF-Token': 'csrf-test'})
            self.assertEqual(response.status, 403)
            identity.member.guild_permissions.administrator = False
            auth.return_value = (identity, False)
            response = await client.get('/api/admin/members/account-removal?lookup=sample.account')
            self.assertEqual(response.status, 403)
            identity.member.guild_permissions.administrator = True
            response = await client.post('/api/admin/members/account-removal', json={**body, 'user_id': '1'}, headers={'X-CSRF-Token': 'csrf-test'})
            self.assertEqual(response.status, 400)
            self.assertEqual((await response.json())['error'], 'account_self_removal_forbidden')
            auth.return_value = (identity, False)
            response = await client.post('/api/admin/members/account-removal', json=body, headers={'X-CSRF-Token': 'csrf-test'})
            self.assertEqual(response.status, 200)
            self.assertTrue((await response.json())['removed'])
        finally:
            await client.close()
