/* SPA веб-интерфейса оркестратора. Vanilla JS, без зависимостей.
 *
 * Данные: REST /api/* (Bearer-токен или cookie) + WebSocket /api/ws
 * (живые события: ответы, статус-бабл, permission-запросы, bash-вывод).
 * Токен приходит в /?token=… (сервер ставит cookie), дублируется в
 * localStorage — оттуда он попадает в заголовки и в WS URL.
 */
"use strict";

/* ── токен ─────────────────────────────────────────────────── */

(function grabToken() {
  const q = new URLSearchParams(location.search);
  const tok = q.get("token");
  if (tok) {
    localStorage.setItem("orch_token", tok);
    history.replaceState(null, "", location.pathname); // токен из адресной строки убираем
  }
})();
const TOKEN = localStorage.getItem("orch_token") || "";

function api(path, opts = {}) {
  opts.headers = Object.assign({}, opts.headers);
  if (TOKEN) opts.headers["Authorization"] = "Bearer " + TOKEN;
  return fetch(path, opts);
}

async function apiJson(path, opts = {}) {
  const resp = await api(path, opts);
  let data = null;
  try { data = await resp.json(); } catch (e) { /* не-JSON тело */ }
  if (!resp.ok) {
    const msg = (data && data.error) ? data.error : ("HTTP " + resp.status);
    throw new Error(msg);
  }
  return data;
}

