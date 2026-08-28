"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const state = { csrf: "", questions: [], characters: [], step: 0, busy: false, application: null };
  const statusOrder = ["ovr_review", "ovr_approved", "consensus_queued", "membership_approved"];
  const statusCopy = {
    ovr_review: ["Заявка находится в ОВР", "Сведения переданы в закрытый контур. Сотрудники ОВР проводят проверку и подготовят мотивированное решение в срок до 48 часов."],
    ovr_approved: ["ОВР одобрил допуск", "Проверка завершена положительно. T-Mod готовит инициативу для пленарного консенсуса."],
    ovr_denied: ["Проверка ОВР завершена", "ОВР не допустил заявку к рассмотрению консенсусом. Решение сохранено в закрытом досье."],
    consensus_queued: ["Кандидатура передана на консенсус", "Инициатива находится в очереди пленарного консенсуса. После решения T-Mod сразу сообщит результат."],
    membership_approved: ["Товарищество приняло вас", "Инициатива принята. Один из сопредседателей свяжется с вами и завершит процедуру вступления."],
    membership_denied: ["Рассмотрение завершено", "Инициатива о вступлении не была принята пленарным консенсусом."],
  };
  const eventCopy = {
    submitted: "Заявка зарегистрирована и передана в ОВР",
    ovr_approved: "ОВР допустил кандидатуру до консенсуса",
    ovr_denied: "ОВР завершил проверку отказом в допуске",
    consensus_queued: "T-Mod создал инициативу для консенсуса",
    consensus_result: "Пленарный консенсус принял решение",
  };

  const show = (id, visible) => { const element = byId(id); if (element) element.hidden = !visible; };
  const formatMoment = (value) => {
    const date = new Date(value || 0);
    if (Number.isNaN(date.getTime())) return "—";
    return new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
  };
  function toast(copy, kind = "success") {
    const element = byId("toast");
    element.textContent = String(copy || "Готово");
    element.dataset.kind = kind;
    element.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { element.hidden = true; }, 5200);
  }
  async function api(path, options = {}) {
    const response = await fetch(path, {
      cache: "no-store", credentials: "same-origin", ...options,
      headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json", "X-CSRF-Token": state.csrf } : {}), ...(options.headers || {}) },
    });
    let payload = {};
    try { payload = await response.json(); } catch { payload = {}; }
    if (!response.ok) throw new Error(payload.message || payload.error || `Ошибка ${response.status}`);
    return payload;
  }

  function characterRow(item = {}) {
    const row = document.createElement("div");
    row.className = "character-row";
    row.innerHTML = `<b>${String(byId("characters").children.length + 1).padStart(2, "0")}</b><label><span>Имя и фамилия персонажа</span><input data-field="nickname" maxlength="80" placeholder="Saul Goodman" value="${escapeHtml(item.nickname || "")}" required></label><label><span>Статик</span><input data-field="static_id" maxlength="12" inputmode="numeric" placeholder="263345" value="${escapeHtml(item.static_id || "")}" required></label><button type="button" aria-label="Удалить персонажа">×</button>`;
    row.querySelector("button").addEventListener("click", () => {
      if (byId("characters").children.length <= 1) return toast("В заявке должен остаться хотя бы один персонаж.", "error");
      row.remove(); renumberCharacters();
    });
    return row;
  }
  function escapeHtml(value) { const div = document.createElement("div"); div.textContent = String(value); return div.innerHTML; }
  function renumberCharacters() { [...byId("characters").children].forEach((row, index) => { row.querySelector("b").textContent = String(index + 1).padStart(2, "0"); }); byId("add-character").hidden = byId("characters").children.length >= 3; }
  function collectCharacters() { return [...byId("characters").children].map((row) => ({ nickname: row.querySelector('[data-field="nickname"]').value.trim(), static_id: row.querySelector('[data-field="static_id"]').value.trim() })); }

  function renderQuestions() {
    byId("questions").replaceChildren(...state.questions.map((question, index) => {
      const article = document.createElement("article"); article.className = "question";
      article.innerHTML = `<small>${String(index + 1).padStart(2, "0")} / ${String(state.questions.length).padStart(2, "0")}</small><h4>${escapeHtml(question.title)}</h4>`;
      const options = document.createElement("div"); options.className = "question-options";
      question.options.forEach((option) => {
        const label = document.createElement("label"); label.className = "question-option";
        label.innerHTML = `<input type="radio" name="question-${escapeHtml(question.id)}" value="${escapeHtml(option.id)}"><span>${escapeHtml(option.label)}</span>`;
        options.append(label);
      });
      article.append(options); return article;
    }));
  }
  function collectAnswers() { return Object.fromEntries(state.questions.map((question) => [question.id, document.querySelector(`input[name="question-${CSS.escape(question.id)}"]:checked`)?.value || ""])); }
  function validateStep(step) {
    if (step === 0) {
      if (!byId("forum-url").checkValidity()) return "Укажите полную ссылку на форумный профиль.";
      const characters = collectCharacters();
      if (!characters.length || characters.some((item) => item.nickname.split(/\s+/).length < 2 || !/^\d{1,12}$/.test(item.static_id))) return "Проверьте имя, фамилию и цифровой статик каждого персонажа.";
      if (new Set(characters.map((item) => String(Number(item.static_id)))).size !== characters.length) return "Статики персонажей не должны повторяться.";
    }
    if (step === 1) {
      if (byId("motivation").value.trim().length < 20) return "Расскажите немного подробнее о мотивации.";
      if (byId("contribution").value.trim().length < 10) return "Опишите, чем вы готовы быть полезны.";
      if (byId("availability").value.trim().length < 3) return "Укажите вашу доступность.";
    }
    if (step === 2 && Object.values(collectAnswers()).some((value) => !value)) return "Ответьте на каждый вопрос ситуационного теста.";
    if (step === 3 && !byId("consent").checked) return "Подтвердите достоверность сведений.";
    return "";
  }
  function renderReview() {
    const characters = collectCharacters();
    const blocks = [
      ["ПЕРСОНАЖИ", `${characters.map((item) => `${item.nickname} · #${item.static_id}`).join("\n")}\n${byId("forum-url").value.trim()}`],
      ["МОТИВАЦИЯ", byId("motivation").value.trim()],
      ["ВКЛАД И ДОСТУПНОСТЬ", `${byId("contribution").value.trim()}\n\n${byId("availability").value.trim()}`],
    ];
    byId("review").replaceChildren(...blocks.map(([title, copy]) => { const article = document.createElement("article"); article.innerHTML = `<small>${title}</small><h4>${title === "ПЕРСОНАЖИ" ? "Игровая идентичность" : title === "МОТИВАЦИЯ" ? "Намерение" : "Ресурсы участия"}</h4><p></p>`; article.querySelector("p").textContent = copy; return article; }));
  }
  function setStep(next) {
    state.step = Math.max(0, Math.min(3, Number(next)));
    document.querySelectorAll(".form-step").forEach((section) => { section.hidden = Number(section.dataset.step) !== state.step; });
    document.querySelectorAll("#step-nav button").forEach((button) => { const value = Number(button.dataset.step); button.dataset.state = value < state.step ? "done" : value === state.step ? "active" : ""; });
    byId("form-progress").style.width = `${((state.step + 1) / 4) * 100}%`;
    byId("back-step").style.visibility = state.step ? "visible" : "hidden";
    byId("next-step").hidden = state.step === 3;
    byId("submit-form").hidden = state.step !== 3;
    byId("form-message").textContent = "";
    if (state.step === 3) renderReview();
    byId("form-shell").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderStatus(application, events) {
    state.application = application;
    const [title, copy] = statusCopy[application.status] || ["Статус заявки обновлён", "Откройте карточку позже — T-Mod продолжает сопровождать процесс."];
    byId("status-title").textContent = title; byId("status-copy").textContent = copy;
    byId("status-reference").textContent = application.ovr_case_number ? `ОВР-${String(application.ovr_case_number).padStart(3, "0")}` : `PHX-${String(application.id).padStart(4, "0")}`;
    const stages = [["Аккаунт", "Идентичность"], ["ОВР", "Проверка"], ["Допуск", "Решение ОВР"], ["Консенсус", "Голосование"], ["Вступление", "Итог"]];
    let active = statusOrder.indexOf(application.status);
    if (application.status === "ovr_denied") active = 2;
    if (application.status === "membership_denied") active = 4;
    byId("status-track").replaceChildren(...stages.map(([label, caption], index) => { const article = document.createElement("article"); article.dataset.state = index < active ? "done" : index === active ? "active" : "future"; article.innerHTML = `<b>${String(index + 1).padStart(2, "0")}</b><i></i><strong>${label}</strong><small>${caption}</small>`; return article; }));
    byId("status-characters").replaceChildren(...(application.characters || []).map((item) => { const row = document.createElement("div"); row.className = "status-character"; const initials = item.nickname.split(/\s+/).map((part) => part[0]).join("").slice(0, 2).toUpperCase(); row.innerHTML = `<i>${escapeHtml(initials)}</i><span><strong>${escapeHtml(item.nickname)}</strong><small>Статик #${escapeHtml(item.static_id)}</small></span>`; return row; }));
    byId("status-events").replaceChildren(...events.slice().reverse().map((event) => { const row = document.createElement("article"); row.className = "event"; row.innerHTML = `<strong>${escapeHtml(eventCopy[event.action] || event.action)}</strong><p>${escapeHtml(event.note || "Статус зафиксирован.")}</p><time>${escapeHtml(formatMoment(event.created_at))}</time>`; return row; }));
    byId("status-updated").textContent = `Обновлено ${formatMoment(application.updated_at)}`;
    show("status-card", true);
  }

  async function bootstrap({ silent = false } = {}) {
    try {
      const payload = await api("/api/admission"); state.csrf = payload.csrf_token || ""; state.questions = payload.questions || [];
      if (!silent) show("loading-card", false);
      if (!payload.authenticated || payload.account_required) { show("account-gate", true); return; }
      if (payload.already_senator) { show("senator-card", true); return; }
      if (payload.application) { renderStatus(payload.application, payload.events || []); return; }
      if (!silent) {
        state.characters = payload.characters || [];
        byId("characters").replaceChildren(...(state.characters.length ? state.characters : [{}]).map(characterRow)); renumberCharacters(); renderQuestions(); show("form-shell", true); setStep(0);
      }
    } catch (error) { if (!silent) { show("loading-card", false); show("account-gate", true); } toast(error.message, "error"); }
  }

  byId("add-character").addEventListener("click", () => { if (byId("characters").children.length >= 3) return; byId("characters").append(characterRow()); renumberCharacters(); });
  byId("next-step").addEventListener("click", () => { const error = validateStep(state.step); if (error) { byId("form-message").textContent = error; return; } setStep(state.step + 1); });
  byId("back-step").addEventListener("click", () => setStep(state.step - 1));
  document.querySelectorAll("#step-nav button").forEach((button) => button.addEventListener("click", () => { const target = Number(button.dataset.step); if (target <= state.step) setStep(target); }));
  document.querySelectorAll("textarea[maxlength]").forEach((field) => field.addEventListener("input", () => { const counter = document.querySelector(`[data-count="${field.id}"]`); if (counter) counter.textContent = String(field.value.length); }));
  byId("admission-form").addEventListener("submit", async (event) => {
    event.preventDefault(); if (state.busy) return; const error = validateStep(3); if (error) { byId("form-message").textContent = error; return; }
    state.busy = true; byId("submit-form").disabled = true; byId("submit-form").textContent = "Фиксируем заявку…";
    try {
      const payload = await api("/api/admission", { method: "POST", headers: { "X-Idempotency-Key": `${Date.now()}-${crypto.randomUUID?.() || Math.random().toString(36).slice(2)}` }, body: JSON.stringify({ forum_url: byId("forum-url").value.trim(), characters: collectCharacters(), motivation: byId("motivation").value.trim(), contribution: byId("contribution").value.trim(), availability: byId("availability").value.trim(), answers: collectAnswers() }) });
      show("form-shell", false); renderStatus(payload.application, payload.events || []); toast("Заявка зафиксирована и передана в ОВР.");
    } catch (submitError) { byId("form-message").textContent = submitError.message; toast(submitError.message, "error"); }
    finally { state.busy = false; byId("submit-form").disabled = false; byId("submit-form").textContent = "Зафиксировать и передать в ОВР"; }
  });

  bootstrap();
  setInterval(() => { if (state.application && document.visibilityState === "visible") bootstrap({ silent: true }); }, 30000);
})();
