"use strict";

const params = new URLSearchParams(location.search);
const form = document.querySelector("#credential-form");
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
  invalid: "Логин или PIN не подошли. Проверьте данные и повторите вход.",
  locked: "Вход временно приостановлен после частых попыток. Подождите несколько минут и повторите вход.",
  reset_required: "После трёх неверных попыток вход заблокирован. Напишите боту /reset в личных сообщениях и задайте новый PIN.",
  administrator: "Эта учётная запись действует, но административных прав в Discord нет.",
  atlas_access: "Для этого раздела нужен отдельный доступ администратора.",
};

form.action = `/auth/login?next=${encodeURIComponent(next)}`;
context.textContent = destinations[next] || "После входа сервис определит доступные вам возможности.";
if (errors[params.get("error")]) {
  feedback.textContent = errors[params.get("error")];
  feedback.hidden = false;
}

visibility.addEventListener("click", () => {
  const visible = pin.type === "text";
  pin.type = visible ? "password" : "text";
  visibility.setAttribute("aria-label", visible ? "Показать PIN" : "Скрыть PIN");
  visibility.textContent = visible ? "◉" : "○";
  pin.focus();
});

form.addEventListener("submit", () => {
  form.classList.add("submitting");
  form.querySelector("button[type='submit']").disabled = true;
});