function postJson(path, body) {
  return apiJson(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

/* ── состояние и DOM ───────────────────────────────────────── */

const $ = (id) => document.getElementById(id);
const els = {
  list: $("session-list"), conn: $("conn-status"), chat: $("chat"),
  chatArea: $("chat-area"), emptyHint: $("empty-hint"), title: $("session-title"),
  bubble: $("bubble"), bubbleBody: $("bubble-body"), typing: $("typing"),
  input: $("input"), bashPanel: $("bash-panel"), bashOut: $("bash-out"),
  dropHint: $("drop-hint"),
};

let sessions = [];          // последний список с сервера
let current = null;         // имя выбранной сессии
let bubbleRef = null;       // ref активного статус-бабла текущей сессии
let bubbleHtml = "";        // его html — чтобы «заморозить» копию в чат
let typingTimer = null;
let loadSeq = 0;            // токен загрузки: гасит гонку историй при быстром переключении

const STATUS_ICON = { working: "🔄", waiting: "🟢", stopped: "⏸" };

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}

function nearBottom() {
  const c = els.chat;
  return c.scrollHeight - c.scrollTop - c.clientHeight < 80;
}

function scrollDown(force) {
  if (force || nearBottom()) els.chat.scrollTop = els.chat.scrollHeight;
}

/* ── список сессий ─────────────────────────────────────────── */

function showAuthError() {
  // Явная подсказка вместо тихой «нет связи» + пустого списка, когда токена
  // нет или он отвергнут (частый случай: открыли 127.0.0.1:8180 без ?token=).
  els.emptyHint.innerHTML =
    "🔑 Нужен токен доступа.<br><br>" +
    "Открой ссылку с токеном из журнала сервиса:<br>" +
    "<code>journalctl --user -u claude-orchestrator | grep Веб-интерфейс</code>";
  els.emptyHint.hidden = false;
  els.chatArea.hidden = true;
}

async function fetchSessions() {
  try {
    sessions = await apiJson("/api/sessions");
  } catch (e) {
    // 401 (нет/неверный токен) — показать понятную подсказку, а не молчать.
    if (/\b401\b|unauthorized/i.test(e.message) || !TOKEN) showAuthError();
    return;
  }
  renderSessions();
}

function renderSessions() {
  els.list.innerHTML = "";
  for (const s of sessions) {
    const li = document.createElement("li");
    if (s.name === current) li.classList.add("active");
    const icon = STATUS_ICON[s.status] || "⏸";
    let meta = s.model ? esc(s.model) : "";
    if (s.uptime) meta += (meta ? " · " : "") + esc(s.uptime);
    li.innerHTML = '<span class="s-icon">' + icon + '</span>' +
      '<span class="s-title" title="' + esc(s.linked_path || "") + '">' + esc(s.title) + "</span>" +
      (meta ? '<span class="s-model">' + meta + "</span>" : "");
    li.onclick = () => selectSession(s.name);
    els.list.appendChild(li);
  }
  const cur = sessions.find((s) => s.name === current);
  if (cur) els.title.textContent = cur.title + (cur.linked_path ? " — " + cur.linked_path : "");
}

async function selectSession(name) {
  current = name;
  bubbleRef = null;
  bubbleHtml = "";
  els.bubble.hidden = true;
  els.typing.hidden = true;
  els.emptyHint.hidden = true;
  els.chatArea.hidden = false;
  // Сброс bash-панели: она привязана к shell выбранной сессии — иначе
  // «Досыл» ушёл бы в терминал другой сессии, а на экране висел бы чужой вывод.
  els.bashPanel.hidden = true;
  els.bashOut.textContent = "";
  renderSessions();
  const seq = ++loadSeq;
  await loadHistory(name, seq);
  if (seq === loadSeq) await restoreBubble(name, seq);
  if (seq === loadSeq) els.input.focus();
}

async function restoreBubble(name, seq) {
  // Подтягиваем активный бабл работающей сессии (снапшот на сервере), чтобы
  // после переключения были видны индикатор и кнопки ⏹/⛔.
  try {
    const b = await apiJson("/api/sessions/" + encodeURIComponent(name) + "/bubble");
    if (seq === loadSeq && b && b.html) showBubble(b.ref, b.html, b.stop_button, b.unblock_active);
  } catch (e) { /* нет бабла — не критично */ }
}

/* ── чат: рендер сообщений ─────────────────────────────────── */

function addMsg(cls, html) {
  const div = document.createElement("div");
  div.className = "msg " + cls;
  div.innerHTML = html;
  els.chat.appendChild(div);
  scrollDown(false);
  return div;
}

function fileLink(session, path, name, caption) {
  // Токен в href НЕ вставляем: скачивание авторизуется HttpOnly-cookie,
  // а токен в ссылке утёк бы при «копировать адрес ссылки» и в логи прокси.
  const url = "/api/sessions/" + encodeURIComponent(session) +
    "/file?path=" + encodeURIComponent(path);
  return "📄 <a href=\"" + esc(url) + "\" download>" + esc(name || path) + "</a>" +
    (caption ? "<br>" + esc(caption) : "");
}

function permCard(session, ev) {
  const div = document.createElement("div");
  div.className = "msg perm";
  div.dataset.requestId = ev.request_id;
  div.innerHTML =
    '<div class="perm-tool">🔐 ' + esc(ev.tool) + "</div>" +
    (ev.description ? '<div class="perm-desc">' + esc(ev.description) + "</div>" : "") +
    (ev.preview ? "<pre>" + esc(ev.preview) + "</pre>" : "") +
    '<div class="perm-buttons">' +
    '<button class="btn perm-allow">✅ Разрешить</button>' +
    '<button class="btn perm-deny">❌ Отклонить</button>' +
    // Третья кнопка — только если ядро её предложило (ASK-грант кошелька):
    // подпись приходит с сервера и содержит, ЧТО именно записывается в policy.
    // Нет метки → кнопки ровно две, как и раньше.
    (ev.always_label
      ? '<button class="btn perm-always">' + esc(ev.always_label) + "</button>"
      : "") +
    "</div>";
  div.querySelector(".perm-allow").onclick = () => permVerdict(session, ev.request_id, "allow");
  div.querySelector(".perm-deny").onclick = () => permVerdict(session, ev.request_id, "deny");
  const always = div.querySelector(".perm-always");
  if (always) always.onclick = () => permVerdict(session, ev.request_id, "allow_always");
  els.chat.appendChild(div);
  scrollDown(false);
  return div;
}

async function permVerdict(session, requestId, behavior) {
  try {
    await postJson("/api/sessions/" + encodeURIComponent(session) + "/permission",
      { request_id: requestId, behavior: behavior });
  } catch (e) {
    addMsg("notice", "⚠ " + esc(e.message));
  }
}

function markPermResolved(requestId, behavior) {
  const card = els.chat.querySelector('.perm[data-request-id="' + CSS.escape(requestId) + '"]');
  if (!card || card.classList.contains("resolved")) return;
  card.classList.add("resolved");
  const btns = card.querySelector(".perm-buttons");
  if (btns) btns.remove();
  const v = document.createElement("div");
  v.className = "perm-verdict";
  v.textContent = behavior === "allow" ? "✅ разрешено"
    : behavior === "allow_always" ? "🔒 разрешено навсегда (записано в policy)"
    : "❌ отклонено";
  card.appendChild(v);
}

function renderHistoryEvent(session, ev) {
  switch (ev.kind) {
    case "user":
      addMsg("user", esc(ev.text));
      break;
    case "reply":
      addMsg("reply", ev.html || esc(ev.text));
      break;
    case "intermediate":
      addMsg("intermediate", "💬 " + (ev.html || esc(ev.text)));
      break;
    case "notice":
      addMsg("notice", ev.html || esc(ev.text));
      break;
    case "file":
      addMsg("file", fileLink(session, ev.path, ev.path.split("/").pop(), ev.caption));
      break;
    case "perm_request":
      permCard(session, ev);
      break;
    case "perm_resolved":
      markPermResolved(ev.request_id, ev.behavior);
      break;
    case "status":
      addMsg("notice", ev.status === "interrupted" ? "⛔ ход прерван" : "⏸ сессия остановлена");
      break;
    case "wallet": {
      // wallet-активность (аудит) — при переключении сессий тоже не теряем.
      const denied = ev.allowed === false ? " — отказано" : "";
      addMsg("notice", "🔐 wallet: <code>" +
        esc((ev.secret || "") + " → " + (ev.cmd || "")) + "</code>" + denied);
      break;
    }
  }
}

async function loadHistory(name, seq) {
  els.chat.innerHTML = "";
  let events = [];
  try {
    events = await apiJson("/api/sessions/" + encodeURIComponent(name) + "/history");
  } catch (e) {
    if (seq === undefined || seq === loadSeq) {
      addMsg("notice", "⚠ история недоступна: " + esc(e.message));
    }
    return;
  }
  // Пока грузилась история, пользователь мог переключиться на другую сессию —
  // не подмешиваем ответ старого запроса в чужой чат (гонка loadHistory).
  if (seq !== undefined && seq !== loadSeq) return;
  for (const ev of events) renderHistoryEvent(name, ev);
  scrollDown(true);
}

/* ── статус-бабл ───────────────────────────────────────────── */

function showBubble(ref, html, stopButton, unblockActive) {
  bubbleRef = ref;
  bubbleHtml = html;
  els.bubbleBody.innerHTML = html;
  $("bubble-buttons").style.display = stopButton ? "" : "none";
  // Кнопка ⏬ «В фон» — видимая всегда (вёрстка не прыгает), но неактивная,
  // когда сворачивать нечего (нет идущей задачи или модель уже ждёт фон).
  $("btn-bg").disabled = !unblockActive;
  els.bubble.hidden = false;
}

function closeBubble(ref, keepAsLog) {
  if (ref !== bubbleRef) return;
  if (keepAsLog && bubbleHtml) addMsg("frozen", bubbleHtml);
  bubbleRef = null;
  bubbleHtml = "";
  els.bubble.hidden = true;
}

/* ── WebSocket: живые события ──────────────────────────────── */

let ws = null;
let backoff = 1000;

function wsConnect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  let url = proto + "//" + location.host + "/api/ws";
  // WS-хендшейк несёт HttpOnly-cookie (первичная авторизация, как у REST через
  // Bearer-заголовок). ?token= — фолбэк, если cookie ещё не проставлен (зашли
  // без /?token=). Токен в URL виден локальному access-логу aiohttp — на
  // localhost под моделью угроз оператора это принято (не отправляем наружу).
  if (TOKEN) url += "?token=" + encodeURIComponent(TOKEN);
  ws = new WebSocket(url);

  ws.onopen = () => {
    backoff = 1000;
    els.conn.textContent = "онлайн";
    els.conn.className = "conn online";
  };
  ws.onclose = () => {
    els.conn.textContent = "нет связи — переподключение…";
    els.conn.className = "conn offline";
    setTimeout(wsConnect, backoff);
    backoff = Math.min(backoff * 2, 15000); // экспоненциальный бэкофф до 15 с
  };
  ws.onerror = () => { try { ws.close(); } catch (e) { /* уже закрыт */ } };
  ws.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch (e) { return; }
    handleEvent(ev);
  };
}

