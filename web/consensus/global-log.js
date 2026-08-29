(() => {
  "use strict";
  const $ = (selector) => document.querySelector(selector);
  const authShell = $("#authShell");
  const appShell = $("#appShell");
  const state = { cursor: null, events: [], filters: {}, loading: false, timer: null };
  const formatTime = (value) => value ? new Intl.DateTimeFormat("ru-RU", {dateStyle:"short",timeStyle:"medium"}).format(new Date(value)) : "—";
  const api = async (path, options = {}) => {
    const response = await fetch(path, {credentials:"same-origin", ...options, headers:{"Content-Type":"application/json", ...(options.headers || {})}});
    const data = await response.json().catch(() => ({}));
    if (response.status === 401 && !path.endsWith("/login")) showLogin();
    if (!response.ok) throw Object.assign(new Error(data.error || `HTTP ${response.status}`), {status:response.status, data});
    return data;
  };
  function showLogin() { authShell.hidden = false; appShell.hidden = true; clearInterval(state.timer); }
  function showApp() { authShell.hidden = true; appShell.hidden = false; }
  function fillSelect(name, items) {
    const select = document.querySelector(`[name="${name}"]`); if (!select) return;
    const current = select.value; while (select.options.length > 1) select.remove(1);
    for (const item of items || []) { const option=document.createElement("option"); option.value=item.value; option.textContent=`${item.value} · ${item.count}`; select.append(option); }
    select.value = current;
  }
  async function loadFacets() {
    const data = await api("/api/global-log/facets");
    $("#totalEvents").textContent = Number(data.total || 0).toLocaleString("ru-RU");
    $("#eventRange").textContent = data.first_at ? `${formatTime(data.first_at)} — ${formatTime(data.last_at)}` : "Журнал пока пуст";
    fillSelect("source", data.sources); fillSelect("source_type", data.source_types); fillSelect("event_type", data.event_types); fillSelect("severity", data.severities);
  }
  function query(filters, cursor) {
    const params = new URLSearchParams(); for (const [key,value] of Object.entries(filters)) if (value) params.set(key,value);
    if (cursor) params.set("cursor",cursor); params.set("limit","60"); return params.toString();
  }
  function statusMarkup(event) {
    if (!event.status_code) return "—";
    const good = Number(event.status_code) < 400; return `<span class="${good?"status-good":"status-bad"}">${event.status_code}</span>`;
  }
  function renderEvents(append = false) {
    const body = $("#eventsBody"); if (!append) body.replaceChildren();
    const fragment = document.createDocumentFragment();
    for (const event of state.events.slice(append ? body.children.length : 0)) {
      const row = document.createElement("tr"); row.dataset.id = event.id;
      const values = [formatTime(event.occurred_at), event.source_service, event.event_type, event.actor_display || event.actor_user_id || "Система", event.summary, ""];
      values.forEach((value,index) => { const cell=document.createElement("td");
        if(index===1){const badge=document.createElement("span");badge.className=`badge ${event.severity||""}`;badge.textContent=value;cell.append(badge);}
        else if(index===2){cell.className="event-title";cell.textContent=value;}
        else if(index===4){cell.className="event-summary";cell.textContent=value;}
        else if(index===5){cell.innerHTML=statusMarkup(event);}
        else cell.textContent=value; row.append(cell);
      });
      row.addEventListener("click", () => openDetail(event)); fragment.append(row);
    }
    body.append(fragment); $("#emptyState").hidden = state.events.length > 0; $("#resultLabel").textContent = `${state.events.length} событий в выборке`;
  }
  async function loadEvents({append=false, silent=false}={}) {
    if(state.loading)return; state.loading=true; if(!silent)$("#resultLabel").textContent="Загрузка…";
    try { const data=await api(`/api/global-log/events?${query(state.filters,append?state.cursor:null)}`);
      if(append) state.events.push(...data.events); else state.events=data.events; state.cursor=data.next_cursor; renderEvents(append); $("#loadMore").hidden=!data.has_more;
      $("#lastRefresh").textContent=new Date().toLocaleTimeString("ru-RU");
    } catch(error) { $("#resultLabel").textContent=`Ошибка: ${error.message}`; } finally {state.loading=false;}
  }
  function openDetail(event) {
    $("#detailMeta").textContent=`#${event.id} · ${formatTime(event.occurred_at)}`; $("#detailTitle").textContent=event.summary;
    const facts=$("#detailFacts"); facts.replaceChildren();
    for(const [label,value] of [["Сервис",event.source_service],["Тип",event.source_type],["Событие",event.event_type],["Важность",event.severity],["Пользователь",event.actor_display||event.actor_user_id],["Канал",event.channel_id],["Сообщение",event.message_id],["HTTP",event.status_code],["Время",event.duration_ms?`${event.duration_ms} ms`:null],["Request ID",event.request_id]]){
      if(value===null||value===undefined||value==="")continue;const node=document.createElement("div");node.className="fact";const key=document.createElement("span");key.textContent=label;const val=document.createElement("strong");val.textContent=value;node.append(key,val);facts.append(node);
    }
    $("#detailContent").textContent=event.content_text||"Содержимое не записывалось."; $("#detailJson").textContent=JSON.stringify(event.details||{},null,2);
    $("#previousHash").textContent=event.previous_hash; $("#eventHash").textContent=event.event_hash; $("#detailDialog").showModal();
  }
  function formFilters() { const data=new FormData($("#filterForm")); const output={}; for(const [key,value] of data) if(String(value).trim()) output[key]=String(value).trim(); return output; }
  function exportEvents(format) { const params=new URLSearchParams(state.filters);params.set("format",format);window.location.href=`/api/global-log/export?${params}`; }
  async function bootstrap() {
    try { const session=await api("/api/global-log/session"); if(!session.authenticated){showLogin();return;} showApp();
      const health=session.runtime||{};$("#runtimeHealth").className=`health-pill ${health.running&&!health.last_error?"ok":"fail"}`;$("#runtimeHealth").innerHTML=`<i></i>${health.running?"Журнал активен":"Нарушение потока"}`;$("#queueEvents").textContent=Number(health.queued||0).toLocaleString("ru-RU");
      await Promise.all([loadFacets(),loadEvents()]);state.timer=setInterval(()=>loadEvents({silent:true}),10000);
    } catch {showLogin();}
  }
  $("#loginForm").addEventListener("submit",async(event)=>{event.preventDefault();const alert=$("#loginAlert");alert.hidden=true;const button=event.submitter;button.disabled=true;
    try{await api("/api/global-log/login",{method:"POST",body:JSON.stringify({user_id:$("#userId").value,code:$("#accessCode").value})});$("#accessCode").value="";await bootstrap();}
    catch(error){alert.textContent=error.status===429?"Слишком много попыток. Подождите несколько минут.":"Код неверен, истёк или уже был использован.";alert.hidden=false;}finally{button.disabled=false;}
  });
  $("#filterForm").addEventListener("submit",event=>{event.preventDefault();state.filters=formFilters();state.cursor=null;loadEvents();});
  $("#resetFilters").addEventListener("click",()=>{$("#filterForm").reset();state.filters={};state.cursor=null;loadEvents();});
  $("#loadMore").addEventListener("click",()=>loadEvents({append:true})); $("#exportCsv").addEventListener("click",()=>exportEvents("csv")); $("#exportJson").addEventListener("click",()=>exportEvents("json"));
  $("#detailClose").addEventListener("click",()=>$("#detailDialog").close()); $("#logoutButton").addEventListener("click",async()=>{await api("/api/global-log/logout",{method:"POST",body:"{}"});showLogin();});
  $("#integrityButton").addEventListener("click",async(event)=>{event.currentTarget.disabled=true;$("#chainStatus").textContent="Проверка…";try{const result=await api("/api/global-log/integrity");$("#chainStatus").textContent=result.ok?"Цепочка цела":"Нарушена";$("#chainDetail").textContent=result.ok?`Проверено записей: ${result.checked}`:`Разрыв на записи #${result.broken_at}`;}catch(error){$("#chainStatus").textContent="Ошибка";$("#chainDetail").textContent=error.message;}finally{event.currentTarget.disabled=false;}});
  bootstrap();
})();
