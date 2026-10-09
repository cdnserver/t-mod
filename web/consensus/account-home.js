"use strict";
(() => {
  const byId = id => document.getElementById(id);
  let loading = false;
  async function load() {
    if (loading) return;
    loading = true; byId('account-retry').hidden = true;
    byId('account-status').textContent = 'Загружаем аккаунт…';
    try {
      const response = await fetch('/api/account/overview', { credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(15000) });
      if (response.status === 401) { location.assign('/login?next=/account-home'); return; }
      if (!response.ok) throw Error('account_unavailable');
      const data = await response.json();
      if (typeof data.login !== 'string' || typeof data.display_name !== 'string' || typeof data.discord_id !== 'string' || !Array.isArray(data.characters)) throw Error('account_invalid');
      byId('identity-name').textContent = data.display_name;
      byId('identity-login').textContent = data.login;
      byId('identity-discord').textContent = data.discord_id;
      byId('identity-characters').replaceChildren(...data.characters.map(item => {
        if (typeof item.nickname !== 'string' || typeof item.static_id !== 'string') throw Error('character_invalid');
        const line = document.createElement('p'); line.textContent = `${item.nickname} · #${item.static_id}`; return line;
      }));
      byId('account-status').textContent = 'Аккаунт подключён';
    } catch {
      byId('account-status').textContent = 'Не удалось загрузить данные. Ваш аккаунт не изменён.';
      byId('account-retry').hidden = false;
    } finally { loading = false; }
  }
  byId('account-retry').addEventListener('click', load); void load();
})();
