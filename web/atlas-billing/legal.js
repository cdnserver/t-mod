(() => {
  'use strict';

  const form = document.querySelector('#privacy-request-form');
  const status = document.querySelector('#privacy-request-status');
  if (!(form instanceof HTMLFormElement) || !(status instanceof HTMLOutputElement)) return;

  let receipt = '';
  const makeReceipt = () => globalThis.crypto?.randomUUID?.()
    || `privacy-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
  const show = (message, success = false) => {
    status.textContent = message;
    status.classList.add('visible');
    status.classList.toggle('success', success);
  };

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    receipt ||= makeReceipt();
    show('Регистрируем запрос…', true);
    const values = new FormData(form);
    try {
      const response = await fetch('/api/privacy/requests', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          receipt,
          request_type: values.get('request_type'),
          email: values.get('email'),
          account_login: values.get('account_login'),
          scope: values.get('scope'),
          details: values.get('details'),
          website: values.get('website'),
          acknowledge: values.get('acknowledge') === 'on',
        }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.message || `Запрос не принят (HTTP ${response.status}).`);
      show(`Запрос принят. Номер: ${result.request_code}. Сохраните его.`, true);
      form.querySelectorAll('input, select, textarea, button').forEach((control) => { control.disabled = true; });
    } catch (error) {
      show(error instanceof Error ? error.message : 'Связь прервалась. Повторите отправку.');
      button.disabled = false;
    }
  });
})();
