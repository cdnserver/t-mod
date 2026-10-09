"""Verify removal against PostgreSQL metadata using session-local TEMP tables only.

Never initializes the application schema or copies production records. The
connection's search_path excludes public before any repository operation.
"""
from __future__ import annotations

import json
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run_smoke() -> dict:
    from persistence import account_removal_repository as removal
    from persistence import account_security_repository as security
    from persistence import web_auth_repository as credentials
    from persistence.postgres_compat import (
        PostgresCompatConnection, postgres_enabled, raw_postgres_connection,
    )
    if not postgres_enabled():
        raise RuntimeError('This smoke test requires PostgreSQL')
    tables = (
        'web_credentials', 'account_security', 'blackbird_media_assets',
        'blackbird_media_posts', 'blackbird_media_profiles', 'profile_characters',
        'member_profiles', 'web_section_grants', 'account_registrations',
        'account_security_challenges', 'telegram_login_codes',
        'telegram_link_challenges', 'telegram_account_links',
        'global_bans', 'atlas_billing_accounts',
    )
    checks = []
    with raw_postgres_connection() as raw:
        raw.execute('SET search_path = pg_temp')
        raw.execute("SET statement_timeout = '15s'")
        raw.execute("SET lock_timeout = '5s'")
        for table in tables:
            # Literal, allowlisted names; LIKE copies metadata, never rows.
            raw.execute(f'CREATE TEMP TABLE "{table}" (LIKE public."{table}" INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES)')
        raw.commit()
        assert raw.execute('SHOW search_path').fetchone()[0] == 'pg_temp'
        for table in tables:
            assert raw.execute('SELECT relpersistence FROM pg_class WHERE oid = to_regclass(%s)', (table,)).fetchone()[0] == 't'
        raw.commit()

        class BorrowedPool:
            def putconn(self, connection):
                # One private session owns these TEMP tables; close only at end.
                assert connection is raw

        pool = BorrowedPool()

        def connect():
            return PostgresCompatConnection(raw, pool)

        @contextmanager
        def readonly():
            with connect() as con:
                yield con

        with ExitStack() as stack:
            for module in (removal, security, credentials):
                stack.enter_context(patch.object(module, 'connect', connect))
                stack.enter_context(patch.object(module, 'connect_readonly', readonly))
            credentials.configure_web_credential(77, 501, 'pg.smoke', '12345678')
            credentials.configure_web_credential(77, 502, 'pg.keeper', '12345678')
            old_payload = {'av': security.state(77, 501)['security_version']}
            with connect() as con:
                for user in (501, 502):
                    con.execute("INSERT INTO member_profiles(guild_id,user_id,created_at,updated_at) VALUES(77,?,'fixture','fixture')", (user,))
                    con.execute("INSERT INTO profile_characters(id,guild_id,user_id,nickname,static_id,position,created_at,updated_at) VALUES(?,77,?,'Fixture',?,1,'fixture','fixture')", (user, user, str(user)))
                con.execute("INSERT INTO global_bans(guild_id,user_id,reason,issued_by_id,issued_by_display,issued_at,updated_at) VALUES(77,501,'fixture ban',1,'fixture','fixture','fixture')")
                con.execute("INSERT INTO atlas_billing_accounts(user_id,period_key,period_started_at,period_ends_at,created_at,updated_at) VALUES(501,'fixture','fixture','fixture','fixture','fixture')")
            try:
                removal.remove_account(77, 501, confirmed_login='wrong')
            except ValueError as exc:
                assert str(exc) == 'account_confirmation_mismatch'
            else:
                raise AssertionError('Wrong confirmation was accepted')
            assert credentials.get_web_credential(77, 501) is not None
            checks.append('wrong confirmation rolls back')
            assert removal.account_for_removal(77, 'pg.smoke')['user_id'] == '501'
            assert removal.remove_account(77, 501, confirmed_login='pg.smoke')
            assert credentials.get_web_credential(77, 501) is None
            assert credentials.get_web_credential(77, 502) is not None
            with readonly() as con:
                assert con.execute('SELECT count(*) FROM profile_characters WHERE user_id=501').fetchone()[0] == 0
                assert con.execute('SELECT count(*) FROM profile_characters WHERE user_id=502').fetchone()[0] == 1
                assert con.execute('SELECT count(*) FROM global_bans WHERE user_id=501').fetchone()[0] == 1
                assert con.execute('SELECT count(*) FROM atlas_billing_accounts WHERE user_id=501').fetchone()[0] == 1
            checks.extend(['personal rows removed', 'other account retained', 'ban retained', 'billing retained'])
            assert not security.session_allowed(77, 501, old_payload, require_mfa=False)
            assert not security.session_allowed(77, 501, {'av': security.state(77, 501)['security_version']}, require_mfa=False)
            assert not removal.remove_account(77, 501, confirmed_login='pg.smoke')
            credentials.configure_web_credential(77, 501, 'pg.recreated', '12345678')
            assert not security.session_allowed(77, 501, old_payload, require_mfa=False)
            assert security.session_allowed(77, 501, {'av': security.state(77, 501)['security_version']}, require_mfa=False)
            checks.extend(['deleted identity cannot authenticate', 'recreation does not revive old session', 'removal is idempotent'])
        # Exiting this connection drops every temporary table automatically.
    return {'ok': True, 'isolation': 'session TEMP tables; production rows untouched', 'checks': checks}


if __name__ == '__main__':
    print(json.dumps(run_smoke(), ensure_ascii=False))