function handleEvent(ev) {
  switch (ev.type) {
    case "hello":
      sessions = ev.sessions || [];
      applyFeatures(ev.features);
      renderSessions();
      // После реконнекта часть событий потеряна — история и бабл переигрываются.
      if (current) {
        const seq = ++loadSeq;
        loadHistory(current, seq).then(() => {
          if (seq === loadSeq) restoreBubble(current, seq);
        });
      }
      return;
    case "sessions_changed":
      fetchSessions();
      return;
  }
  if (ev.type === "notice" && ev.session == null) {
    if (current) addMsg("notice", ev.html || esc(ev.text));
    return;
  }
  if (ev.session !== current) return; // события чужих сессий не показываем

  switch (ev.type) {
    case "reply":
      if (ev.intermediate) addMsg("intermediate", "💬 " + (ev.html || esc(ev.text)));
      else addMsg("reply", ev.html || esc(ev.text));
      break;
    case "notice":
      addMsg("notice", ev.html || esc(ev.text));
      break;
    case "file":
      addMsg("file", fileLink(ev.session, ev.path, ev.name, ev.caption));
      break;
    case "typing":
      els.typing.hidden = false;
      clearTimeout(typingTimer);
      typingTimer = setTimeout(() => { els.typing.hidden = true; }, 6000);
      break;
    case "bubble":
      showBubble(ev.ref, ev.html, ev.stop_button, ev.unblock_active);
      break;
    case "bubble_close":
      closeBubble(ev.ref, !ev.delete);
      break;
    case "bubble_freeze":
      closeBubble(ev.ref, true); // замороженный бабл остаётся журналом в чате
      break;
    case "perm_request":
      permCard(ev.session, ev);
      break;
    case "perm_resolved":
      markPermResolved(ev.request_id, ev.behavior);
      break;
    case "bash":
      els.bashOut.innerHTML = ev.html;
      els.bashOut.scrollTop = els.bashOut.scrollHeight;
      break;
  }
}

