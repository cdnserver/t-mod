"use strict";

const params = new URLSearchParams(location.search);
const form = document.querySelector("#credential-form");
const pin = document.querySelector("#credential-pin");
const visibility = document.querySelector("#pin-visibility");
const feedback = document.querySelector("#login-feedback");
const requestedNext = params.get("next");
const next = ["/admin", "/reactor", "/atlas", "/games"].includes(requestedNext) ? requestedNext : "/";
const errors = {
  invalid: "Логин или PIN не подошли. Проверьте данные и повторите вход.",
  locked: "Вход временно приостановлен после частых попыток. Подождите несколько минут и повторите вход.",
  reset_required: "После трёх неверных попыток вход заблокирован. Напишите боту /reset в личных сообщениях и задайте новый PIN.",
  administrator: "Эта учётная запись действует, но административных прав в Discord нет.",
};

form.action = `/auth/login?next=${encodeURIComponent(next)}`;
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
