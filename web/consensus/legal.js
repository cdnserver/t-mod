(() => {
  "use strict";

  const route = location.pathname.replace(/^\/+|\/+$/g, "") || "legal";
  const supported = new Set(["legal", "privacy", "terms", "cookies", "data-request"]);
  const selected = supported.has(route) ? route : "legal";
  document.querySelectorAll("[data-policy]").forEach((section) => {
    section.classList.toggle("active", section.dataset.policy === selected);
  });
  document.querySelectorAll("[data-route]").forEach((link) => {
    const active = link.dataset.route === selected;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
  });
  document.documentElement.classList.add("legal-ready");

  const titles = {
    legal: "Правовой центр — T-Mod",
    privacy: "Политика конфиденциальности — T-Mod",
    terms: "Условия использования — T-Mod",
    cookies: "Cookies и локальное хранение — T-Mod",
    "data-request": "Управление персональными данными — T-Mod",
  };
  document.title = titles[selected] || titles.legal;

  const form = document.querySelector("#privacy-request-form");
  const status = document.querySelector("#privacy-request-status");
  if (!(form instanceof HTMLFormElement) || !(status instanceof HTMLOutputElement)) return;

  let receipt = "";
  const makeReceipt = () => {
    if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
      return globalThis.crypto.randomUUID();
    }
    const random = Math.random().toString(36).slice(2);
    return `privacy-${Date.now().toString(36)}-${random}-${random}`;
  };
  const showStatus = (message, success = false) => {
    status.textContent = message;
    status.classList.add("visible");
    status.classList.toggle("success", success);
  };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const submit = form.querySelector("button[type=submit]");
    if (submit instanceof HTMLButtonElement) submit.disabled = true;
    showStatus("Регистрируем запрос в защищённом контуре…", true);
    receipt ||= makeReceipt();
    const values = new FormData(form);
    const payload = {
      receipt,
      request_type: values.get("request_type"),
      email: values.get("email"),
      discord_id: values.get("discord_id"),
      account_login: values.get("account_login"),
      scope: values.get("scope"),
      details: values.get("details"),
      website: values.get("website"),
      acknowledge: values.get("acknowledge") === "on",
    };
    try {
      const response = await fetch("/api/privacy/requests", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.message || "Запрос не принят. Повторите попытку.");
      const code = String(result.request_code || "").trim();
      showStatus(
        code
          ? `Запрос принят. Номер: ${code}. Сохраните его до получения ответа.`
          : "Запрос принят.",
        true,
      );
      form.querySelectorAll("input, select, textarea, button").forEach((control) => {
        control.disabled = true;
      });
    } catch (error) {
      showStatus(error instanceof Error ? error.message : "Связь прервалась. Повторная отправка не создаст дубликат.");
      if (submit instanceof HTMLButtonElement) submit.disabled = false;
    }
  });
})();
