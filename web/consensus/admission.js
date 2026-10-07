"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const state = {
    csrf: "", questions: [], characters: [], step: 0, kind: null,
    busy: false, application: null, viewer: null, alreadyMember: false,
    canReview: false, pendingReviews: [],
  };
  const surfaces = ["account-gate", "senator-card", "route-choice", "status-card", "review-panel", "form-shell"];
  const eventCopy = {
    community_submitted: "Заявка передана Совету председателей",
    leadership_approved: "Совет председателей одобрил вступление",
    leadership_denied: "Совет председателей отклонил заявку",
    submitted: "Сенатская заявка зарегистрирована и передана в ОВР",
    senate_upgrade: "Открыта сенатская траектория",
    ovr_approved: "ОВР допустил кандидатуру до консенсуса",
    ovr_denied: "ОВР завершил проверку отказом в допуске",
    consensus_queued: "T-Mod создал инициативу для консенсуса",
    consensus_result: "Пленарный консенсус принял решение",
  };

  const show = (id, visible) => { const element = byId(id); if (element) element.hidden = !visible; };
  const escapeHtml = (value) => { const node = document.createElement("div"); node.textContent = String(value ?? ""); return node.innerHTML; };
  const selectSurface = (activeId) => surfaces.forEach((id) => show(id, id === activeId));
  const flow = () => state.kind === "community" ? [0, 1, 3] : [0, 1, 2, 3];
  const formatMoment = (value) => {
    const date = new Date(value || 0);
    if (Number.isNaN(date.getTime())) return "—";
    return new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
  };
  function toast(copy, kind = "success") {
    const element = byId("toast");
    element.textContent = String(copy || "Готово"); element.dataset.kind = kind; element.hidden = false;
    clearTimeout(toast.timer); toast.timer = setTimeout(() => { element.hidden = true; }, 5200);
  }
  async function api(path, options = {}) {
    const response = await fetch(path, {
      cache: "no-store", credentials: "same-origin", ...options,
      headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json", "X-CSRF-Token": state.csrf } : {}), ...(options.headers || {}) },
    });
    let payload = {}; try { payload = await response.json(); } catch { payload = {}; }
    if (!response.ok) throw new Error(payload.message || payload.error || `Ошибка ${response.status}`);
    return payload;
  }

  function renderIdentity(payload) {
    const authenticated = Boolean(payload.authenticated && payload.viewer);
    state.viewer = authenticated ? payload.viewer : null;
    show("top-login", !authenticated); show("account-chip", authenticated);
    if (authenticated) byId("account-name").textContent = payload.viewer.name || "T-Mod Account";
    state.canReview = Boolean(payload.can_review);
    state.pendingReviews = payload.pending_reviews || [];
    show("review-link", state.canReview);
    const communityButton = document.querySelector('[data-kind="community"]');
    if (communityButton) {
      communityButton.disabled = Boolean(payload.already_member);
      communityButton.setAttribute(
        "aria-label",
        payload.already_member
          ? "Вы уже состоите в Товариществе"
          : "Подать заявку в Товарищество",
      );
    }
  }
  function renderAccountGate(payload) {
    const authenticated = Boolean(payload.authenticated && payload.viewer);
    byId("account-gate").dataset.mode = authenticated ? "character-required" : "login-required";
    show("gate-login", !authenticated); show("gate-session", authenticated);
    if (authenticated) {
      byId("gate-session-name").textContent = payload.viewer.name || "T-Mod Account";
      byId("gate-eyebrow").textContent = "АККАУНТ ПОДКЛЮЧЁН · НУЖЕН ПЕРСОНАЖ";
      byId("gate-title").innerHTML = "Завершите игровую<br>идентичность";
      byId("gate-copy").textContent = "Вы уже авторизованы. Добавьте в /account хотя бы одного персонажа — повторно входить не нужно.";
      byId("discord-account-action").textContent = "Добавить персонажа в Discord";
    } else {
      byId("gate-eyebrow").textContent = "ШАГ НОЛЬ · ИДЕНТИЧНОСТЬ";
      byId("gate-title").innerHTML = "Сначала — ваш<br>T-Mod аккаунт";
      byId("gate-copy").textContent = "Он связывает персонажей, заявку, решения председателей, ОВР и уведомления лично с вами.";
      byId("discord-account-action").textContent = "Открыть T-Mod в Discord";
    }
    selectSurface("account-gate");
  }

  function characterRow(item = {}) {
    const row = document.createElement("div"); row.className = "character-row";
    row.innerHTML = `<b>${String(byId("characters").children.length + 1).padStart(2, "0")}</b><label><span>Имя и фамилия персонажа</span><input data-field="nickname" maxlength="80" placeholder="Saul Goodman" value="${escapeHtml(item.nickname || "")}" required></label><label><span>Статик</span><input data-field="static_id" maxlength="12" inputmode="numeric" placeholder="263345" value="${escapeHtml(item.static_id || "")}" required></label><button type="button" aria-label="Удалить персонажа">×</button>`;
    row.querySelector("button").addEventListener("click", () => {
      if (byId("characters").children.length <= 1) return toast("В заявке должен остаться хотя бы один персонаж.", "error");
      row.remove(); renumberCharacters();
    });
    return row;
  }
  function renumberCharacters() {
    [...byId("characters").children].forEach((row, index) => { row.querySelector("b").textContent = String(index + 1).padStart(2, "0"); });
    byId("add-character").hidden = byId("characters").children.length >= 3;
  }
  const collectCharacters = () => [...byId("characters").children].map((row) => ({ nickname: row.querySelector('[data-field="nickname"]').value.trim(), static_id: row.querySelector('[data-field="static_id"]').value.trim() }));
  const collectAnswers = () => Object.fromEntries(state.questions.map((question) => [question.id, document.querySelector(`input[name="question-${CSS.escape(question.id)}"]:checked`)?.value || ""]));

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

  function selectKind(kind) {
    if (!['community', 'senate'].includes(kind)) return;
    if (kind === "community" && state.alreadyMember) return toast("Вы уже состоите в Товариществе. Доступна сенатская траектория.", "error");
    state.kind = kind;
    const community = kind === "community";
    show("test-step-button", !community); show("forum-field", !community);
    byId("forum-url").required = !community;
    byId("form-kind-label").textContent = community ? "ТОВАРИЩЕСТВО · КОРОТКАЯ ЗАЯВКА" : "СЕНАТ · PHOENIX 15";
    byId("form-kind-title").innerHTML = community ? "Познакомимся<br>поближе" : "Расскажите<br>о себе";
    byId("form-kind-copy").textContent = community ? "Три коротких блока. После отправки заявку увидят председатели." : "Расширенная анкета перейдёт в закрытый контур ОВР.";
    byId("review-warning").textContent = community ? "Решение по заявке примет Совет председателей без консенсуса." : "При отказе ОВР путь в Сенат закрывается окончательно.";
    byId("consent-copy").textContent = community ? "Я понимаю, что статус участника не даёт права голоса и сенатских преимуществ." : "Я понимаю, что заявка передаётся в ОВР и только после допуска попадёт на консенсус. Отказ ОВР окончателен.";
    byId("submit-form").textContent = community ? "Передать Совету председателей" : "Зафиксировать и передать в ОВР";
    selectSurface("form-shell"); setStep(0);
  }

  function validateStep(step) {
    if (step === 0) {
      if (state.kind === "senate" && !byId("forum-url").checkValidity()) return "Укажите полную ссылку на форумный профиль.";
      const characters = collectCharacters();
      if (!characters.length || characters.some((item) => item.nickname.split(/\s+/).length < 2 || !/^\d{1,12}$/.test(item.static_id))) return "Проверьте имя, фамилию и цифровой статик каждого персонажа.";
      if (new Set(characters.map((item) => String(Number(item.static_id)))).size !== characters.length) return "Статики персонажей не должны повторяться.";
    }
    if (step === 1) {
      const minimum = state.kind === "community" ? 10 : 20;
      if (byId("motivation").value.trim().length < minimum) return "Расскажите немного подробнее о мотивации.";
      if (byId("contribution").value.trim().length < 10) return "Опишите, чем вы готовы быть полезны.";
      if (byId("availability").value.trim().length < 3) return "Укажите вашу доступность.";
    }
    if (step === 2 && state.kind === "senate" && Object.values(collectAnswers()).some((value) => !value)) return "Ответьте на каждый вопрос ситуационного теста.";
    if (step === 3 && !byId("consent").checked) return "Подтвердите достоверность сведений.";
    return "";
  }
  function renderFormReview() {
    const characters = collectCharacters();
    const blocks = [
      ["ПЕРСОНАЖИ", `${characters.map((item) => `${item.nickname} · #${item.static_id}`).join("\n")}${state.kind === "senate" ? `\n${byId("forum-url").value.trim()}` : ""}`],
      ["МОТИВАЦИЯ", byId("motivation").value.trim()],
      ["ВКЛАД И ДОСТУПНОСТЬ", `${byId("contribution").value.trim()}\n\n${byId("availability").value.trim()}`],
    ];
    byId("review").replaceChildren(...blocks.map(([title, copy]) => {
      const article = document.createElement("article"); article.innerHTML = `<small>${title}</small><h4>${title === "ПЕРСОНАЖИ" ? "Игровая идентичность" : title === "МОТИВАЦИЯ" ? "Намерение" : "Ресурсы участия"}</h4><p></p>`; article.querySelector("p").textContent = copy; return article;
    }));
  }
  function setStep(step) {
    const available = flow(); state.step = available.includes(Number(step)) ? Number(step) : available[0];
    const activeIndex = available.indexOf(state.step);
    document.querySelectorAll(".form-step").forEach((section) => { section.hidden = Number(section.dataset.step) !== state.step; });
    document.querySelectorAll("#step-nav button").forEach((button) => {
      const index = available.indexOf(Number(button.dataset.step));
      button.dataset.state = index >= 0 && index < activeIndex ? "done" : index === activeIndex ? "active" : "";
    });
    byId("form-progress").style.width = `${((activeIndex + 1) / available.length) * 100}%`;
    byId("back-step").style.visibility = activeIndex > 0 ? "visible" : "hidden";
    byId("next-step").hidden = activeIndex === available.length - 1; byId("submit-form").hidden = activeIndex !== available.length - 1;
    byId("form-message").textContent = ""; if (state.step === 3) renderFormReview();
    byId("form-shell").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  function moveStep(direction) {
    const available = flow(); const index = available.indexOf(state.step); const target = available[index + direction];
    if (target !== undefined) setStep(target);
  }

  function statusPresentation(application) {
    const community = application.application_kind === "community";
    if (community) {
      const copy = {
        chair_review: ["Заявка у Совета председателей", "Председатели изучают анкету. ОВР и пленарный консенсус для этой траектории не требуются."],
        membership_approved: ["Вы приняты в Товарищество", "Доступ участника активирован. Голосование и сенатские преимущества остаются доступны только сенаторам."],
        membership_denied: ["Заявка рассмотрена", "Совет председателей не одобрил вступление в Товарищество."],
      };
      return { copy: copy[application.status], stages: [["Аккаунт", "Идентичность"], ["Заявка", "Знакомство"], ["Совет", "Решение"], ["Доступ", "Итог"]], active: application.status === "chair_review" ? 2 : 3 };
    }
    const copy = {
      ovr_review: ["Заявка находится в ОВР", "Сотрудники ОВР проводят обязательную проверку сроком до 48 часов."],
      ovr_approved: ["ОВР одобрил допуск", "T-Mod готовит инициативу для пленарного консенсуса."],
      ovr_denied: ["ОВР отказал в допуске", "Решение окончательно. Кандидат не передаётся на консенсус и не вступит в Сенат Phoenix."],
      consensus_queued: ["Кандидатура передана на консенсус", "После решения T-Mod сразу сообщит результат."],
      membership_approved: ["Вы приняты в Сенат Phoenix", "Один из сопредседателей свяжется с вами и завершит IC-процедуру."],
      membership_denied: ["Консенсус не принял кандидатуру", "Инициатива о вступлении в Сенат Phoenix не была принята."],
    };
    const active = { ovr_review: 1, ovr_approved: 2, ovr_denied: 2, consensus_queued: 3, membership_approved: 4, membership_denied: 4 }[application.status] ?? 0;
    return { copy: copy[application.status], stages: [["Аккаунт", "Идентичность"], ["ОВР", "Проверка"], ["Допуск", "Решение ОВР"], ["Консенсус", "Голосование"], ["Мандат", "Итог"]], active };
  }
  function renderStatus(application, events) {
    state.application = application;
    const presentation = statusPresentation(application); const [title, copy] = presentation.copy || ["Статус заявки обновлён", "T-Mod продолжает сопровождать процесс."];
    byId("status-title").textContent = title; byId("status-copy").textContent = copy;
    byId("status-reference").textContent = application.ovr_case_number ? `ОВР-${String(application.ovr_case_number).padStart(3, "0")}` : `TVR-${String(application.id).padStart(4, "0")}`;
    byId("status-track").replaceChildren(...presentation.stages.map(([label, caption], index) => { const article = document.createElement("article"); article.dataset.state = index < presentation.active ? "done" : index === presentation.active ? "active" : "future"; article.innerHTML = `<b>${String(index + 1).padStart(2, "0")}</b><i></i><strong>${label}</strong><small>${caption}</small>`; return article; }));
    byId("status-characters").replaceChildren(...(application.characters || []).map((item) => { const row = document.createElement("div"); row.className = "status-character"; const initials = item.nickname.split(/\s+/).map((part) => part[0]).join("").slice(0, 2).toUpperCase(); row.innerHTML = `<i>${escapeHtml(initials)}</i><span><strong>${escapeHtml(item.nickname)}</strong><small>Статик #${escapeHtml(item.static_id)}</small></span>`; return row; }));
    byId("status-events").replaceChildren(...events.slice().reverse().map((event) => { const row = document.createElement("article"); row.className = "event"; row.innerHTML = `<strong>${escapeHtml(eventCopy[event.action] || event.action)}</strong><p>${escapeHtml(event.note || "Статус зафиксирован.")}</p><time>${escapeHtml(formatMoment(event.created_at))}</time>`; return row; }));
    byId("status-updated").textContent = `Обновлено ${formatMoment(application.updated_at)}`;
    show("senate-upgrade", application.application_kind === "community" && application.status === "membership_approved");
    selectSurface("status-card");
  }

  function renderReviewPanel() {
    byId("review-count").textContent = String(state.pendingReviews.length).padStart(2, "0");
    const list = byId("review-list"); list.replaceChildren();
    if (!state.pendingReviews.length) {
      const empty = document.createElement("div"); empty.className = "review-empty"; empty.innerHTML = "<strong>Очередь чиста</strong><p>Новых заявок в Товарищество сейчас нет.</p>"; list.append(empty);
    }
    state.pendingReviews.forEach((application) => {
      const card = document.createElement("article"); card.className = "chair-application"; card.dataset.id = application.id;
      const people = (application.characters || []).map((item) => `${escapeHtml(item.nickname)} · #${escapeHtml(item.static_id)}`).join("<br>");
      card.innerHTML = `<header><span>TVR-${String(application.id).padStart(4, "0")}</span><time>${escapeHtml(formatMoment(application.created_at))}</time></header><h3>${escapeHtml(application.user_display)}</h3><p class="candidate-characters">${people}</p><dl><div><dt>Мотивация</dt><dd>${escapeHtml(application.motivation || "—")}</dd></div><div><dt>Вклад</dt><dd>${escapeHtml(application.contribution || "—")}</dd></div><div><dt>Доступность</dt><dd>${escapeHtml(application.availability || "—")}</dd></div></dl><label><span>Мотивировка решения</span><textarea maxlength="5000" placeholder="Не менее пяти символов"></textarea></label><footer><button type="button" data-action="deny">Отклонить</button><button type="button" data-action="approve">Принять в Товарищество</button></footer>`;
      card.querySelectorAll("button").forEach((button) => button.addEventListener("click", () => decideApplication(card, button.dataset.action)));
      list.append(card);
    });
    selectSurface("review-panel");
  }
  async function decideApplication(card, action) {
    if (state.busy) return; const note = card.querySelector("textarea").value.trim();
    if (note.length < 5) return toast("Добавьте краткую мотивировку решения.", "error");
    state.busy = true; card.dataset.busy = "true";
    try {
      await api("/api/admission/review", { method: "POST", body: JSON.stringify({ application_id: Number(card.dataset.id), action, note }) });
      state.pendingReviews = state.pendingReviews.filter((item) => Number(item.id) !== Number(card.dataset.id));
      renderReviewPanel(); toast(action === "approve" ? "Участник принят. Роль поставлена в очередь выдачи." : "Решение об отказе сохранено.");
    } catch (error) { toast(error.message, "error"); card.dataset.busy = "false"; }
    finally { state.busy = false; }
  }

  async function bootstrap({ silent = false } = {}) {
    try {
      const payload = await api("/api/admission"); state.csrf = payload.csrf_token || ""; state.questions = payload.questions || []; state.characters = payload.characters || []; state.alreadyMember = Boolean(payload.already_member);
      renderIdentity(payload); if (!silent) show("loading-card", false);
      if (!payload.authenticated) return renderAccountGate(payload);
      if (state.canReview && location.hash === "#review") return renderReviewPanel();
      if (payload.account_required) return renderAccountGate(payload);
      if (payload.already_senator) return selectSurface("senator-card");
      if (payload.application) return renderStatus(payload.application, payload.events || []);
      if (!silent) selectSurface("route-choice");
    } catch (error) { if (!silent) { show("loading-card", false); show("account-gate", true); } toast(error.message, "error"); }
  }

  document.querySelectorAll("[data-kind]").forEach((button) => button.addEventListener("click", () => {
    byId("characters").replaceChildren(...(state.characters.length ? state.characters : [{}]).map(characterRow)); renumberCharacters(); renderQuestions(); selectKind(button.dataset.kind);
  }));
  byId("add-character").addEventListener("click", () => { if (byId("characters").children.length >= 3) return; byId("characters").append(characterRow()); renumberCharacters(); });
  byId("next-step").addEventListener("click", () => { const error = validateStep(state.step); if (error) return void (byId("form-message").textContent = error); moveStep(1); });
  byId("back-step").addEventListener("click", () => moveStep(-1));
  document.querySelectorAll("#step-nav button").forEach((button) => button.addEventListener("click", () => { const target = Number(button.dataset.step); const available = flow(); if (available.indexOf(target) <= available.indexOf(state.step)) setStep(target); }));
  document.querySelectorAll("textarea[maxlength]").forEach((field) => field.addEventListener("input", () => { const counter = document.querySelector(`[data-count="${field.id}"]`); if (counter) counter.textContent = String(field.value.length); }));
  byId("review-link").addEventListener("click", () => { location.hash = "review"; renderReviewPanel(); });
  byId("senate-upgrade").addEventListener("click", () => { byId("characters").replaceChildren(...(state.characters.length ? state.characters : state.application.characters || [{}]).map(characterRow)); renumberCharacters(); renderQuestions(); selectKind("senate"); });
  byId("admission-form").addEventListener("submit", async (event) => {
    event.preventDefault(); if (state.busy) return; const error = validateStep(3); if (error) return void (byId("form-message").textContent = error);
    state.busy = true; byId("submit-form").disabled = true; const idleLabel = state.kind === "community" ? "Передать Совету председателей" : "Зафиксировать и передать в ОВР"; byId("submit-form").textContent = "Фиксируем заявку…";
    try {
      const payload = await api("/api/admission", { method: "POST", headers: { "X-Idempotency-Key": `${Date.now()}-${crypto.randomUUID?.() || Math.random().toString(36).slice(2)}` }, body: JSON.stringify({ application_kind: state.kind, forum_url: byId("forum-url").value.trim(), characters: collectCharacters(), motivation: byId("motivation").value.trim(), contribution: byId("contribution").value.trim(), availability: byId("availability").value.trim(), answers: state.kind === "senate" ? collectAnswers() : {} }) });
      renderStatus(payload.application, payload.events || []); toast(state.kind === "community" ? "Заявка передана Совету председателей." : "Заявка зафиксирована и передана в ОВР.");
    } catch (submitError) { byId("form-message").textContent = submitError.message; toast(submitError.message, "error"); }
    finally { state.busy = false; byId("submit-form").disabled = false; byId("submit-form").textContent = idleLabel; }
  });
  addEventListener("hashchange", () => { if (location.hash === "#review" && state.canReview) renderReviewPanel(); });
  bootstrap(); setInterval(() => { if ((state.application || location.hash === "#review") && document.visibilityState === "visible" && !state.busy) bootstrap({ silent: true }); }, 30000);
})();
