"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const allowed = new Set(["/", "/admin", "/reactor", "/atlas", "/atlas-billing", "/account", "/games", "/sgl", "/ovr", "/host", "/tasks", "/admission"]);
  const wanted = new URLSearchParams(location.search).get("next");
  const next = allowed.has(wanted) ? wanted : "/";
  for (const id of ["back-login", "finish-login"]) $(id).href = `/login?next=${encodeURIComponent(next)}`;
  let stage = 0, csrf = "", expires = 0, timer, polling = false, busy = false, verified = false, existing = [];
  // Storage may be disabled in a private browser; the HttpOnly cookie still owns the wizard.
  const savedCode = {
    get() { try { return sessionStorage.getItem("tmod-registration-code"); } catch { return null; } },
    set(value) { try { sessionStorage.setItem("tmod-registration-code", value); } catch {} },
    clear() { try { sessionStorage.removeItem("tmod-registration-code"); } catch {} },
  };
  const showError = message => { $("wizard-feedback").textContent = message; $("wizard-feedback").hidden = !message; };
  async function api(path, body) {
    const response = await fetch(`/api/account-registration/${path}`, { method: body ? "POST" : "GET", cache: "no-store", credentials: "same-origin",
      headers: { Accept: "application/json", ...(body ? { "Content-Type": "application/json", "X-CSRF-Token": csrf } : {}) },
      ...(body ? { body: JSON.stringify(body) } : {}), signal: AbortSignal.timeout(15000) });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) { const error = new Error(result.message || "Не удалось связаться с системой. Повторите попытку."); error.status = response.status; throw error; }
    return result;
  }
  function go(value) {
    stage = value; showError("");
    document.querySelectorAll("[data-stage]").forEach(element => { element.hidden = Number(element.dataset.stage) !== value; });
    $("wizard-form").hidden = value === 0;
    $("wizard-loading").hidden = true;
    $("wizard-next").hidden = value === 3;
    $("wizard-save").hidden = value !== 3;
    $("wizard-back").disabled = value === 1;
    document.querySelectorAll("#wizard-steps li").forEach((element, index) => { element.classList.toggle("active", index === value); element.classList.toggle("done", index < value); });
    if (value === 3) review();
    const heading = document.querySelector(`[data-stage="${value}"] h2`);
    heading?.setAttribute("tabindex", "-1"); heading?.focus({ preventScroll: true });
  }
  function success(login) {
    clearTimeout(timer); $("wizard-form").hidden = true; $("wizard-loading").hidden = true;
    document.querySelectorAll("[data-stage]").forEach(element => { element.hidden = true; });
    $("wizard-success").hidden = false; $("created-login").textContent = login;
    $("new-password").value = ""; $("confirm-password").value = "";
    savedCode.clear(); showError("");
    document.querySelectorAll("#wizard-steps li").forEach(element => { element.classList.remove("active"); element.classList.add("done"); });
  }
  function characters() { return [...$("new-characters").children].map(row => ({ nickname: row.querySelector('[data-character="nickname"]').value.trim(), static_id: row.querySelector('[data-character="static_id"]').value.trim() })); }
  function updateCharacterButton() { $("add-wizard-character").hidden = existing.length + $("new-characters").children.length >= 3; }
  function addCharacter() {
    if (existing.length + $("new-characters").children.length >= 3) return;
    const row = document.createElement("article"); row.className = "wizard-character";
    row.innerHTML = '<header><span>Персонаж · Phoenix</span><button type="button" aria-label="Удалить персонажа">Убрать</button></header><label class="field"><span>Имя и фамилия</span><input data-character="nickname" maxlength="48" autocomplete="off" placeholder="Robert Bailey" required></label><label class="field"><span>Статик</span><input data-character="static_id" inputmode="numeric" pattern="[0-9]{1,12}" maxlength="12" autocomplete="off" placeholder="263345" required></label>';
    row.querySelector("button").addEventListener("click", () => { row.remove(); updateCharacterButton(); });
    $("new-characters").append(row); updateCharacterButton(); row.querySelector("input").focus();
  }
  function review() {
    const values = [["Discord", $("discord-name").textContent], ["Как обращаться", $("preferred-name").value.trim()], ["Логин", $("new-login").value.trim().toLowerCase()], ["Способ входа", document.querySelector('[name="kind"]:checked').value === "pin" ? "PIN из 8 цифр" : "Пароль"], ["Персонажи · Phoenix", [...existing, ...characters()].map(item => `${item.nickname} · #${item.static_id}`).join(" / ") || "Добавите позже"]];
    $("wizard-review").replaceChildren(...values.map(([label, text]) => { const row = document.createElement("div"), dt = document.createElement("dt"), dd = document.createElement("dd"); dt.textContent = label; dd.textContent = text; row.append(dt, dd); return row; }));
  }
  async function poll() {
    if (polling || verified || document.hidden) return;
    polling = true;
    try {
      const state = await api("status"); csrf = state.csrf_token; expires = state.expires_at;
      if (state.status === "completed") { verified = true; success(state.login); return; }
      if (state.status === "claimed") {
        verified = true; savedCode.clear(); $("discord-name").textContent = state.discord_name;
        existing = state.characters || []; $("existing-characters").hidden = !existing.length;
        $("existing-characters").replaceChildren(...existing.map(item => { const row = document.createElement("div"); row.className = "existing-character"; row.textContent = `${item.nickname} · #${item.static_id}`; const note = document.createElement("small"); note.textContent = "Уже привязан в Discord — сохраняем без изменений"; row.append(note); return row; }));
        updateCharacterButton(); go(1); return;
      }
      $("pairing-status").textContent = "Ждём подтверждения в Discord";
    } catch (error) {
      if (error.status === 410 || error.status === 423) { clearTimeout(timer); $("pairing").hidden = true; $("start-pairing").hidden = false; showError(error.message); return; }
      $("pairing-status").textContent = "Связь прервалась. Повторяем проверку…";
    } finally { polling = false; }
    if (!verified) timer = setTimeout(poll, 4000);
  }
  $("start-pairing").addEventListener("click", async () => {
    if (busy) return; busy = true; $("start-pairing").disabled = true; showError(""); clearTimeout(timer);
    try { const result = await api("start", {}); csrf = result.csrf_token; expires = result.expires_at;
      savedCode.set(result.pairing_code);
      $("pairing-command").textContent = `/master ${result.pairing_code}`; $("pairing").hidden = false; $("start-pairing").hidden = true;
      void poll();
    } catch (error) { showError(error.message); }
    finally { busy = false; $("start-pairing").disabled = false; }
  });
  $("copy-pairing").addEventListener("click", async () => { try { await navigator.clipboard.writeText($("pairing-command").textContent); $("copy-pairing").textContent = "Скопировано"; setTimeout(() => { $("copy-pairing").textContent = "Копировать"; }, 1800); } catch { showError("Выделите команду и скопируйте вручную."); } });
  document.querySelectorAll('[name="kind"]').forEach(input => input.addEventListener("change", () => {
    const pin = input.value === "pin"; $("new-password").value = ""; $("confirm-password").value = "";
    $("new-password").minLength = pin ? 8 : 12; $("new-password").maxLength = pin ? 8 : 128; $("new-password").inputMode = pin ? "numeric" : "text";
    if (pin) $("new-password").pattern = "[0-9]{8}"; else $("new-password").removeAttribute("pattern");
    $("secret-label").textContent = pin ? "PIN-код" : "Пароль"; $("secret-hint").textContent = pin ? "Ровно 8 цифр. Не используйте дату рождения." : "От 12 до 128 символов. Не используйте пароль от Discord.";
  }));
  $("add-wizard-character").addEventListener("click", addCharacter);
  $("wizard-back").addEventListener("click", () => { if (!busy && stage > 1) go(stage - 1); });
  $("wizard-next").addEventListener("click", () => {
    if (busy) return;
    const current = document.querySelector(`[data-stage="${stage}"]`);
    for (const input of current.querySelectorAll("input")) { if (!input.checkValidity()) { input.reportValidity(); return; } }
    if (stage === 1 && $("new-password").value !== $("confirm-password").value) { showError("PIN или пароль не совпали. Проверьте оба поля."); $("confirm-password").focus(); return; }
    go(stage + 1);
  });
  $("wizard-form").addEventListener("submit", async event => {
    event.preventDefault(); if (busy) return;
    if (stage !== 3) { $("wizard-next").click(); return; }
    if (!$("registration-consent").checked) { $("registration-consent").reportValidity(); return; }
    busy = true; $("wizard-save").disabled = true; $("wizard-back").disabled = true; $("wizard-save").textContent = "Создаём аккаунт…"; showError("");
    try { const result = await api("complete", { login: $("new-login").value.trim(), password: $("new-password").value, kind: document.querySelector('[name="kind"]:checked').value, preferred_name: $("preferred-name").value.trim(), characters: characters(), consent: true }); success(result.login); }
    catch (error) {
      if (error.status === 410) {
        verified = false; csrf = ""; expires = 0;
        $("new-password").value = ""; $("confirm-password").value = "";
        savedCode.clear(); $("pairing").hidden = true; $("start-pairing").hidden = false;
        go(0);
      }
      showError(error.message);
    }
    finally { busy = false; $("wizard-save").disabled = false; $("wizard-back").disabled = false; $("wizard-save").textContent = "Создать аккаунт →"; }
  });
  document.addEventListener("visibilitychange", () => { clearTimeout(timer); if (!document.hidden && !verified) void poll(); });
  setInterval(() => { if (expires && !verified) { const remaining = Math.max(0, expires - Math.floor(Date.now() / 1000)); $("pairing-time").textContent = `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")}`; } }, 1000);
  go(0); const saved = savedCode.get(); if (saved) { $("pairing-command").textContent = `/master ${saved}`; $("pairing").hidden = false; $("start-pairing").hidden = true; }
  // Resume a claimed browser without putting the password or PIN in storage.
  void api("status").then(state => { csrf = state.csrf_token; expires = state.expires_at; if (state.status === "claimed" || state.status === "completed" || saved) void poll(); }).catch(() => { $("pairing").hidden = true; $("start-pairing").hidden = false; savedCode.clear(); });
})();