/* ── отправка сообщений ────────────────────────────────────── */

async function sendMessage() {
  const text = els.input.value.trim();
  if (!text || !current) return;
  els.input.value = "";
  els.input.style.height = "";
  addMsg("user", esc(text));
  try {
    const res = await postJson(
      "/api/sessions/" + encodeURIComponent(current) + "/message", { text: text });
    if (res && res.slash) addMsg("notice", "⌨ команда отправлена в терминал Claude");
  } catch (e) {
    addMsg("notice", "⚠ " + esc(e.message));
  }
}

$("btn-send").onclick = sendMessage;
els.input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});
els.input.addEventListener("input", () => {
  els.input.style.height = "";
  els.input.style.height = Math.min(els.input.scrollHeight, 180) + "px";
});

/* ── загрузка файлов (скрепка + drag&drop) ─────────────────── */

async function uploadFiles(files, caption) {
  if (!current) return;
  for (const file of files) {
    const fd = new FormData();
    fd.append("file", file, file.name);
    if (caption) fd.append("caption", caption);
    addMsg("notice", "📤 отправка файла " + esc(file.name) + "…");
    try {
      await apiJson("/api/sessions/" + encodeURIComponent(current) + "/upload",
        { method: "POST", body: fd });
    } catch (e) {
      addMsg("notice", "⚠ файл не отправлен: " + esc(e.message));
    }
  }
}

