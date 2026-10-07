/* Страница настройки «Штурмана». Обычный скрипт без сборки и без библиотек.
 *
 * Вход. Значение одноразовой ссылки берётся из части адреса после «#» (на сервер при открытии
 * она не уходит), отправляется один раз телом POST и сразу убирается из адресной строки. В ответ
 * сервер один раз отдаёт ключ сессии. Страница хранит его в localStorage своего адреса и шлёт
 * заголовком X-Shturman-Session с каждым запросом. Cookie нет вовсе: страница стоит на том же
 * имени узла, что и дашборд ассистента, но на другом порту, а cookie по портам не разделяются.
 * localStorage разделяется: соседний порт его не видит. Кроме ключа сессии, в хранилище ничего нет.
 *
 * Секреты (токен, ключ, пароль, код) уходят только в теле запроса и обратно не приходят: сервер
 * отвечает «задано / не задано». Поле с секретом очищается сразу после отправки.
 */
(function () {
  "use strict";

  var API = "api/";
  var KEY_NAME = "shturman-setup-session";
  var key = "";                 // ключ сессии; пусто — входа нет
  var S = null;                 // последнее состояние с сервера
  var FIRST_CHATS = 15, MORE_CHATS = 30;

  /* Хранилище может быть недоступно (закрытый режим браузера): тогда вход живёт до закрытия вкладки. */
  function loadKey() {
    try { return localStorage.getItem(KEY_NAME) || ""; } catch (e) { return ""; }
  }
  function saveKey(value) {
    key = value || "";
    try {
      if (key) localStorage.setItem(KEY_NAME, key); else localStorage.removeItem(KEY_NAME);
    } catch (e) { /* останется в памяти страницы */ }
  }
  var ui = {
    botEditing: false, botToken: "", bind: null, keysEditing: false,
    login: null, loginLink: "", chatAccount: null, chats: [], chatsTotal: 0, chatsEnabled: 0,
    chatsLoading: false, chatsKey: "", scans: {}, scanning: {}, exclude: {}, upload: null,
    signatures: {}, fastUntil: 0, lastState: 0, lastOverview: 0, optionsFor: null,
    messages: null, importOpened: false
  };

  /* ------------------------------------------------------------ мелочи */

  function $(id) { return document.getElementById(id); }
  function show(node, on) { if (typeof node === "string") node = $(node); if (node) node.hidden = !on; }
  function text(node, value) { if (typeof node === "string") node = $(node); if (node) node.textContent = value; }

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (key) {
      var value = attrs[key];
      if (value === null || value === undefined || value === false) return;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.indexOf("on") === 0) node.addEventListener(key.slice(2), value);
      else if (value === true) node.setAttribute(key, "");
      else node.setAttribute(key, value);
    });
    (children || []).forEach(function (child) {
      if (child === null || child === undefined || child === false) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  /* Перерисовывает блок, только если изменилось то, от чего он зависит: иначе при каждом опросе
   * сбрасывались бы отметки и фокус, пока человек нажимает. */
  function renderIfChanged(id, signature, build) {
    var key = JSON.stringify(signature);
    if (ui.signatures[id] === key) return;
    ui.signatures[id] = key;
    var box = $(id);
    box.textContent = "";
    var nodes = build();
    (Array.isArray(nodes) ? nodes : [nodes]).forEach(function (n) { if (n) box.appendChild(n); });
  }

  function note(id, kind, message) {
    var node = $(id);
    node.className = "note" + (kind ? " " + kind : "");
    node.textContent = message || "";
    node.hidden = !message;
  }

  function number(value) { return (Number(value) || 0).toLocaleString("ru-RU"); }

  function plural(n, one, few, many) {
    var a = Math.abs(n) % 100, b = a % 10;
    if (a > 10 && a < 20) return many;
    if (b === 1) return one;
    if (b >= 2 && b <= 4) return few;
    return many;
  }

  function megabytes(bytes) {
    var mb = bytes / 1048576;
    return mb < 1 ? "меньше 1 МБ" : mb < 1024 ? Math.round(mb) + " МБ" : (mb / 1024).toFixed(1).replace(".", ",") + " ГБ";
  }

  function when(iso) {
    var d = new Date(iso);
    if (isNaN(d)) return "";
    return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  }

  var toastTimer = null;
  function toast(message, bad) {
    var node = $("toast");
    node.textContent = message;
    node.className = "toast" + (bad ? " bad" : "");
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { node.hidden = true; }, bad ? 9000 : 5000);
  }

  function fast(seconds) { ui.fastUntil = Date.now() + (seconds || 30) * 1000; }

  /* ------------------------------------------------------------- запросы */

  var GENERIC = {
    0: "Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.",
    403: "Сервер не принял запрос. Обновите страницу и повторите.",
    404: "Этого уже нет. Обновите страницу.",
    413: "Слишком большой файл или запрос.",
    429: "Слишком часто. Подождите минуту и повторите.",
    500: "На сервере что-то сломалось. Попробуйте ещё раз; если повторится — посмотрите журнал сервиса."
  };

  function headers(json) {
    var h = { "X-Shturman-Setup": "1" };
    if (key) h["X-Shturman-Session"] = key;
    if (json) h["Content-Type"] = "application/json";
    return h;
  }

  /* Возвращает {ok, status, data, error}. Не бросает. При устаревшем входе показывает экран входа. */
  function call(method, path, body, quiet) {
    var options = { method: method, credentials: "omit", cache: "no-store", redirect: "error", referrerPolicy: "no-referrer", headers: headers(body !== undefined) };
    if (body !== undefined) options.body = JSON.stringify(body);
    return fetch(API + path, options).then(function (res) {
      return res.text().then(function (raw) {
        var data = {};
        try { data = raw ? JSON.parse(raw) : {}; } catch (e) { data = {}; }
        var out = { ok: res.ok, status: res.status, data: data, error: null };
        if (!res.ok) {
          out.error = (data && typeof data.error === "string" && data.error) || GENERIC[res.status] || GENERIC[res.status >= 500 ? 500 : 403];
          if (res.status === 401 && data.code === "unauthenticated" && !quiet) { saveKey(""); loginScreen("Вход устарел. Войдите заново."); }
        }
        return out;
      });
    }).catch(function () {
      return { ok: false, status: 0, data: {}, error: GENERIC[0] };
    });
  }

  function busy(button, on) { if (button) button.disabled = !!on; }

  /* Обычное действие по кнопке: запрос, сообщение об ошибке рядом, обновление состояния. */
  function act(button, errorId, method, path, body, done) {
    busy(button, true);
    if (errorId) note(errorId, "warn", "");
    return call(method, path, body).then(function (r) {
      busy(button, false);
      if (!r.ok) {
        if (errorId) note(errorId, "warn", r.error); else toast(r.error, true);
        return r;
      }
      if (done) done(r.data);
      refresh();
      refreshOverview();
      return r;
    });
  }

  /* ---------------------------------------------------------------- вход */

  var LOGIN_VIEWS = ["login-link", "login-start", "login-code", "login-nolink", "login-message"];
  var CODE_REASONS = {
    wrong: "Код не подошёл. Проверьте цифры и попробуйте ещё раз.",
    expired: "Срок действия кода вышел. Запросите новый.",
    none: "Этот код уже использован или не запрашивался. Запросите новый.",
    locked: "Слишком много неверных попыток. Вход по коду временно закрыт — попробуйте позже или войдите по новой ссылке.",
    no_owner: "Код прислать некому: бот согласований не настроен. Войдите по одноразовой ссылке — попросите того, кто ставил ассистента, или выполните на сервере ./ops/setup-link.sh",
    wait: "Недавняя отправка не удалась. Попробуйте ещё раз через минуту.",
    send_failed: "Не удалось отправить код: бот сейчас не может написать вам в Telegram. Попробуйте через минуту."
  };

  function loginView(name) {
    LOGIN_VIEWS.forEach(function (id) { show(id, id === name); });
  }

  function loginMessage(message, retry) {
    loginView("login-message");
    text("login-message-text", message);
    show("login-message-retry", !!retry);
  }

  function loginScreen(message) {
    stopLoop();
    S = null;
    show("screen-app", false);
    show("screen-login", true);
    call("GET", "session", undefined, true).then(function (r) {
      if (r.ok && r.data.authenticated && key) { startApp(); return; }
      if (r.ok && key) saveKey("");                  // сервер ключа не признал: стираем
      var canCode = r.ok && r.data.code_login;
      if (message) { loginMessage(message, true); return; }
      loginView(canCode ? "login-start" : "login-nolink");
    });
  }

  function takeLinkToken() {
    var value = (location.hash || "").replace(/^#/, "");
    return /^[A-Za-z0-9_-]{40,64}$/.test(value) ? value : "";
  }

  function wireLogin() {
    var input = $("login-code-input"), error = $("login-code-error");

    $("login-link-go").addEventListener("click", function () {
      var token = takeLinkToken(), button = this;
      // Убираем значение из адресной строки и истории до всякой отправки.
      try { history.replaceState(null, "", location.pathname + location.search); } catch (e) { /* не критично */ }
      if (!token) { loginMessage("Ссылка неполная. Откройте её целиком, вместе с частью после знака «#».", false); return; }
      busy(button, true);
      call("POST", "login/link", { token: token }, true).then(function (r) {
        busy(button, false);
        if (r.ok && r.data.key) { saveKey(r.data.key); startApp(); return; }
        loginMessage(r.error || GENERIC[500], false);
      });
    });

    function requestCode(button) {
      busy(button, true);
      call("POST", "login/code/request", {}, true).then(function (r) {
        busy(button, false);
        var result = r.ok ? r.data.result : "";
        if (result === "sent" || result === "reused") {
          loginView("login-code");
          text("login-code-lead", result === "sent"
            ? "Бот согласований отправил вам код в Telegram. Он действует 5 минут."
            : "Код уже был отправлен и ещё действует. Возьмите последний код из чата с ботом согласований.");
          error.hidden = true;
          input.value = "";
          input.focus();
          return;
        }
        loginMessage(CODE_REASONS[result] || r.error || GENERIC[0], result !== "no_owner" && result !== "locked");
      });
    }
    $("login-code-send").addEventListener("click", function () { requestCode(this); });
    $("login-code-resend").addEventListener("click", function () { requestCode(this); });
    $("login-message-retry").addEventListener("click", function () { loginScreen(""); });

    input.addEventListener("input", function () {
      var digits = input.value.replace(/\D/g, "").slice(0, 8);
      input.value = digits.length > 4 ? digits.slice(0, 4) + " " + digits.slice(4) : digits;
      error.hidden = true;
    });
    $("login-code-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var digits = input.value.replace(/\D/g, ""), button = $("login-code-submit");
      if (digits.length !== 8) { error.textContent = "В коде восемь цифр."; error.hidden = false; return; }
      busy(button, true);
      call("POST", "login/code", { code: digits }, true).then(function (r) {
        busy(button, false);
        input.value = "";
        if (r.ok && r.data.key) { saveKey(r.data.key); startApp(); return; }
        var reason = r.data.code;
        if (reason === "locked" || reason === "none" || reason === "expired") {
          loginMessage(CODE_REASONS[reason], reason !== "locked");
          return;
        }
        error.textContent = CODE_REASONS[reason] || r.error;
        error.hidden = false;
        input.focus();
      });
    });
  }

  /* ---------------------------------------------------- цикл обновления */

  var loop = null;
  function startLoop() {
    stopLoop();
    loop = setInterval(function () {
      if (document.hidden || !key) return;
      var now = Date.now(), every = now < ui.fastUntil || ui.login || ui.bind ? 2000 : 6000;
      if (now - ui.lastState >= every) refresh();
      if (now - ui.lastOverview >= 30000) refreshOverview();
      if (ui.login) pollLogin();
    }, 1000);
  }
  function stopLoop() { if (loop) clearInterval(loop); loop = null; }

  function startApp() {
    show("screen-login", false);
    show("screen-app", true);
    ui.signatures = {};
    refresh().then(function () { refreshOverview(); });
    startLoop();
  }

  function refresh() {
    ui.lastState = Date.now();
    return call("GET", "state").then(function (r) {
      show("net-error", !r.ok && r.status !== 401);
      if (!r.ok) return;
      S = r.data;
      render();
    });
  }

  function refreshOverview() {
    ui.lastOverview = Date.now();
    return call("GET", "overview").then(function (r) { if (r.ok) renderSummary(r.data); });
  }

  function pill(id, label, kind) {
    var node = $("st-" + id), dot = $("dot-" + id);
    if (node) { node.textContent = label; node.className = "pill" + (kind ? " " + kind : ""); }
    if (dot) dot.className = "dot" + (kind ? " " + kind : "");
  }

  function render() {
    if (!S) return;
    text("foot-version", "Штурман " + S.version);
    renderKeys(S.tg);
    renderAccounts(S.tg);
    renderChats(S.tg);
    renderImports(S.imports);
    renderBot(S.bot);
    renderBusiness(S.bot);
    renderLlm(S.llm);
    renderProgress();
  }

  /* Строка «что уже сделано» и замки на шагах: следующий шаг открыт, когда готов предыдущий. */
  function lockStep(id, locked, done) {
    var card = $(id);
    card.classList.toggle("locked", !!locked);
    card.classList.toggle("is-done", !!done);
  }

  function renderProgress() {
    var tg = S.tg, keys = tg.keys.configured;
    var accounts = (tg.accounts || []).filter(function (a) { return a.account_id !== null; });
    var chats = accounts.reduce(function (sum, a) { return sum + (a.chats_enabled || 0); }, 0);
    var imported = (S.imports.items || []).some(function (i) { return i.state === "done"; });
    var steps = [
      ["Ключи приложения", keys, keys ? "готово" : "сделайте сейчас"],
      ["Вход в аккаунт", accounts.length > 0, accounts.length ? "готово" : keys ? "сделайте сейчас" : "после шага 1"],
      ["Выбор чатов", chats > 0, chats ? "выбрано: " + chats : accounts.length ? "сделайте сейчас" : "после шага 2"]
    ];
    lockStep("s-keys", false, keys);
    lockStep("s-accounts", !keys, accounts.length > 0);
    lockStep("s-chats", !accounts.length, chats > 0);
    renderIfChanged("progress", [steps, imported], function () {
      var nodes = steps.map(function (s, i) {
        var now = !s[1] && s[2] === "сделайте сейчас";
        return el("li", { class: s[1] ? "done" : now ? "now" : "wait" }, [
          el("span", { class: "p-mark", "aria-hidden": "true", text: s[1] ? "✓" : String(i + 1) }),
          el("span", { class: "p-name", text: s[0] }),
          el("span", { class: "p-state", text: s[2] })
        ]);
      });
      if (imported) nodes.push(el("li", { class: "done" }, [
        el("span", { class: "p-mark", "aria-hidden": "true", text: "✓" }),
        el("span", { class: "p-name", text: "Выгрузка" }), el("span", { class: "p-state", text: "импортирована" })
      ]));
      return nodes;
    });
  }

  /* ----------------------------------------------- 1. бот согласований */

  function renderBot(b) {
    var editing = ui.botEditing || (!b.configured && b.editable);
    pill("bot", !b.configured ? "Не настроен" : b.problem_text ? "Нет связи" : b.owner_bound ? "Готово" : "Привяжите владельца",
         !b.configured ? "" : b.problem_text || !b.owner_bound ? "todo" : "done");
    show("bot-server", b.configured && b.source === "server");
    if (!b.configured) note("bot-status", "", "");
    else if (b.problem_text) note("bot-status", "warn", b.problem_text);
    else if (b.username && b.polling) note("bot-status", "ok", "Бот @" + b.username + " на связи с Telegram.");
    else note("bot-status", "info", b.username ? "Бот @" + b.username + " сохранён, выхожу на связь с Telegram…" : "Токен сохранён, выхожу на связь с Telegram…");
    show("bot-form", editing && !ui.botToken);
    show("bot-form-cancel", ui.botEditing);
    show("bot-confirm", !!ui.botToken);
    show("bot-actions", b.configured && b.source === "page" && !ui.botEditing);
    show("shot-newbot", !b.configured || ui.botEditing);

    show("s-business", b.configured);
    var canBind = b.configured && !!b.username && !ui.botEditing;
    show("bind", canBind);
    if (!canBind) { ui.bind = null; return; }
    if (ui.bind && b.owner_bound && b.owner_bound_at !== ui.bind.before) {
      ui.bind = null;                                  // ссылку открыли: владелец привязан
      toast("Владелец привязан" + (b.owner_name ? ": " + b.owner_name : "") + ".");
    }
    show("bind-none", !b.owner_bound);
    note("bind-done", "ok", b.owner_bound ? "Владелец привязан" + (b.owner_name ? ": " + b.owner_name : "") + "." : "");
    show("bind-link-box", !!ui.bind);
    show("shot-start", !!ui.bind);
    show("bind-go", !ui.bind);
    text("bind-go", b.owner_bound ? "Привязать другой аккаунт" : "Показать ссылку привязки");
    if (b.bind_paused) note("bind-error", "warn", "Бот получил много неверных кодов привязки и на 10 минут перестал их принимать. Подождите и выдайте ссылку заново.");
  }

  function wireBot() {
    var input = $("bot-token");
    $("bot-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var token = input.value.trim(), button = $("bot-check");
      input.value = "";
      if (!token) { note("bot-error", "warn", "Вставьте токен бота из сообщения @BotFather."); return; }
      busy(button, true);
      note("bot-error", "warn", "");
      call("POST", "bot/token", { token: token }).then(function (r) {
        busy(button, false);
        if (!r.ok) { note("bot-error", "warn", r.error); return; }
        ui.botToken = token;         // до подтверждения живёт только в памяти страницы
        text("bot-confirm-name", "@" + r.data.bot.username + (r.data.bot.name ? " («" + r.data.bot.name + "»)" : ""));
        render();
        $("bot-save").focus();
      });
    });
    $("bot-save").addEventListener("click", function () {
      var button = this, token = ui.botToken;
      busy(button, true);
      show("bot-save-wait", true);
      call("POST", "bot/token", { token: token, separate: true }).then(function (r) {
        busy(button, false);
        show("bot-save-wait", false);
        ui.botToken = "";
        if (!r.ok) { note("bot-error", "warn", r.error); render(); return; }
        ui.botEditing = false;
        toast("Токен сохранён. Бот @" + r.data.bot.username + " выходит на связь.");
        fast(20);
        refresh(); refreshOverview();
      });
    });
    $("bot-confirm-cancel").addEventListener("click", function () { ui.botToken = ""; render(); input.focus(); });
    $("bot-change").addEventListener("click", function () { ui.botEditing = true; render(); input.focus(); });
    $("bot-form-cancel").addEventListener("click", function () { ui.botEditing = false; note("bot-error", "warn", ""); render(); });
    $("bot-remove").addEventListener("click", function () {
      if (!confirm("Убрать токен бота согласований?\n\nБот перестанет присылать карточки, а привязка владельца к нему потеряет силу. Токен можно будет ввести снова.")) return;
      act(this, "bot-error", "DELETE", "bot/token", {}, function () { toast("Токен бота убран."); });
    });
    $("bind-go").addEventListener("click", function () {
      var button = this;
      if (S && S.bot.owner_bound && !confirm("Владелец уже привязан.\n\nЕсли новую ссылку откроет другой аккаунт, владелец сменится: ждущие черновики отклонятся, список доверенных очистится. Продолжить?")) return;
      busy(button, true);
      note("bind-error", "warn", "");
      call("POST", "bot/bind", {}).then(function (r) {
        busy(button, false);
        if (!r.ok) { note("bind-error", "warn", r.error); return; }
        ui.bind = { before: S ? S.bot.owner_bound_at : null };
        var qr = $("bind-qr");
        qr.textContent = "";
        try { qr.appendChild(window.ShturmanQR.svg(r.data.link, { label: "QR-код со ссылкой привязки" })); } catch (e) { /* без кода останется кнопка */ }
        $("bind-open").setAttribute("href", r.data.link);
        text("bind-minutes", String(r.data.minutes));
        setTimeout(function () {                       // ссылка одноразовая и недолгая — не держим её на экране дольше срока
          if (ui.bind) { ui.bind = null; $("bind-open").setAttribute("href", "#"); qr.textContent = ""; render(); }
        }, r.data.minutes * 60000);
        render();
        refreshOverview();
      });
    });
  }

  /* ------------------------------------------ 2. ключи приложения Telegram */

  function renderKeys(tg) {
    var k = tg.keys, editing = ui.keysEditing || (!k.configured && k.editable);
    pill("keys", k.configured ? "Готово" : "Не заданы", k.configured ? "done" : "");
    show("keys-server", k.configured && k.source === "server");
    show("keys-ok", k.configured && k.source !== "server" && !ui.keysEditing);
    show("keys-form", editing);
    show("keys-form-cancel", ui.keysEditing);
    show("keys-actions", k.configured && k.source === "page" && !ui.keysEditing);
    show("keys-shot", editing);                       // схема нужна, только пока ключи вводят
  }

  function wireKeys() {
    $("keys-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var id = $("keys-id"), hash = $("keys-hash");
      var body = { api_id: id.value.trim(), api_hash: hash.value.trim() };
      hash.value = "";
      act($("keys-save"), "keys-error", "PUT", "tg/keys", body, function () {
        id.value = ""; ui.keysEditing = false; toast("Ключи сохранены. Теперь шаг 2 — вход в аккаунт.");
      });
    });
    $("keys-change").addEventListener("click", function () { ui.keysEditing = true; render(); $("keys-id").focus(); });
    $("keys-form-cancel").addEventListener("click", function () { ui.keysEditing = false; note("keys-error", "warn", ""); render(); });
    $("keys-remove").addEventListener("click", function () {
      if (!confirm("Убрать ключи приложения Telegram?\n\nБез них сервис не сможет подключать аккаунты.")) return;
      act(this, "keys-error", "DELETE", "tg/keys", {}, function () { toast("Ключи приложения убраны."); });
    });
  }

  /* ------------------------------------------------- 3. аккаунты Telegram */

  var STATUS = {
    running: ["Подключён", "ok"], starting: ["Подключается…", ""], disconnected: ["Нет связи с Telegram", "warn"],
    error: ["Нет связи с Telegram", "warn"], paused: ["На паузе", ""], unauthorized: ["Нужно войти заново", "warn"],
    failed: ["Нужно войти заново", "warn"], no_session: ["Нужно войти заново", "warn"], locked: ["Сессия занята", "warn"]
  };
  var RELOGIN = { unauthorized: 1, failed: 1, no_session: 1 };

  function accountOf(tg, role) {
    return (tg.accounts || []).filter(function (a) { return a.role === role; })[0] || null;
  }

  function connectControls(role, tg, again) {
    var nodes = [], consent = null;
    if (role === "owner") {
      consent = el("input", { type: "checkbox", id: "consent-owner" });
      nodes.push(el("label", { class: "check" }, [consent, el("span", { text: "Понимаю: на сервере появится сессия моего основного аккаунта. Если Telegram сочтёт её подозрительной, ограничения коснутся основного номера. Завершить сессию можно в Telegram: «Настройки» → «Устройства»." })]));
    }
    if (role === "assistant" && !tg.owner_known) {
      nodes.push(el("p", { class: "note info", text: "Сначала подключите свой основной аккаунт — шаг 2. Пока сервис не знает, какой аккаунт ваш, он не сможет отличить его от помощника." }));
      return nodes;
    }
    nodes.push(el("div", { class: "row" }, [el("button", {
      class: "btn", type: "button", text: again ? "Войти заново" : role === "owner" ? "Показать QR-код для входа" : "Подключить помощника",
      onclick: function () {
        if (consent && !consent.checked) { toast("Отметьте, что понимаете: на сервере появится сессия основного аккаунта.", true); consent.focus(); return; }
        startLogin(role, this);
      }
    })]));
    return nodes;
  }

  function accountNodes(account, role, tg) {
    if (!account) return connectControls(role, tg, false);
    var st = STATUS[account.status] || [account.status, ""], id = account.account_id, nodes = [];
    nodes.push(el("div", { class: "row" }, [
      el("span", { class: "account-name", text: account.label || "Аккаунт" }),
      el("span", { class: "badge " + st[1], text: st[0] })
    ]));
    if (account.error) nodes.push(el("p", { class: "small", text: account.error }));
    if (id !== null && account.status === "running") {
      nodes.push(el("p", { class: "small", text: "Выбрано чатов: " + account.chats_enabled +
        (account.chats_enabled ? " · история загружена: " + account.chats_loaded : "") +
        (account.chats_lost ? " · нет доступа: " + account.chats_lost : "") }));
    }
    var buttons = [];
    if (id !== null && account.paused) {
      buttons.push(el("button", { class: "btn small-btn", type: "button", text: "Снять с паузы", onclick: function () { act(this, null, "POST", "tg/accounts/" + id + "/resume", {}); } }));
    } else if (id !== null && !RELOGIN[account.status]) {
      buttons.push(el("button", { class: "btn ghost small-btn", type: "button", text: "Пауза", onclick: function () { act(this, null, "POST", "tg/accounts/" + id + "/pause", {}); } }));
    }
    if (id !== null) {
      buttons.push(el("button", { class: "btn ghost danger small-btn", type: "button", text: "Выйти из аккаунта", onclick: function () {
        if (!confirm("Выйти из аккаунта?\n\nСессия на сервере завершится. Уже сохранённые сообщения останутся в архиве.")) return;
        act(this, null, "POST", "tg/accounts/" + id + "/logout", {}, function () { toast("Сессия аккаунта завершена."); });
      } }));
    }
    if (buttons.length) nodes.push(el("div", { class: "row" }, buttons));
    if (id === null || RELOGIN[account.status]) nodes = nodes.concat(connectControls(role, tg, true));
    return nodes;
  }

  function renderAccounts(tg) {
    var ready = tg.keys.configured;
    var owner = accountOf(tg, "owner"), helper = accountOf(tg, "assistant");
    pill("accounts", !ready ? "" : owner && owner.status === "running" ? "Готово" : owner ? "Нужно внимание" : "Не выполнен",
         owner && owner.status === "running" ? "done" : owner ? "todo" : "");
    pill("assistant", helper && helper.status === "running" ? "Подключён" : "", helper ? "done" : "");
    show("accounts-nokeys", !ready);
    show("accounts-body", ready);
    show("assistant-nokeys", !ready);
    show("role-assistant-state", ready);
    if (!ready) return;
    if (!ui.login && tg.logins && tg.logins.length) {        // страницу обновили посреди входа
      ui.login = { id: tg.logins[0].login_id, role: tg.logins[0].role };
      placeLogin(ui.login.role);
      pollLogin();
    }
    ["owner", "assistant"].forEach(function (role) {
      var account = accountOf(tg, role);
      renderIfChanged("role-" + role + "-state", [account, tg.owner_known, !!ui.login], function () {
        return ui.login && ui.login.role === role && !account ? [el("p", { class: "small waiting", text: "Идёт вход — код ниже." })] : accountNodes(account, role, tg);
      });
    });
    show("tg-login", !!ui.login);
  }

  /* Блок с QR один на оба аккаунта: он переезжает туда, где начали вход. */
  function placeLogin(role) {
    var slot = $("login-slot-" + role), box = $("tg-login");
    if (slot && box.parentNode !== slot) slot.appendChild(box);
    if (role === "assistant") $("extras").open = true;
  }

  function startLogin(role, button) {
    busy(button, true);
    call("POST", "tg/login", { role: role, confirm_owner: role === "owner" }).then(function (r) {
      busy(button, false);
      if (!r.ok) { toast(r.error, true); return; }
      ui.login = { id: r.data.login_id, role: role };
      ui.loginLink = "";
      placeLogin(role);
      showLogin(r.data);
      render();
      $("tg-login").scrollIntoView({ block: "nearest" });
    });
  }

  function showLogin(flow) {
    text("tg-login-title", flow.role === "owner" ? "Вход в основной аккаунт" : "Вход в аккаунт-помощник");
    var pending = flow.status === "pending", password = flow.status === "password_required";
    show("tg-login-qr-box", pending);
    show("tg-password-form", password);
    show("tg-login-cancel", pending || password);
    if (pending && flow.link && flow.link !== ui.loginLink) {
      ui.loginLink = flow.link;
      var box = $("tg-login-qr");
      box.textContent = "";
      try { box.appendChild(window.ShturmanQR.svg(flow.link, { label: "QR-код для входа в аккаунт Telegram" })); }
      catch (e) { note("tg-login-error", "warn", "Не удалось нарисовать код. Обновите страницу и начните вход заново."); }
    }
    if (password) {
      text("tg-password-hint", (flow.hint ? "Подсказка, которую вы задали в Telegram: " + flow.hint + ". " : "") +
        (flow.attempts_left ? "Осталось попыток: " + flow.attempts_left + "." : ""));
      note("tg-login-error", "warn", flow.error || "");
    }
    if (flow.status === "completed") {
      closeLogin();
      toast("Аккаунт подключён. Теперь шаг 3 — выберите чаты.");
      fast(20);
      refresh(); refreshOverview();
    } else if (!pending && !password) {
      var why = flow.error || { cancelled: "Вход отменён.", expired: "Время на вход истекло. Начните заново." }[flow.status] || "Войти не получилось. Начните заново.";
      closeLogin();
      if (flow.status !== "cancelled") toast(why, true);
      refresh();
    }
  }

  function closeLogin() {
    ui.login = null; ui.loginLink = "";
    $("tg-login-qr").textContent = "";
    $("tg-password").value = "";
    note("tg-login-error", "warn", "");
    show("tg-login", false);
  }

  var pollingLogin = false;
  function pollLogin() {
    if (!ui.login || pollingLogin) return;
    pollingLogin = true;
    var id = ui.login.id;
    call("GET", "tg/login/" + encodeURIComponent(id)).then(function (r) {
      pollingLogin = false;
      if (!ui.login || ui.login.id !== id) return;
      if (r.status === 404) { closeLogin(); refresh(); return; }
      if (r.ok) showLogin(r.data);
    });
  }

  function wireAccounts() {
    $("tg-password-form").addEventListener("submit", function (event) {
      event.preventDefault();
      if (!ui.login) return;
      var input = $("tg-password"), value = input.value, button = $("tg-password-send");
      input.value = "";                                   // пароль не остаётся ни в поле, ни в памяти страницы
      if (!value) { note("tg-login-error", "warn", "Введите облачный пароль Telegram."); return; }
      busy(button, true);
      call("POST", "tg/login/" + encodeURIComponent(ui.login.id) + "/password", { password: value }).then(function (r) {
        value = "";
        busy(button, false);
        if (!r.ok) { note("tg-login-error", "warn", r.error); return; }
        showLogin(r.data);
        if (r.data.status === "password_required") input.focus();
      });
    });
    $("tg-login-cancel").addEventListener("click", function () {
      if (!ui.login) return;
      var id = ui.login.id;
      closeLogin();
      call("POST", "tg/login/" + encodeURIComponent(id) + "/cancel", {}).then(function () { refresh(); });
    });
  }

  /* ------------------------------------------------------------- 4. чаты */

  var KIND_NAMES = { personal: "личный", group: "группа", channel: "канал" };
  var KIND_BULK = { personal: "личные чаты", group: "группы", channel: "каналы" };

  function runningAccounts(tg) {
    return (tg.accounts || []).filter(function (a) { return a.account_id !== null && a.status === "running"; });
  }

  function renderChats(tg) {
    var accounts = runningAccounts(tg), all = (tg.accounts || []).filter(function (a) { return a.account_id !== null; });
    var total = all.reduce(function (sum, a) { return sum + (a.chats_enabled || 0); }, 0);
    pill("chats", all.length ? (total ? "Выбрано: " + total : "Выберите чаты") : "", total ? "done" : all.length ? "todo" : "");
    show("chats-none", !all.length);
    show("chats-body", !!all.length);
    if (!all.length) { ui.chatAccount = null; return; }
    if (!all.some(function (a) { return a.account_id === ui.chatAccount; })) {
      ui.chatAccount = (accounts[0] || all[0]).account_id;
      ui.chatsKey = "";
    }
    var current = all.filter(function (a) { return a.account_id === ui.chatAccount; })[0];
    show("chats-accounts", all.length > 1);
    renderIfChanged("chats-accounts", [all.map(function (a) { return [a.account_id, a.label, a.role]; }), ui.chatAccount], function () {
      return all.map(function (a) {
        return el("button", {
          class: "tab", type: "button", role: "tab", "aria-selected": a.account_id === ui.chatAccount ? "true" : "false",
          text: (a.role === "owner" ? "Основной: " : "Помощник: ") + (a.label || ""),
          onclick: function () { ui.chatAccount = a.account_id; ui.chatsKey = ""; render(); }
        });
      });
    });
    var online = current.status === "running";
    note("chats-offline", "warn", online ? "" : "Этот аккаунт сейчас не подключён, поэтому список чатов недоступен. Его состояние — на шаге 2.");
    ["chats-options", "chats-counter", "chats-list"].forEach(function (id) { show(id, online); });
    document.querySelector("#chats-body .toolbar").hidden = !online;
    show("chats-refresh", online);
    if (!online) { show("chats-more", false); show("chats-bulk", false); return; }
    if (ui.optionsFor !== current.account_id + ":" + current.backfill_months + ":" + current.auto_personal + ":" + current.auto_groups) {
      ui.optionsFor = current.account_id + ":" + current.backfill_months + ":" + current.auto_personal + ":" + current.auto_groups;
      var depth = $("opt-depth"), value = current.backfill_months === null ? "all" : String(current.backfill_months);
      if (!Array.prototype.some.call(depth.options, function (o) { return o.value === value; })) {
        depth.appendChild(el("option", { value: value, text: value + " мес." }));
      }
      depth.value = value;
      $("opt-auto-personal").checked = !!current.auto_personal;
      $("opt-auto-groups").checked = !!current.auto_groups;
    }
    var key = chatsQueryKey();
    if (ui.chatsKey !== key) loadChats(true);
  }

  function chatsQueryKey() {
    return [ui.chatAccount, $("chats-q").value.trim(), $("chats-kind").value, $("chats-only").checked].join("|");
  }

  function loadChats(reset, refreshList) {
    if (ui.chatAccount === null || ui.chatsLoading) return;
    var key = chatsQueryKey(), offset = reset ? 0 : ui.chats.length;
    ui.chatsLoading = true;
    ui.chatsKey = key;
    var query = "?offset=" + offset + "&limit=" + (reset ? FIRST_CHATS : MORE_CHATS) + "&q=" + encodeURIComponent($("chats-q").value.trim()) +
      "&kind=" + encodeURIComponent($("chats-kind").value) + ($("chats-only").checked ? "&only=enabled" : "") +
      (refreshList ? "&refresh=1" : "");
    call("GET", "tg/accounts/" + ui.chatAccount + "/dialogs" + query).then(function (r) {
      ui.chatsLoading = false;
      if (key !== chatsQueryKey()) { loadChats(true); return; }       // пока шёл запрос, условие поменяли
      if (!r.ok) { note("chats-error", "warn", r.error); return; }
      note("chats-error", "warn", "");
      ui.chats = reset ? r.data.items : ui.chats.concat(r.data.items);
      ui.chatsTotal = r.data.total;
      ui.chatsEnabled = r.data.enabled_total;
      drawChats();
    });
  }

  function chatRow(item) {
    var kind = KIND_NAMES[item.kind] || "чат";
    var meta = [el("span", { text: kind })];
    if (item.username) meta.push(el("span", { text: "@" + item.username }));
    if (item.excluded) meta.push(el("span", { class: "badge warn", text: "не сохраняется" }));
    var toggle = el("input", { type: "checkbox", checked: item.enabled, disabled: item.excluded });
    toggle.checked = !!item.enabled;
    toggle.addEventListener("change", function () {
      var want = toggle.checked;
      toggle.disabled = true;
      call("POST", "tg/accounts/" + ui.chatAccount + "/sync", { enabled: want, chats: [{ peer_class: item.peer_class, tg_id: item.tg_id }] }).then(function (r) {
        toggle.disabled = false;
        var result = r.ok && r.data.chats[0];
        if (!r.ok || !result || result.enabled !== want) {
          toggle.checked = !want;
          toast((result && result.error) || r.error || "Не получилось изменить чат.", true);
          return;
        }
        item.enabled = want;
        ui.chatsEnabled += want ? 1 : -1;
        drawCounter();
        fast(10);
      });
    });
    var exclude = item.locked ? el("span", { class: "small", text: "служебный чат Telegram" }) : el("button", {
      class: "btn ghost small-btn" + (item.excluded ? "" : " quiet"), type: "button",
      text: item.excluded ? "Вернуть в архив" : "Не сохранять",
      "aria-label": (item.excluded ? "Вернуть в архив чат " : "Не сохранять чат ") + (item.title || ""),
      onclick: function () {
        var body = { peer_class: item.peer_class, tg_id: item.tg_id, excluded: !item.excluded };
        if (!item.excluded) {
          if (!confirm("Не сохранять этот чат?\n\nСервис перестанет принимать его сообщения из любого источника: из аккаунта, бизнес-режима и выгрузки.")) return;
          body.purge = confirm("Стереть и то, что из этого чата уже сохранено?\n\nОК — стереть (вернуть будет нельзя). Отмена — оставить сохранённое.");
        }
        act(this, "chats-error", "POST", "tg/accounts/" + ui.chatAccount + "/exclude", body, function (data) {
          toast(data.excluded ? "Чат исключён из архива" + (data.purged ? ": стёрто сообщений — " + data.purged + "." : ".") : "Чат возвращён. Отметьте «читать», чтобы сервис снова его сохранял.");
          loadChats(true);
        });
      }
    });
    return el("li", { class: "chat" + (item.excluded ? " is-excluded" : "") }, [
      el("div", { class: "chat-main" }, [
        el("span", { class: "chat-title", text: item.title || "Без названия" }),
        el("span", { class: "chat-meta" }, meta)
      ]),
      el("label", { class: "chat-read" }, [toggle, el("span", { text: "читать" })]),
      exclude
    ]);
  }

  function drawCounter() {
    text("chats-counter", "Выбрано для чтения: " + ui.chatsEnabled + " " + plural(ui.chatsEnabled, "чат", "чата", "чатов") +
      ". Показано " + ui.chats.length + " из " + ui.chatsTotal + ".");
  }

  function drawChats() {
    var list = $("chats-list");
    list.textContent = "";
    ui.chats.forEach(function (item) { list.appendChild(chatRow(item)); });
    if (!ui.chats.length) note("chats-error", "info", "Ничего не найдено. Измените поиск или вид чатов.");
    drawCounter();
    var left = ui.chatsTotal - ui.chats.length;
    show("chats-more", left > 0);
    if (left > 0) text("chats-more", "Показать ещё " + Math.min(left, MORE_CHATS) + " (осталось " + left + ")");
    var kind = $("chats-kind").value, bulk = $("chats-bulk");
    show(bulk, !!kind && !$("chats-q").value.trim() && !$("chats-only").checked && ui.chatsTotal > 0);
    if (kind) text(bulk, "Читать все " + KIND_BULK[kind] + " (" + ui.chatsTotal + ")");
  }

  function wireChats() {
    var timer = null;
    $("chats-q").addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(function () { loadChats(true); }, 300);
    });
    $("chats-kind").addEventListener("change", function () { loadChats(true); });
    $("chats-only").addEventListener("change", function () { loadChats(true); });
    $("chats-more").addEventListener("click", function () { loadChats(false); });
    $("chats-refresh").addEventListener("click", function () { loadChats(true, true); });
    $("chats-bulk").addEventListener("click", function () {
      var kind = $("chats-kind").value;
      if (!kind) return;
      if (!confirm("Читать все " + KIND_BULK[kind] + " этого аккаунта (" + ui.chatsTotal + ")?\n\nСервис загрузит их историю на выбранную глубину и будет сохранять новые сообщения. Исключённые чаты останутся исключёнными.")) return;
      act(this, "chats-error", "POST", "tg/accounts/" + ui.chatAccount + "/sync", { enabled: true, kind: kind }, function (data) {
        toast("Включено чатов: " + data.enabled + ".");
        loadChats(true);
      });
    });

    function saveOptions(body, control) {
      control.disabled = true;
      call("PUT", "tg/accounts/" + ui.chatAccount + "/options", body).then(function (r) {
        control.disabled = false;
        if (!r.ok) { toast(r.error, true); ui.optionsFor = null; }
        else toast("Настройка сохранена.");
        refresh(); refreshOverview();
      });
    }
    $("opt-depth").addEventListener("change", function () {
      saveOptions({ backfill_months: this.value === "all" ? null : Number(this.value) }, this);
    });
    $("opt-auto-personal").addEventListener("change", function () {
      if (this.checked && !confirm("Брать новые личные чаты в архив без вашего выбора?\n\nЛюбой, кто впервые напишет на этот аккаунт, попадёт в архив сам.")) { this.checked = false; return; }
      saveOptions({ auto_personal: this.checked }, this);
    });
    $("opt-auto-groups").addEventListener("change", function () {
      if (this.checked && !confirm("Брать новые группы и каналы в архив без вашего выбора?\n\nГруппа, в которую добавят этот аккаунт, попадёт в архив сама.")) { this.checked = false; return; }
      saveOptions({ auto_groups: this.checked }, this);
    });
  }

  /* ------------------------------------- 5. выгрузка из Telegram Desktop */

  function fetchScan(id) {
    if (ui.scans[id] || ui.scanning[id]) return;
    ui.scanning[id] = true;
    call("GET", "imports/" + id + "/scan?wait=20").then(function (r) {
      delete ui.scanning[id];
      if (r.status === 200) { ui.scans[id] = r.data; ui.exclude[id] = ui.exclude[id] || {}; }
      else if (r.status === 202) setTimeout(function () { fetchScan(id); }, 500);
      else ui.scans[id] = { failed: r.error };
      fast(10);
      refresh();
    });
  }

  function importItem(item) {
    var id = item.import_id, scan = ui.scans[id], nodes = [];
    var head = "Файл " + megabytes(item.size_bytes) + ", загружен " + when(item.uploaded_at);
    nodes.push(el("b", { text: head }));
    if (item.state === "failed") {
      nodes.push(el("p", { class: "note warn", text: "Не получилось: " + (item.error || "ошибка") + (item.file_kept ? " Файл остался на сервере — импорт можно запустить ещё раз." : "") }));
    } else if (item.state === "done") {
      var st = item.stats || {};
      nodes.push(el("p", { class: "note ok", text: "Импорт завершён. Новых сообщений: " + number(st.messages_new) + ", уже были в архиве: " + number(st.messages_known) + ", чатов: " + number(st.chats) + (st.chats_excluded ? ", пропущено исключённых чатов: " + number(st.chats_excluded) : "") + "." }));
      nodes.push(el("p", { class: "small", text: "Файл с сервера удалён. Удалите выгрузку и со своего компьютера, если она больше не нужна: в ней вся переписка открытым текстом." }));
    } else if (item.state === "running") {
      var p = item.progress || {};
      nodes.push(el("p", { class: "small waiting", role: "status", text: "Идёт импорт: " + (p.percent || 0) + "%" + (p.messages_read ? ", прочитано сообщений: " + number(p.messages_read) : "") + ". Страницу можно закрыть — импорт продолжится." }));
      nodes.push(el("progress", { max: "100", value: String(p.percent || 0), "aria-label": "Ход импорта" }));
    } else if (!scan || item.state === "scanning") {
      nodes.push(el("p", { class: "small waiting", role: "status", text: "Читаю файл и считаю чаты. Для большого файла это около минуты…" }));
    } else if (scan.failed) {
      nodes.push(el("p", { class: "note warn", text: scan.failed }));
    } else {
      var excluded = ui.exclude[id] || (ui.exclude[id] = {});
      var chosen = scan.chats.filter(function (c) { return !c.locked && !c.excluded && !excluded[c.key]; });
      var messages = chosen.reduce(function (sum, c) { return sum + c.messages; }, 0);
      nodes.push(el("p", { text: "В выгрузке " + number(scan.chats.length) + " " + plural(scan.chats.length, "чат", "чата", "чатов") + " и " + number(scan.total_messages) + " " + plural(scan.total_messages, "сообщение", "сообщения", "сообщений") +
        (scan.owner ? ". Владелец выгрузки: " + (scan.owner.name || "без имени") + "." : ". В выгрузке один чат: владельца в ней нет.") }));
      nodes.push(el("p", { class: "small", text: "Снимите отметку с чатов, которые не нужны в архиве. Снятый чат запоминается как исключённый: его сообщения не будут сохраняться и из других источников." }));
      nodes.push(el("ul", { class: "import-chats" }, scan.chats.map(function (c) {
        var box = el("input", { type: "checkbox", disabled: c.locked || c.excluded });
        box.checked = !c.locked && !c.excluded && !excluded[c.key];
        box.addEventListener("change", function () {
          if (box.checked) delete excluded[c.key]; else excluded[c.key] = true;
          ui.signatures["import-items"] = "";
          renderImports(S.imports);
        });
        var name = (c.name || "Без названия") + (c.locked ? " — служебный чат Telegram, не принимается" : c.excluded ? " — уже исключён" : "");
        return el("li", {}, [el("label", {}, [box, el("span", { class: "name", text: name })]), el("span", { class: "count", text: number(c.messages) })]);
      })));
      nodes.push(el("p", { class: "counter", text: "К импорту: " + number(chosen.length) + " " + plural(chosen.length, "чат", "чата", "чатов") + ", " + number(messages) + " " + plural(messages, "сообщение", "сообщения", "сообщений") + "." }));
    }
    var buttons = [];
    if (scan && !scan.failed && item.state === "uploaded" && item.file_kept) {
      buttons.push(el("button", { class: "btn", type: "button", text: "Импортировать выбранное", onclick: function () {
        var skip = Object.keys(ui.exclude[id] || {});
        var body = { exclude: skip };
        if (!scan.owner) {
          var owner = prompt("В этой выгрузке один чат, и в ней не сказано, чей это аккаунт.\n\nВведите числовой идентификатор своего аккаунта Telegram (его показывает, например, бот @userinfobot):");
          if (!owner || !/^\d{1,19}$/.test(owner.trim())) return;
          body.owner_id = Number(owner.trim());
        }
        act(this, "import-error", "POST", "imports/" + id + "/run", body, function () { toast("Импорт запущен."); fast(120); });
      } }));
    }
    buttons.push(el("button", { class: "btn ghost small-btn" + (item.state === "running" ? " danger" : ""), type: "button",
      text: item.state === "running" ? "Остановить импорт" : item.file_kept ? "Удалить файл с сервера" : "Убрать из списка",
      onclick: function () {
        if (item.state === "running" && !confirm("Остановить импорт?\n\nУже записанные сообщения останутся в архиве.")) return;
        delete ui.scans[id]; delete ui.exclude[id];
        act(this, "import-error", "DELETE", "imports/" + id, {});
      } }));
    nodes.push(el("div", { class: "row" }, buttons));
    return el("div", { class: "import-item" }, nodes);
  }

  function renderImports(imports) {
    var items = imports.items || [];
    var done = items.some(function (i) { return i.state === "done"; }), active = items.some(function (i) { return i.state === "running"; });
    pill("import", active ? "Идёт импорт" : done ? "Импортировано" : items.length ? "Файл загружен" : "", done && !active ? "done" : items.length ? "todo" : "");
    if (active) fast(10);
    if (items.length && !ui.importOpened) { ui.importOpened = true; $("import-details").open = true; }
    items.forEach(function (item) {
      if (item.file_kept && (item.state === "uploaded" || item.state === "scanning") && !ui.scans[item.import_id]) fetchScan(item.import_id);
    });
    renderIfChanged("import-items", [items, Object.keys(ui.scans), ui.exclude], function () { return items.map(importItem); });
  }

  function wireImport() {
    $("import-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var input = $("import-file"), file = input.files && input.files[0];
      if (!file) { note("import-error", "warn", "Выберите файл result.json из папки выгрузки."); return; }
      var limit = S && S.imports.max_bytes;
      if (limit && file.size > limit) { note("import-error", "warn", "Файл больше, чем сервис принимает (" + megabytes(limit) + "). Выгрузите чаты без вложений или частями."); return; }
      note("import-error", "warn", "");
      var xhr = new XMLHttpRequest();
      ui.upload = xhr;
      show("import-form", false); show("import-progress", true);
      $("import-bar").value = 0;
      text("import-progress-text", "Загружаю файл: 0% из " + megabytes(file.size));
      xhr.open("POST", API + "imports");
      xhr.setRequestHeader("X-Shturman-Setup", "1");
      xhr.setRequestHeader("X-Shturman-Session", key);
      xhr.setRequestHeader("Content-Type", "application/json");
      xhr.upload.onprogress = function (e) {
        if (!e.lengthComputable) return;
        var percent = Math.floor(e.loaded * 100 / e.total);
        $("import-bar").value = percent;
        text("import-progress-text", "Загружаю файл: " + percent + "% из " + megabytes(file.size));
      };
      function finish(message) {
        ui.upload = null;
        show("import-form", true); show("import-progress", false);
        input.value = "";
        if (message) note("import-error", "warn", message);
      }
      xhr.onerror = function () { finish(GENERIC[0]); };
      xhr.onabort = function () { finish("Загрузка отменена."); };
      xhr.onload = function () {
        var data = {};
        try { data = JSON.parse(xhr.responseText || "{}"); } catch (e) { data = {}; }
        if (xhr.status === 401) { finish(""); saveKey(""); loginScreen("Вход устарел. Войдите заново и загрузите файл ещё раз."); return; }
        if (xhr.status !== 201) { finish(data.error || GENERIC[xhr.status] || GENERIC[500]); return; }
        finish("");
        toast("Файл загружен. Считаю чаты…");
        fast(60);
        refresh(); refreshOverview();
      };
      xhr.send(file);
    });
    $("import-abort").addEventListener("click", function () { if (ui.upload) ui.upload.abort(); });
  }

  /* ----------------------------------------------------- 6. бизнес-режим */

  function renderBusiness(b) {
    var ready = b.configured && b.owner_bound && !!b.username;
    var connected = b.business_connections > 0;
    pill("business", !ready ? "" : connected ? "Подключён" : "Не подключён", connected ? "done" : "");
    show("business-nobot", !ready);
    show("business-body", ready);
    if (!ready) return;
    Array.prototype.forEach.call(document.querySelectorAll(".bot-name"), function (node) { node.textContent = "@" + b.username; });
    note("business-status", connected ? "ok" : "info", connected
      ? "Бизнес-режим подключён. Новые сообщения выбранных чатов попадают в архив."
      : "Бизнес-режим ещё не подключён. Когда вы подключите бота в Telegram, это станет видно здесь само.");
    show("business-reply", connected && b.business_can_reply);
    note("business-capable", b.business_capable ? "ok" : "info", b.business_capable
      ? "У бота @" + b.username + " режим включён."
      : "У бота @" + b.username + " режим пока выключен. Включите его у @BotFather и нажмите кнопку ниже.");
  }

  function wireBusiness() {
    $("business-refresh").addEventListener("click", function () {
      act(this, "business-error", "POST", "bot/refresh", {}, function (data) {
        toast(data.business_capable ? "Режим у бота включён." : "Режим у бота пока выключен.", !data.business_capable);
      });
    });
  }

  /* -------------------------------------------------- 7. своя модель */

  function renderLlm(llm) {
    var locked = llm.key.source === "server";
    pill("llm", llm.configured ? "Настроена" : "", llm.configured ? "done" : "");
    show("llm-server", locked);
    show("llm-form", !locked);
    show("llm-remove", llm.key.source === "page");
    if (llm.configured) {
      note("llm-status", llm.problem_text ? "warn" : "ok", llm.problem_text
        ? "Модель «" + llm.model.value + "» настроена, но последнее обращение не удалось. " + llm.problem_text
        : "Сервис обращается к модели «" + llm.model.value + "» по своему ключу." + (llm.last_call_ok === null ? " Обращений после запуска ещё не было." : ""));
    } else note("llm-status", "", "");
    var signature = JSON.stringify([llm.base_url, llm.model, llm.key.set]);
    if (ui.signatures.llm === signature) return;
    ui.signatures.llm = signature;
    var url = $("llm-url"), model = $("llm-model");
    url.disabled = !llm.base_url.editable; model.disabled = !llm.model.editable;
    if (document.activeElement !== url) url.value = llm.base_url.value === "https://api.openai.com/v1" && llm.base_url.editable ? "" : llm.base_url.value;
    if (document.activeElement !== model) model.value = llm.model.value || "";
    text("llm-key-hint", llm.key.set ? "Ключ уже сохранён. Оставьте поле пустым, чтобы сменить только модель. Если меняете адрес — вставьте ключ заново." : "Ключ провайдера с API, совместимым с OpenAI: OpenAI, OpenRouter и подобные.");
    if (!llm.base_url.editable) text("llm-url-hint", "Задан в настройках сервера: здесь его не поменять.");
    if (!llm.model.editable) text("llm-model-hint", "Задано в настройках сервера.");
  }

  function wireLlm() {
    $("llm-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var key = $("llm-key"), body = { api_key: key.value.trim(), base_url: $("llm-url").value.trim(), model: $("llm-model").value.trim() };
      key.value = "";
      act($("llm-save"), "llm-error", "PUT", "llm", body, function (data) {
        ui.signatures.llm = "";
        toast("Модель ответила, ключ сохранён" + (data.model ? ": " + data.model : "") + ".");
      });
    });
    $("llm-remove").addEventListener("click", function () {
      if (!confirm("Убрать свою модель сервиса?\n\nСервис перестанет обращаться к модели сам.")) return;
      act(this, "llm-error", "DELETE", "llm", {}, function () { ui.signatures.llm = ""; toast("Своя модель убрана."); });
    });
  }

  /* --------------------------------------------------- 8. что собрано */

  function fact(title, value) {
    return el("div", { class: "fact" }, [el("b", { text: title }), el("span", { class: "value", text: value })]);
  }

  function renderSummary(data) {
    var a = data.archive || {};
    $("sending-note").hidden = !!a.sending;
    text("collected", number(a.messages));
    renderIfChanged("tiles", a, function () {
      return [
        ["Сообщений в архиве", a.messages], ["Чатов", a.chats], ["Исключено чатов", a.chats_excluded], ["Аккаунтов", a.accounts]
      ].map(function (t) { return el("div", { class: "tile" }, [el("b", { text: number(t[1]) }), el("span", { text: t[0] })]); });
    });
    renderIfChanged("facts", a, function () {
      var search = !a.embeddings_enabled
        ? "Выключен: поиск идёт по словам. Включает оператор на сервере: ./ops/embeddings.sh on"
        : a.embeddings_problem ? "Включён, но модель не отвечает. Проверка на сервере: ./ops/embeddings.sh status"
        : "Включён. Посчитано сообщений: " + number(a.embeddings_embedded) + (a.embeddings_left ? ", осталось: " + number(a.embeddings_left) : "");
      var guard = !a.guard_enabled
        ? "Выключена: входящие сообщения ассистент читает без проверки. Включает оператор на сервере: ./ops/guard.sh on"
        : "Включена" + (a.guard_model_used ? "" : " (без модели — только правила)") + ". Проверено: " + number(a.guard_checked) + ", скрыто: " + number(a.guard_hidden) +
          (a.guard_problem ? ". Модель сейчас не отвечает." : "");
      return [
        fact("Последнее новое сообщение", a.last_message_seen_at ? when(a.last_message_seen_at) : "ещё не было"),
        fact("Очередь заданий", "ждут: " + number(a.jobs_waiting) + ", не выполнено: " + number(a.jobs_failed)),
        fact("Поиск по смыслу", search),
        fact("Защита от внедрённых инструкций", guard),
        fact("Отправка сообщений", a.sending ? "Включена в настройках сервера." : "Выключена. Включается только в настройках сервера, не здесь.")
      ];
    });
    [["audit-key", data.audit_key], ["audit", data.audit]].forEach(function (pair) {
      var rows = pair[1];
      renderIfChanged(pair[0], rows, function () {
        if (!rows || !rows.length) return [el("li", {}, [el("span", { class: "detail", text: "Пока ничего не менялось." })])];
        return rows.map(function (row) {
          var bad = row.outcome !== "ok";
          return el("li", {}, [
            el("time", { datetime: row.at, text: when(row.at) }),
            el("span", { class: "what" + (bad ? " bad" : ""), text: row.title }),
            row.detail ? el("span", { class: "detail", text: row.detail }) : null
          ]);
        });
      });
    });
  }

  function wireSession() {
    $("logout").addEventListener("click", function () {
      call("POST", "logout", {}, true).then(function () { saveKey(""); loginScreen(""); });
    });
    $("logout-all").addEventListener("click", function () {
      if (!confirm("Завершить все сессии этой страницы — на всех устройствах?\n\nВойти снова можно будет по новой ссылке: ./ops/setup-link.sh")) return;
      call("POST", "logout-all", {}, true).then(function () { saveKey(""); loginScreen(""); });
    });
    document.addEventListener("visibilitychange", function () { if (!document.hidden && key) { refresh(); refreshOverview(); } });
    // Вышли в соседней вкладке — выходим и здесь; вошли — подхватываем вход.
    window.addEventListener("storage", function (event) {
      if (event.key !== null && event.key !== KEY_NAME) return;
      var stored = loadKey();
      if (stored === key) return;
      key = stored;
      loginScreen("");
    });
  }

  /* ------------------------------------------------------------- запуск */

  wireLogin(); wireBot(); wireKeys(); wireAccounts(); wireChats(); wireImport(); wireBusiness(); wireLlm(); wireSession();

  function offerLink() {
    // По ссылке входим только после нажатия: предпросмотр ссылки в мессенджере её не израсходует.
    stopLoop();
    show("screen-app", false);
    show("screen-login", true);
    loginView("login-link");
  }

  // Ссылку могли вставить в адресную строку уже открытой страницы: тогда меняется только часть после «#».
  window.addEventListener("hashchange", function () {
    if (!takeLinkToken()) return;
    if (key) {
      // Вход уже выполнен: ссылка не нужна. Убираем её из адреса, не расходуя.
      try { history.replaceState(null, "", location.pathname + location.search); } catch (e) { /* не критично */ }
      return;
    }
    offerLink();
  });

  key = loadKey();
  if (!takeLinkToken()) loginScreen("");
  else if (!key) offerLink();
  else {
    // Ссылку открыли там, где вход, возможно, уже есть. Если он действует — ссылка не нужна:
    // убираем её из адреса, не расходуя. Если устарел — предлагаем войти по ссылке.
    call("GET", "session", undefined, true).then(function (r) {
      if (r.ok && r.data.authenticated) {
        try { history.replaceState(null, "", location.pathname + location.search); } catch (e) { /* не критично */ }
        startApp();
        return;
      }
      if (r.ok) saveKey("");
      offerLink();
    });
  }
})();
