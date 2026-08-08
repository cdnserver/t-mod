"use strict";

const byId = (id) => document.getElementById(id);
const appState = { data: null, screen: "home", selectedTemplate: null, busy: false };
const screenMeta = {
  home: ["ATLAS COMMAND", "Командный центр"],
  ai: ["GROUNDED INTELLIGENCE", "Atlas AI"],
  documents: ["DOCUMENT STUDIO", "Документы"],
  knowledge: ["KNOWLEDGE VAULT", "База знаний"],
  forum: ["FORUM DESK", "Форум и памятки"],
};
let toastTimer = null;

function element(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "") node.textContent = String(text);
  return node;
}

function clear(node) {
  while (node.firstChild) node.firstChild.remove();
}

function showToast(message, error = false) {
  const toast = byId("toast");
  clearTimeout(toastTimer);
  toast.textContent = String(message || "Готово");
  toast.className = `toast${error ? " error" : ""}`;
  toast.hidden = false;
  toastTimer = setTimeout(() => { toast.hidden = true; }, 5500);
}

function requestId() {
  return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

async function api(url, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body) {
    headers["Content-Type"] = "application/json";
    headers["X-CSRF-Token"] = appState.data?.viewer?.csrf_token || "";
    headers["X-Idempotency-Key"] = options.idempotencyKey || requestId();
  }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), options.timeout || 50000);
  try {
    const response = await fetch(url, {
      ...options,
      headers,
      credentials: "same-origin",
      cache: "no-store",
      signal: controller.signal,
    });
    if (response.status === 401) {
      location.assign("/login?next=%2Fatlas");
      throw new Error("Требуется вход");
    }
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(payload.message || `Atlas вернул HTTP ${response.status}`);
      error.payload = payload;
      throw error;
    }
    return payload;
  } finally {
    clearTimeout(timeout);
  }
}

