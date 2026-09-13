(() => {
  "use strict";
  if (window.__tmodGlobalLogClient) return;
  window.__tmodGlobalLogClient = true;
  const endpoint = "/api/global-log/client";
  const timers = new WeakMap();
  const service = location.hostname.split(".")[0] || "web";
  const secretName = /pass|pin|secret|token|authorization|cookie|api.?key|private.?key|rcon|csrf/i;
  const cleanText = (value, limit = 20000) => String(value || "").replace(/(bearer|bot)\s+[\w.-]+/gi, "$1 [REDACTED]").slice(0, limit);
  const describe = (element) => {
    if (!(element instanceof Element)) return { target_id: null, label: "" };
    const id = element.id || element.getAttribute("name") || element.getAttribute("data-action") || element.getAttribute("href") || element.getAttribute("aria-label") || element.tagName.toLowerCase();
    const label = element.getAttribute("aria-label") || element.getAttribute("title") || element.textContent || element.getAttribute("placeholder") || "";
    return { target_id: cleanText(id, 500), label: cleanText(label.trim().replace(/\s+/g, " "), 500) };
  };
  const send = (event, payload = {}) => {
    const body = JSON.stringify({ service, event, summary: payload.summary || event, target_type: payload.target_type || "ui", target_id: payload.target_id || null, content: payload.content || null, details: { ...payload.details, path: location.pathname, hash: location.hash } });
    fetch(endpoint, { method: "POST", credentials: "same-origin", keepalive: true, headers: { "Content-Type": "application/json" }, body }).catch(() => {});
  };
  document.addEventListener("click", (event) => {
    const control = event.target instanceof Element ? event.target.closest("button,a,[role='button'],input[type='submit'],input[type='button']") : null;
    if (!control) return;
    const item = describe(control);
    send("ui_click", { summary: `Нажатие: ${item.label || item.target_id}`, target_id: item.target_id, details: { label: item.label, tag: control.tagName.toLowerCase(), disabled: Boolean(control.disabled) } });
  }, { capture: true });
  document.addEventListener("input", (event) => {
    const input = event.target;
    if (!(input instanceof HTMLInputElement || input instanceof HTMLTextAreaElement || input instanceof HTMLSelectElement)) return;
    clearTimeout(timers.get(input));
    timers.set(input, setTimeout(() => {
      const item = describe(input); const secret = secretName.test(`${input.name} ${input.id} ${input.type} ${input.autocomplete}`);
      const value = input instanceof HTMLInputElement && ["checkbox", "radio"].includes(input.type) ? String(input.checked) : input.value;
      send("ui_input", { summary: `Изменено поле: ${item.label || item.target_id}`, target_id: item.target_id, content: secret ? null : cleanText(value), details: { label: item.label, input_type: input.type || input.tagName.toLowerCase(), secret, length: String(value).length } });
    }, 850));
  }, { capture: true });
  document.addEventListener("submit", (event) => { const item = describe(event.target); send("ui_submit", { summary: `Отправлена форма: ${item.target_id}`, target_id: item.target_id }); }, { capture: true });
  document.addEventListener("visibilitychange", () => send("page_visibility", { summary: document.hidden ? "Страница скрыта" : "Страница открыта", details: { hidden: document.hidden } }));
  window.addEventListener("error", (event) => send("client_error", { summary: `Ошибка интерфейса: ${event.message || "unknown"}`, content: event.error?.stack || event.message, details: { filename: event.filename, line: event.lineno, column: event.colno } }));
  window.addEventListener("unhandledrejection", (event) => send("client_rejection", { summary: "Необработанная ошибка интерфейса", content: event.reason?.stack || event.reason || "unknown" }));
  send("page_opened", { summary: `Открыта страница ${location.pathname}`, target_type: "page", target_id: location.pathname, details: { referrer: document.referrer, title: document.title } });
})();
