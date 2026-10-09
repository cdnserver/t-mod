"""Remove an account and private settings, not financial or institutional history."""
from persistence.core import _db_lock, connect, connect_readonly, postgres_enabled
from persistence.web_auth_repository import normalize_web_login


def account_for_removal(guild: int, lookup: str) -> dict | None:
    lookup = str(lookup).strip()
    with connect_readonly() as con:
        if lookup.isdigit():
            user = int(lookup)
            if not 0 < user <= 9223372036854775807:
                raise ValueError('account_lookup_invalid')
            row = con.execute('SELECT user_id,login_display FROM web_credentials WHERE guild_id=? AND user_id=?', (guild, user)).fetchone()
        else:
            row = con.execute('SELECT user_id,login_display FROM web_credentials WHERE guild_id=? AND login_key=?', (guild, normalize_web_login(lookup))).fetchone()
    return {'user_id': str(row['user_id']), 'login': row['login_display']} if row else None


def remove_account(guild: int, user: int, *, confirmed_login: str) -> bool:
    with _db_lock, connect() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT login_display FROM web_credentials WHERE guild_id=? AND user_id=?' + (' FOR UPDATE' if postgres_enabled() else ''), (guild, user)).fetchone()
        if row is None:
            con.rollback()
            return False
        if row['login_display'] != confirmed_login:
            raise ValueError('account_confirmation_mismatch')
        # A durable generation prevents old cookies from becoming valid again
        # if the same Discord identity creates a new account later.
        con.execute("""INSERT INTO account_security(guild_id,user_id,credential_kind,security_version)
            VALUES(?,?,'deleted',2) ON CONFLICT(guild_id,user_id) DO UPDATE SET
            credential_kind='deleted',security_version=account_security.security_version+1,
            mfa_method='',mfa_secret='',pending_method='',pending_secret='',pending_until=0,last_step=-1,recovery_hashes='[]'""", (guild, user))
        for table, column in (
            ('blackbird_media_assets', 'user_id'), ('blackbird_media_posts', 'author_id'),
            ('blackbird_media_profiles', 'user_id'), ('profile_characters', 'user_id'),
            ('member_profiles', 'user_id'), ('web_section_grants', 'user_id'),
            ('account_registrations', 'user_id'), ('account_security_challenges', 'user_id'),
            ('telegram_login_codes', 'discord_user_id'), ('telegram_link_challenges', 'discord_user_id'),
            ('telegram_account_links', 'discord_user_id'), ('web_credentials', 'user_id'),
        ):
            con.execute(f'DELETE FROM {table} WHERE guild_id=? AND {column}=?', (guild, user))
        con.commit()
        return True