$("btn-attach").onclick = () => $("file-input").click();
$("file-input").addEventListener("change", (e) => {
  uploadFiles(Array.from(e.target.files));
  e.target.value = "";
});

let dragDepth = 0;
document.addEventListener("dragenter", (e) => {
  if (!current || !e.dataTransfer || !e.dataTransfer.types.includes("Files")) return;
  dragDepth++;
  els.dropHint.hidden = false;
});
document.addEventListener("dragleave", () => {
  if (--dragDepth <= 0) { dragDepth = 0; els.dropHint.hidden = true; }
});
document.addEventListener("dragover", (e) => e.preventDefault());
document.addEventListener("drop", (e) => {
  e.preventDefault();
  dragDepth = 0;
  els.dropHint.hidden = true;
  if (current && e.dataTransfer && e.dataTransfer.files.length) {
    uploadFiles(Array.from(e.dataTransfer.files));
  }
});

/* ── тулбар ────────────────────────────────────────────────── */

function sesUrl(suffix) {
  return "/api/sessions/" + encodeURIComponent(current) + suffix;
}

// Выключенная фича не должна оставлять артефактов в UI: кнопку, которая всегда
// упрётся в отказ, просто убираем (напр. Stats под SANDBOX=agent-vm — транскрипт
// лежит внутри microVM и на хосте не появится).
function applyFeatures(features) {
  if (!features) return;
  const btn = $("btn-stats");
  if (btn) btn.hidden = features.stats === false;
}

$("btn-stats").onclick = async () => {
  if (!current) return;
  try {
    const r = await apiJson(sesUrl("/stats"));
    addMsg("notice", esc(r.text));
  } catch (e) { addMsg("notice", "⚠ " + esc(e.message)); }
};

$("btn-log").onclick = () => {
  if (!current) return;
  // Скачивание авторизуется HttpOnly-cookie (токен в ссылку не кладём).
  window.location.href = sesUrl("/log");
};

$("btn-usage").onclick = async () => {
  if (!current) return;
  addMsg("notice", "⏳ собираю данные о расходах…");
  try {
    const r = await apiJson(sesUrl("/usage"));
    addMsg("notice", r.text ? esc(r.text) : "не удалось получить данные /cost");
  } catch (e) { addMsg("notice", "⚠ " + esc(e.message)); }
};

$("model-select").addEventListener("change", async (e) => {
  if (!current) return;
  let model = e.target.value;
  e.target.selectedIndex = 0; // возвращаем placeholder «Model»
  if (model === "__custom__") {
    model = (prompt("Имя модели (например claude-sonnet-4-5):") || "").trim();
  }
  if (!model) return;
  addMsg("notice", "🔁 переключаю модель на " + esc(model) + "…");
  try {
    const r = await postJson(sesUrl("/model"), { model: model });
    addMsg("notice", "✅ модель: " + esc(model) +
      (r.resumed ? "" : " (контекст не перенесён — начат заново)"));
  } catch (e2) { addMsg("notice", "⚠ " + esc(e2.message)); }
});

$("btn-compact").onclick = async () => {
  if (!current) return;
  try {
    await postJson(sesUrl("/compact"), {});
    addMsg("notice", "🧹 /compact отправлен в терминал Claude");
  } catch (e) { addMsg("notice", "⚠ " + esc(e.message)); }
};

