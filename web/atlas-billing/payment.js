(() => {
  'use strict';
  const title = document.querySelector('#payment-title');
  const message = document.querySelector('#payment-message');
  const details = document.querySelector('#payment-details');
  const action = document.querySelector('#payment-action');
  const failed = location.pathname.endsWith('/fail');
  const params = new URLSearchParams(location.search);
  const orderId = params.get('InvId') || params.get('InvoiceID') || '';
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  const setResult = (headline, copy, detail = '') => {
    title.textContent = headline;
    message.textContent = copy;
    details.textContent = detail;
  };

  if (failed) {
    setResult('Оплата не завершена', 'Atlas Token не зачислены. Вернитесь к тарифам, чтобы безопасно повторить оплату.', orderId ? `Заказ №${orderId}` : 'Списание не подтверждено');
    action.textContent = 'Вернуться к тарифам';
    return;
  }
  if (!/^\d+$/.test(orderId)) {
    setResult('Проверяем подтверждение', 'Robokassa вернула пользователя без номера заказа. Проверьте историю операций в личном кабинете.', 'Баланс нельзя изменить по одному редиректу');
    return;
  }

  (async () => {
    for (let attempt = 0; attempt < 8; attempt += 1) {
      try {
        const response = await fetch(`/api/atlas/billing/orders/${encodeURIComponent(orderId)}`, {credentials: 'include', cache: 'no-store'});
        if (response.status === 401) {
          setResult('Войдите в Учётную запись', 'Платёж проверяется сервером, но для просмотра заказа необходимо восстановить защищённую сессию.', `Заказ №${orderId}`);
          action.href = `https://tvr.lat/login?next=${encodeURIComponent(location.href)}`;
          action.textContent = 'Войти и проверить';
          return;
        }
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const order = await response.json();
        if (order.status === 'paid') {
          setResult('Платёж подтверждён', `${new Intl.NumberFormat('ru-RU').format(order.atlas_tokens)} AT зачислены в ваш Atlas.`, `Заказ №${order.id} · Robokassa`);
          return;
        }
        details.textContent = `Заказ №${order.id} · ожидаем серверное подтверждение`;
      } catch {
        details.textContent = `Заказ №${orderId} · повторяем проверку`;
      }
      await wait(1800);
    }
    setResult('Платёж обрабатывается', 'Подтверждение ещё не поступило. Заказ сохранён: баланс обновится автоматически после ответа Robokassa.', `Заказ №${orderId}`);
  })();
})();