function openDialog(dialog) {
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeDialog(dialog) {
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
}

function switchScreen(screen, updateHash = true) {
  const selected = screenMeta[screen] ? screen : "home";
  appState.screen = selected;
  document.querySelectorAll(".screen").forEach((node) => {
    node.classList.toggle("active", node.id === `screen-${selected}`);
  });
  document.querySelectorAll(".atlas-nav [data-screen]").forEach((button) => {
    button.classList.toggle("active", button.dataset.screen === selected);
  });
  byId("screen-kicker").textContent = screenMeta[selected][0];
  byId("screen-title").textContent = screenMeta[selected][1];
  if (updateHash) history.replaceState(null, "", `#/${selected}`);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function templateCard(template) {
  const card = element("article", "template-card");
  const icon = element("i", "", template.category === "Форум" ? "↗" : "▤");
  const copy = element("span");
  copy.append(element("b", "", template.name), element("small", "", template.description || template.category));
  const button = element("button", "", "Использовать");
  button.type = "button";
  button.addEventListener("click", () => openDocumentDialog(template));
  card.append(icon, copy, button);
  return card;
}

function documentCard(document) {
  const card = element("article", "document-card");
  const icon = element("i", "", "◇");
  const copy = element("span");
  copy.append(
    element("b", "", document.title),
    element("small", "", `${document.template_name || "Без шаблона"} · ${document.status || "draft"} · r${document.revision || 1}`),
  );
  const state = element("small", "", new Date(document.updated_at).toLocaleDateString("ru-RU"));
  card.append(icon, copy, state);
  return card;
}

function renderOnboarding(membership) {
  const step = Number(membership?.onboarding_step || 0);
  const card = byId("onboarding-card");
  card.hidden = step >= 4;
  document.querySelectorAll("#onboarding-steps [data-step]").forEach((button) => {
    button.classList.toggle("done", Number(button.dataset.step) <= step);
  });
}

function render(data) {
  appState.data = data;
  const viewer = data.viewer || {};
  const organization = data.organization || {};
  const membership = data.membership || {};
  byId("viewer-name").textContent = viewer.name || "Участник";
  byId("viewer-avatar").textContent = String(viewer.name || "A").charAt(0).toUpperCase();
  byId("space-name").textContent = organization.name || "Личное пространство";
  byId("space-role").textContent = `${membership.role || "member"} · ${organization.kind || "project"}`;
  byId("atlas-admin-link").hidden = !viewer.administrator;
  const counts = data.counts || {};
  byId("metric-documents").textContent = String(counts.documents || 0);
  byId("metric-knowledge").textContent = String(counts.knowledge || 0);
  byId("metric-members").textContent = String(counts.members || 0);
  byId("metric-threads").textContent = String(counts.threads || 0);
  byId("vault-count").textContent = String(counts.knowledge || 0);
  renderOnboarding(membership);

  const ai = data.ai || {};
  const aiState = byId("ai-state");
  aiState.classList.toggle("warning", !ai.configured || ai.qdrant !== "ok");
  aiState.querySelector("small").textContent = ai.configured && ai.qdrant === "ok" ? "готов" : "настройка";
  byId("context-model").textContent = ai.chat_model || "модель не настроена";
  byId("context-collection").textContent = `${ai.collection || "коллекция"} · ${ai.qdrant || "disabled"}`;

  const templates = Array.isArray(data.templates) ? data.templates : [];
  const templateList = byId("template-list");
  clear(templateList);
  templates.forEach((item) => templateList.append(templateCard(item)));
  if (!templates.length) templateList.append(element("div", "empty", "Шаблоны ещё не опубликованы."));
  byId("template-count").textContent = String(templates.length);
  const select = byId("document-template");
  clear(select);
  select.append(new Option("Без шаблона", ""));
  templates.forEach((item) => select.append(new Option(`${item.category} · ${item.name}`, String(item.id))));

  const documents = Array.isArray(data.documents) ? data.documents : [];
  const documentList = byId("document-list");
  clear(documentList);
  documents.forEach((item) => documentList.append(documentCard(item)));
  if (!documents.length) documentList.append(element("div", "empty", "Создайте первый документ из шаблона."));
  byId("document-count").textContent = String(documents.length);

  const knowledgeEditor = byId("knowledge-editor");
  knowledgeEditor.classList.toggle("locked", !viewer.administrator);
  if (!viewer.administrator) {
    knowledgeEditor.querySelector("h3").textContent = "Добавление источников доступно администратору";
  }
}

function messageNode(role, text, citations = []) {
  const row = element("div", role === "user" ? "user-message" : "assistant-message");
  if (role !== "user") row.append(element("span", "", "A"));
  const copy = element("div");
  if (role !== "user") copy.append(element("small", "", "ATLAS · GROUNDED RESPONSE"));
  copy.append(element("p", "", text));
  if (citations.length) {
    const list = element("div", "citation-list");
    citations.forEach((citation) => {
      const node = citation.url ? element("a", "", `[${citation.index}] ${citation.title}`) : element("span", "", `[${citation.index}] ${citation.title}`);
      if (citation.url) {
        node.href = citation.url;
        node.target = "_blank";
        node.rel = "noreferrer";
      }
      list.append(node);
    });
    copy.append(list);
  }
  row.append(copy);
  return row;
}

async function sendQuestion(question) {
  const stream = byId("chat-stream");
  stream.append(messageNode("user", question));
  stream.scrollTop = stream.scrollHeight;
  const button = byId("atlas-chat-form").querySelector("button[type='submit']");
  button.disabled = true;
  button.textContent = "Atlas думает…";
  try {
    const result = await api("/api/atlas/chat", {
      method: "POST",
      body: JSON.stringify({ question }),
      timeout: 60000,
    });
    stream.append(messageNode("assistant", result.answer, result.citations || []));
    byId("atlas-question").value = "";
  } catch (error) {
    stream.append(messageNode("assistant", error.message || "Ответ временно недоступен."));
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Отправить ↑";
    stream.scrollTop = stream.scrollHeight;
  }
}

function openDocumentDialog(template = null) {
  appState.selectedTemplate = template;
  byId("document-template").value = template ? String(template.id) : "";
  openDialog(byId("document-dialog"));
}

async function reload() {
  const data = await api("/api/atlas/bootstrap");
  if (data.preview) appState.data = data;
  else render(data);
}

function bind() {
  document.querySelectorAll(".atlas-nav [data-screen]").forEach((button) => {
    button.addEventListener("click", () => switchScreen(button.dataset.screen));
  });
  document.querySelectorAll("[data-jump]").forEach((node) => {
    node.addEventListener("click", () => switchScreen(node.dataset.jump));
  });
  document.querySelectorAll("[data-close]").forEach((button) => {
    button.addEventListener("click", () => closeDialog(button.closest("dialog")));
  });
  [byId("onboarding-dialog"), byId("document-dialog")].forEach((dialog) => {
    dialog.addEventListener("click", (event) => { if (event.target === dialog) closeDialog(dialog); });
  });
  byId("continue-onboarding").addEventListener("click", () => openDialog(byId("onboarding-dialog")));
  document.querySelectorAll("#onboarding-steps [data-step]").forEach((button) => {
    button.addEventListener("click", () => openDialog(byId("onboarding-dialog")));
  });
  byId("onboarding-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.currentTarget));
    try {
      await api("/api/atlas/onboarding", { method: "POST", body: JSON.stringify({ step: 4, profile: values }) });
      closeDialog(byId("onboarding-dialog"));
      await reload();
      showToast("Рабочий профиль Atlas настроен.");
    } catch (error) { showToast(error.message, true); }
  });
  byId("atlas-chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const question = byId("atlas-question").value.trim();
    if (question) void sendQuestion(question);
  });
  byId("new-document").addEventListener("click", () => openDocumentDialog());
  byId("document-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    try {
      await api("/api/atlas/documents", {
        method: "POST",
        body: JSON.stringify({ title: values.title, template_id: values.template_id || null, fields: { content: values.content || "" }, rendered_text: values.content || "" }),
      });
      closeDialog(byId("document-dialog"));
      form.reset();
      await reload();
      showToast("Черновик документа создан.");
    } catch (error) { showToast(error.message, true); }
  });
  byId("knowledge-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    const button = form.querySelector("button[type='submit']");
    button.disabled = true;
    button.textContent = "Индексируем…";
    try {
      const result = await api("/api/atlas/knowledge", { method: "POST", body: JSON.stringify(values), timeout: 15000 });
      form.reset();
      await reload();
      showToast(result.message || "Источник принят и индексируется в фоне.");
    } catch (error) { showToast(error.message, true); }
    finally { button.disabled = false; button.textContent = "Добавить и проиндексировать →"; }
  });
  byId("global-search").addEventListener("click", () => {
    switchScreen("ai");
    byId("atlas-question").focus();
    showToast("Глобальный поиск будет расширен; сейчас используйте Atlas AI.");
  });
  window.addEventListener("hashchange", () => switchScreen(location.hash.replace(/^#\//, ""), false));
}

async function bootstrap() {
  bind();
  try {
    await reload();
    byId("loading").hidden = true;
    if (appState.data?.preview) {
      byId("atlas-preview").hidden = false;
      return;
    }
    byId("atlas-app").hidden = false;
    switchScreen(location.hash.replace(/^#\//, "") || "home", false);
  } catch (error) {
    if (!String(error.message).includes("Требуется вход")) {
      byId("loading").querySelector("span").textContent = "Atlas временно недоступен";
      byId("loading").querySelector("small").textContent = error.message || "проверьте контейнер T-Mod";
    }
  }
}

void bootstrap();
