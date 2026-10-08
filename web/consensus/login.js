"use strict";

const params = new URLSearchParams(location.search);
const form = document.querySelector("#credential-form");
const telegramForm = document.querySelector("#telegram-login-form");
const telegramLogin = document.querySelector("#telegram-login");
const pin = document.querySelector("#credential-pin");
const visibility = document.querySelector("#pin-visibility");
const feedback = document.querySelector("#login-feedback");
const context = document.querySelector("#login-context small");
const requestedNext = params.get("next");
const next = ["/admin", "/reactor", "/atlas", "/atlas-billing", "/account", "/games", "/sgl", "/ovr", "/host", "/tasks", "/admission"].includes(requestedNext) ? requestedNext : "/";
const destinations = {
  "/admin": "После входа откроется Ядерный Реактор.",
  "/reactor": "После входа откроется ваш личный Реактор.",
  "/atlas": "После входа откроется контур Atlas.",
  "/atlas-billing": "После входа откроется ваш баланс и магазин Atlas Token.",
  "/account": "После входа откроется ваш личный кабинет Atlas.",
  "/games": "После входа откроется T-Mod Games.",
  "/sgl": "После входа откроется защищённый контур SGL.",
  "/ovr": "После входа откроется портал ОВР.",
  "/host": "После входа откроется пульт ведущего.",
  "/tasks": "После входа откроется общий список задач.",
  "/admission": "После входа вернём вас к заявке в Сенат Phoenix.",
};
const errors = {
  invalid: "Логин, PIN или пароль не подошли. Проверьте данные и повторите вход.",
  locked: "Вход временно приостановлен после частых попыток. Подождите несколько минут и повторите вход.",
  telegram_invalid: "Код Telegram неверный, просрочен или уже использован. Запросите новый в боте.",
  account_missing: "Для этого T‑Mod аккаунта ещё не создан веб-вход. Настройте его через /account у бота.",
  membership: "Этот раздел доступен только участникам сервера Товарищества.",
  reset_required: "После трёх неверных попыток вход заблокирован. Напишите боту /reset в личных сообщениях и задайте новый PIN.",
  administrator: "Эта учётная запись действует, но административных прав в Discord нет.",
  atlas_access: "Для этого раздела нужен отдельный доступ администратора.",
};

form.action = `/auth/login?next=${encodeURIComponent(next)}`;
telegramForm.action = `/auth/telegram?next=${encodeURIComponent(next)}`;
context.textContent = destinations[next] || "После входа сервис определит доступные вам возможности.";
document.querySelector("#create-account-link").href = `/register?next=${encodeURIComponent(next)}`;
if (location.hash === "#telegram-login") {
  telegramLogin.open = true;
  requestAnimationFrame(() => document.querySelector("#telegram-login-code").focus());
}
if (errors[params.get("error")]) {
  feedback.textContent = errors[params.get("error")];
  feedback.hidden = false;
}

visibility.addEventListener("click", () => {
  const visible = pin.type === "text";
  pin.type = visible ? "password" : "text";
  visibility.setAttribute("aria-label", visible ? "Показать PIN или пароль" : "Скрыть PIN или пароль");
  visibility.textContent = visible ? "◉" : "○";
  pin.focus();
});

form.addEventListener("submit", async event => {
  event.preventDefault();
  if (form.classList.contains("submitting")) return;
  form.classList.add("submitting");
  form.querySelector("button[type='submit']").disabled = true;
  feedback.hidden = true;
  try {
    const response = await fetch(form.action, { method: "POST", body: new URLSearchParams(new FormData(form)), credentials: "same-origin", signal: AbortSignal.timeout(25000) });
    if (response.status === 202) {
      const result = await response.json();
      document.querySelector("#factor-challenge").value = result.challenge || "";
      document.querySelector("#factor-field").hidden = false;
      document.querySelector("#factor-label").textContent = result.method === "totp" ? "Код аутентификатора или резервный код" : `Код ${result.method === "telegram" ? "Telegram" : "Discord"} или резервный код`;
      document.querySelector("#factor-code").focus();
      feedback.textContent = result.delivery_failed ? "Доставка кода недоступна. Используйте сохранённый резервный код." : "Подтвердите вход. Если код истёк, измените поле PIN/пароля, чтобы начать заново.";
      feedback.hidden = false;
      return;
    }
    const destination = new URL(response.url, location.origin);
    if (destination.origin !== location.origin) throw new Error("unexpected_origin");
    if (destination.pathname === "/login") {
      feedback.textContent = errors[destination.searchParams.get("error")] || "Не удалось войти. Проверьте данные.";
      feedback.hidden = false;
      return;
    }
    if (!response.ok) throw new Error("login_unavailable");
    location.assign(destination.pathname + destination.search + destination.hash);
  } catch {
    feedback.textContent = "Не удалось завершить вход. Проверьте соединение и повторите попытку.";
    feedback.hidden = false;
  } finally {
    form.classList.remove("submitting");
    form.querySelector("button[type='submit']").disabled = false;
  }
});
for (const input of [pin, form.querySelector('[name="login"]')]) input.addEventListener("input", () => {
  document.querySelector("#factor-challenge").value = "";
  document.querySelector("#factor-code").value = "";
  document.querySelector("#factor-field").hidden = true;
});

telegramForm.addEventListener("submit", () => {
  telegramForm.classList.add("submitting");
  telegramForm.querySelector("button[type='submit']").disabled = true;
});