$("btn-clear").onclick = async () => {
  if (!current) return;
  if (!confirm("Начать с чистым контекстом? История разговора будет забыта.")) return;
  try {
    await postJson(sesUrl("/clear"), {});
    addMsg("notice", "🧼 контекст очищен");
  } catch (e) { addMsg("notice", "⚠ " + esc(e.message)); }
};

$("btn-close").onclick = async () => {
  if (!current) return;
  try {
    await postJson(sesUrl("/close"), {});
    addMsg("notice", "⏸ сессия остановлена (сообщение возобновит её)");
  } catch (e) { addMsg("notice", "⚠ " + esc(e.message)); }
};

$("btn-delete").onclick = async () => {
  if (!current) return;
  const s = sessions.find((x) => x.name === current);
  if (!confirm("Удалить сессию «" + (s ? s.title : current) + "» безвозвратно?")) return;
  try {
    await postJson(sesUrl("/delete"), {});
    current = null;
    els.chatArea.hidden = true;
    els.emptyHint.hidden = false;
    fetchSessions();
  } catch (e) { addMsg("notice", "⚠ " + esc(e.message)); }
};

$("btn-stop").onclick = async () => {
  if (!current) return;
  try { await postJson(sesUrl("/stop"), {}); }
  catch (e) { addMsg("notice", "⚠ " + esc(e.message)); }
};

$("btn-interrupt").onclick = async () => {
  if (!current) return;
  try { await postJson(sesUrl("/interrupt"), {}); }
  catch (e) { addMsg("notice", "⚠ " + esc(e.message)); }
};

$("btn-bg").onclick = async () => {
  if (!current) return;
  try { await postJson(sesUrl("/unblock"), {}); }
  catch (e) { addMsg("notice", "⚠ " + esc(e.message)); }
};

/* ── bash-панель ───────────────────────────────────────────── */

$("btn-bash").onclick = () => { els.bashPanel.hidden = !els.bashPanel.hidden; };

async function bashRun() {
  const cmd = $("bash-cmd").value.trim();
  if (!cmd || !current) return;
  $("bash-cmd").value = "";
  els.bashOut.textContent = "⏳ выполняется: " + cmd;
  try {
    await postJson(sesUrl("/bash"), { cmd: cmd }); // вывод стримится по WS
  } catch (e) {
    els.bashOut.textContent = "⚠ " + e.message;
  }
}

async function bashSendInput() {
  const text = $("bash-stdin").value;
  if (!current) return;
  $("bash-stdin").value = "";
  try { await postJson(sesUrl("/bash_input"), { text: text }); }
  catch (e) { els.bashOut.textContent = "⚠ " + e.message; }
}

$("bash-run").onclick = bashRun;
$("bash-cmd").addEventListener("keydown", (e) => { if (e.key === "Enter") bashRun(); });
$("bash-send").onclick = bashSendInput;
$("bash-stdin").addEventListener("keydown", (e) => { if (e.key === "Enter") bashSendInput(); });

/* ── диалог новой сессии ───────────────────────────────────── */

$("btn-new").onclick = () => {
  $("new-title").value = "";
  $("new-path").value = "";
  $("new-dialog").showModal();
};

$("new-form").addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;
  const title = $("new-title").value.trim();
  const path = $("new-path").value.trim();
  if (!title) return;
  try {
    const s = await postJson("/api/sessions", { title: title, path: path || null });
    await fetchSessions();
    selectSession(s.name);
  } catch (err) {
    alert("Не удалось создать сессию: " + err.message);
  }
});

/* ── старт ─────────────────────────────────────────────────── */

if (!TOKEN) {
  showAuthError();  // открыли без ?token= и без сохранённого — сразу подсказка
} else {
  fetchSessions();
  wsConnect();
}
