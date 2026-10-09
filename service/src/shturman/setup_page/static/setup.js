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
    messages: null, importOpened: false,
    subUrl: ""                  // адрес входа в ChatGPT текущей попытки — только в памяти страницы
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
        // Ссылка одноразовая. Если владелец открыл её впервые, а она уже израсходована, по ней мог войти
        // кто-то другой (docs/architecture.md, «Что осталось») — говорим, что делать.
        loginMessage((r.error || GENERIC[500]) + " Если вы открываете эту ссылку впервые, попросите того, кто ставил ассистента, " +
          "завершить все входы (./ops/logout-all.sh) и выдать новую ссылку.", false);
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
    mem.pages = mem.pending = mem.person = mem.projects = mem.project = mem.chats = mem.profile = null;
    mem.personId = mem.projectId = null;
    mem.creating = false;
    if (memOpen()) showMemory(true);
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
    refreshMemoryCount();
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
    renderMode(S.scenario || {});
    renderKeys(S.tg);
    renderAccounts(S.tg);
    renderChats(S.tg);
    renderImports(S.imports);
    renderBot(S.bot);
    renderBusiness(S.bot);
    renderLlm(S.llm);
    renderMedia(S.media || {});
    renderProgress();
  }

  /* Шаги зависят от выбранного способа подключения:
   *   own   — «ассистент видит всё как вы»: вход в основной аккаунт, выбор чатов;
   *   staff — «ассистент — отдельный сотрудник»: бизнес-режим для личных чатов и
   *           аккаунт ассистента для групп.
   * Шаг другого способа, где уже что-то подключено, остаётся виден без номера и с пояснением:
   * иначе подключённое нельзя было бы ни увидеть, ни отключить. */
  var PLAN = {
    none: ["s-bot", "s-mode"],
    own: ["s-bot", "s-mode", "s-keys", "s-accounts", "s-chats", "s-import", "s-media"],
    staff: ["s-bot", "s-mode", "s-business", "s-keys", "s-assistant", "s-chats", "s-import", "s-media"]
  };
  var ALL_STEPS = ["s-bot", "s-mode", "s-business", "s-keys", "s-accounts", "s-assistant", "s-chats", "s-import", "s-media"];

  function lockStep(id, reason, done) {
    var card = $(id), lock = card.querySelector(".step-lock");
    card.classList.toggle("locked", !!reason);
    card.classList.toggle("is-done", !!done);
    if (lock) { lock.textContent = reason || ""; lock.hidden = !reason; }
  }

  function facts() {
    var tg = S.tg, b = S.bot, sc = (S.scenario || {}).chosen || null;
    var owner = accountOf(tg, "owner"), helper = accountOf(tg, "assistant");
    var accounts = (tg.accounts || []).filter(function (a) { return a.account_id !== null; });
    return {
      sc: sc, bot: !!(b.configured && b.owner_bound), keys: tg.keys.configured,
      owner: owner, helper: helper,
      ownerOk: !!(owner && owner.status === "running"), helperOk: !!(helper && helper.status === "running"),
      business: b.business_connections > 0,
      // Чаты считаются у аккаунта выбранного способа: свой — для «как вы», ассистента — для «сотрудника».
      chats: accounts.filter(function (a) { return !sc || a.role === (sc === "staff" ? "assistant" : "owner"); })
        .reduce(function (sum, a) { return sum + (a.chats_enabled || 0); }, 0),
      imported: (S.imports.items || []).some(function (i) { return i.state === "done"; }),
      importsAny: (S.imports.items || []).length > 0,
      media: !!(S.media && S.media.enabled)
    };
  }

  function renderProgress() {
    var f = facts(), plan = PLAN[f.sc] || PLAN.none;
    var num = function (id) { return plan.indexOf(id) + 1; };
    var BOT = "Откроется после шага 1 — когда бот согласований будет сохранён и привязан к вам.";
    var done = {
      "s-bot": f.bot, "s-mode": !!f.sc, "s-business": f.business, "s-keys": f.keys,
      "s-accounts": f.ownerOk, "s-assistant": f.helperOk, "s-chats": f.chats > 0, "s-import": f.imported,
      "s-media": f.media
    };
    // Какой шаг держит закрытым этот: для строки «после шага N».
    var blocker = {
      "s-business": 1, "s-keys": 1,
      "s-accounts": !f.bot ? 1 : num("s-keys"), "s-assistant": !f.bot ? 1 : num("s-keys"),
      "s-chats": !f.bot ? 1 : num(f.sc === "staff" ? "s-assistant" : "s-accounts")
    };
    var lock = {
      "s-business": !f.bot ? BOT : "",
      "s-keys": !f.bot ? BOT : "",
      "s-accounts": !f.bot ? BOT : !f.keys ? "Откроется, когда будут сохранены ключи приложения — шаг " + num("s-keys") + "." : "",
      "s-assistant": !f.bot ? BOT : !f.keys ? "Откроется, когда будут сохранены ключи приложения — шаг " + num("s-keys") + "." : "",
      "s-chats": !f.bot ? BOT : f.sc === "staff"
        ? (!f.helper ? "Откроется, когда будет подключён аккаунт ассистента — шаг " + num("s-assistant") + "." : "")
        : (!f.owner ? "Откроется после входа в аккаунт — шаг " + num("s-accounts") + "." : "")
    };
    // Подключённое раньше или в другом способе остаётся видно, чтобы его можно было отключить.
    var leftover = {
      "s-accounts": !!f.owner, "s-assistant": !!f.helper, "s-business": f.business,
      "s-chats": !!(f.owner || f.helper), "s-import": f.importsAny, "s-media": f.media
    };
    var why = f.sc ? "Не входит в выбранный способ. Виден, потому что уже подключён; если не нужен — отключите."
                   : "Подключено раньше. Выберите способ подключения — тогда страница покажет нужные шаги.";
    ALL_STEPS.forEach(function (id) {
      var card = $(id), planned = plan.indexOf(id) >= 0, extra = !planned && !!leftover[id];
      card.hidden = !planned && !extra;
      var numNode = card.querySelector(".num");
      if (numNode) numNode.textContent = planned ? String(num(id)) : "";
      var other = card.querySelector(".other-note");
      if (other) { other.textContent = extra ? why : ""; other.hidden = !extra; }
      lockStep(id, planned ? lock[id] || "" : "", done[id]);
    });

    var items;
    if (f.sc === "own") items = [["s-bot", "Бот согласований"], ["s-mode", "Способ"], ["s-keys", "Ключи приложения"], ["s-accounts", "Вход в аккаунт"], ["s-chats", "Выбор чатов"]];
    else if (f.sc === "staff") items = [["s-bot", "Бот согласований"], ["s-mode", "Способ"], ["s-business", "Бизнес-режим"], ["s-chats", "Группы", true, num("s-keys") + "–" + num("s-chats")]];
    else items = [["s-bot", "Бот согласований"], ["s-mode", "Способ подключения"]];
    var current = null;
    var steps = items.map(function (it) {
      var id = it[0], ok = done[id], optional = !!it[2], state;
      if (ok) state = id === "s-chats" ? "выбрано: " + f.chats : id === "s-mode" ? (f.sc === "own" ? "как вы" : "сотрудник") : "готово";
      else if (optional) state = "по желанию";
      else if (!current && !lock[id]) { current = id; state = "сделайте сейчас"; }
      else if (!lock[id]) state = "можно сейчас";
      else state = "после шага " + blocker[id];
      return [it[1], ok, state, it[3] || num(id)];
    });
    renderIfChanged("progress", [steps, f.imported], function () {
      var nodes = steps.map(function (s) {
        var now = !s[1] && s[2] === "сделайте сейчас";
        return el("li", { class: s[1] ? "done" : now ? "now" : "wait" }, [
          el("span", { class: "p-mark", "aria-hidden": "true", text: s[1] ? "✓" : String(s[3]) }),
          el("span", { class: "p-name", text: s[0] }),
          el("span", { class: "p-state", text: s[2] })
        ]);
      });
      if (f.imported) nodes.push(el("li", { class: "done" }, [
        el("span", { class: "p-mark", "aria-hidden": "true", text: "✓" }),
        el("span", { class: "p-name", text: "Выгрузка" }), el("span", { class: "p-state", text: "импортирована" })
      ]));
      return nodes;
    });
  }

  /* ------------------------------------------------ способ подключения */

  var MODE_NAMES = { own: "Как вы", staff: "Сотрудник" };

  function renderMode(sc) {
    var chosen = sc.chosen || null;
    document.body.classList.toggle("sc-own", chosen === "own");
    document.body.classList.toggle("sc-staff", chosen === "staff");
    pill("mode", chosen ? "Выбрано: " + MODE_NAMES[chosen].toLowerCase() : "Не выбран", chosen ? "done" : "");
    // На экземпляре, где что-то уже подключено, подсказываем способ — но выбирает владелец.
    note("mode-hint", "info", !chosen && sc.suggested ? "Судя по тому, что уже подключено, это способ «" +
      (sc.suggested === "own" ? "Ассистент видит всё как вы" : "Ассистент — отдельный сотрудник") +
      "». Выберите его, чтобы страница показала оставшиеся шаги, — или другой." : "");
    ["own", "staff"].forEach(function (name) {
      var on = chosen === name, button = $("mode-" + name);
      $("choice-" + name).classList.toggle("is-chosen", on);
      button.disabled = on;
      button.textContent = on ? "Выбран" : chosen ? "Сменить на этот способ" : "Выбрать этот способ";
      button.className = on || !chosen ? "btn" : "btn ghost";
    });
  }

  function wireMode() {
    ["own", "staff"].forEach(function (name) {
      $("mode-" + name).addEventListener("click", function () {
        var chosen = S && S.scenario && S.scenario.chosen;
        if (chosen && chosen !== name && !confirm("Сменить способ подключения?\n\nНичего не удаляется и не отключается: собранное остаётся в архиве, подключённое — подключённым. Изменится набор шагов на странице. То, что не нужно в новом способе, вы отключите сами.")) return;
        act(this, "mode-error", "PUT", "scenario", { scenario: name }, function () {
          toast(name === "own" ? "Способ выбран. Дальше — шаги для входа в ваш аккаунт." : "Способ выбран. Дальше — бизнес-режим для личных чатов.");
        });
      });
    });
  }

  /* ----------------------------------------------- фото и документы */

  function renderMedia(m) {
    pill("media", m.enabled ? "Включён" : "Выключен", m.enabled ? "done" : "");
    text("media-days", String(m.days || 30));
    text("media-max", String(m.max_mb || 20));
    show("media-on", !m.enabled);
    show("media-off", !!m.enabled);
    note("media-status", "info", m.enabled ? "Разбор включён. Сколько разобрано — в разделе «Дополнительно» → «Что собрано»." : "");
  }

  function wireMedia() {
    $("media-on").addEventListener("click", function () {
      if (!confirm("Включить разбор фото и документов?\n\nФото, сканы и текст документов из переписки будут уходить модели — её провайдеру, как уже уходит текст сообщений. Сами файлы на сервере не хранятся: разобрав, сервис их удаляет.")) return;
      act(this, "media-error", "PUT", "media", { enabled: true }, function () {
        toast("Разбор включён. Свежие фото и документы разберутся в ближайшие минуты.");
      });
    });
    $("media-off").addEventListener("click", function () {
      act(this, "media-error", "PUT", "media", { enabled: false }, function () {
        toast("Разбор выключен. Уже разобранное остаётся в архиве.");
      });
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
        id.value = ""; ui.keysEditing = false; toast("Ключи сохранены. Переходите к следующему шагу.");
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
      nodes.push(el("label", { class: "check" }, [consent, el("span", { text: "Понимаю: на сервере будет храниться вход в мой основной аккаунт. Если Telegram сочтёт его подозрительным, ограничения коснутся моего номера. Отключить его можно в Telegram: «Настройки» → «Устройства»." })]));
    }
    if (role === "assistant" && !tg.owner_known) {
      nodes.push(el("p", { class: "note info", text: "Сначала привяжите себя к боту согласований — шаг 1. Пока сервис не знает, какой аккаунт ваш, он не сможет отличить его от аккаунта ассистента." }));
      return nodes;
    }
    nodes.push(el("div", { class: "row" }, [el("button", {
      class: "btn", type: "button", text: again ? "Войти заново" : role === "owner" ? "Показать QR-код для входа" : "Подключить аккаунт ассистента",
      onclick: function () {
        if (consent && !consent.checked) { toast("Отметьте, что понимаете: на сервере будет храниться вход в ваш основной аккаунт.", true); consent.focus(); return; }
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
        if (!confirm("Выйти из аккаунта?\n\nСессия на сервере завершится. Уже сохранённые сообщения останутся в архиве.\n\nЕсли подключили не тот аккаунт — после выхода удалите его из архива в блоке «Аккаунты в архиве без подключения».")) return;
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
    var detached = tg.detached || [];
    show("detached-box", detached.length > 0);
    renderIfChanged("detached-list", [detached], function () { return detached.map(detachedNode); });
  }

  function detachedNode(account) {
    var id = account.account_id, name = account.label || "Аккаунт";
    return el("div", { class: "row" }, [
      el("span", { class: "account-name", text: name }),
      el("span", { class: "small", text: "записан как " + account.role_name + " · сообщений: " + account.messages }),
      el("button", { class: "btn ghost danger small-btn", type: "button", text: "Удалить из архива", onclick: function () {
        if (!confirm("Удалить «" + name + "» из архива?\n\nУдалятся его чаты и сообщения (" + account.messages +
                     ") и всё, что сервис из них извлёк. Вернуть их нельзя. Если подключить этот аккаунт снова, переписка загрузится заново.")) return;
        act(this, null, "DELETE", "tg/accounts/" + id, {}, function () { toast("Аккаунт удалён из архива. Теперь его можно подключить заново."); });
      } })
    ]);
  }

  /* Блок с QR один на оба аккаунта: он переезжает туда, где начали вход. */
  function placeLogin(role) {
    var slot = $("login-slot-" + role), box = $("tg-login");
    if (slot && box.parentNode !== slot) slot.appendChild(box);
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
    text("tg-login-title", flow.role === "owner" ? "Вход в ваш аккаунт" : "Вход в аккаунт ассистента");
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
      toast("Аккаунт подключён. Теперь выберите, какие чаты читать.");
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
    var total = all.filter(function (a) { return !(S.scenario || {}).chosen || a.role === ((S.scenario || {}).chosen === "staff" ? "assistant" : "owner"); })
      .reduce(function (sum, a) { return sum + (a.chats_enabled || 0); }, 0);
    pill("chats", all.length ? (total ? "Выбрано: " + total : "Выберите чаты") : "", total ? "done" : all.length ? "todo" : "");
    show("chats-none", !all.length);
    show("chats-body", !!all.length);
    if (!all.length) { ui.chatAccount = null; return; }
    var sc = (S.scenario || {}).chosen, mine = sc ? (sc === "staff" ? "assistant" : "owner") : null;
    if (ui.chatScenario !== sc) { ui.chatScenario = sc; ui.chatAccount = null; }   // сменили способ — своя вкладка
    if (!all.some(function (a) { return a.account_id === ui.chatAccount; })) {
      var preferred = all.filter(function (a) { return a.role === mine; })[0];
      ui.chatAccount = (preferred || accounts[0] || all[0]).account_id;
      ui.chatsKey = "";
    }
    var current = all.filter(function (a) { return a.account_id === ui.chatAccount; })[0];
    show("chats-accounts", all.length > 1);
    renderIfChanged("chats-accounts", [all.map(function (a) { return [a.account_id, a.label, a.role]; }), ui.chatAccount], function () {
      return all.map(function (a) {
        return el("button", {
          class: "tab", type: "button", role: "tab", "aria-selected": a.account_id === ui.chatAccount ? "true" : "false",
          text: (a.role === "owner" ? "Ваш: " : "Ассистента: ") + (a.label || ""),
          onclick: function () { ui.chatAccount = a.account_id; ui.chatsKey = ""; render(); }
        });
      });
    });
    var online = current.status === "running";
    note("chats-offline", "warn", online ? "" : "Этот аккаунт сейчас не подключён, поэтому список чатов недоступен. Его состояние — в шаге со входом в аккаунт.");
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
      if (item.kind === "zip") nodes.push(el("p", { class: st.media_no_space ? "note warn" : "small", text: mediaSummary(st) }));
      nodes.push(el("p", { class: "small", text: "Файл с сервера удалён. Удалите выгрузку и со своего компьютера, если она больше не нужна: в ней вся переписка открытым текстом." }));
    } else if (item.state === "running") {
      var p = item.progress || {};
      nodes.push(el("p", { class: "small waiting", role: "status", text: "Идёт импорт: " + (p.percent || 0) + "%" + (p.messages_read ? ", прочитано сообщений: " + number(p.messages_read) : "") + (p.media_files ? ", файлов взято на разбор: " + number(p.media_files) : "") + ". Страницу можно закрыть — импорт продолжится." }));
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

  function mediaSummary(st) {
    var taken = st.media_files || 0, skipped = st.media_skipped || 0;
    if (st.media_no_space) return "Файлов из архива взято на разбор: " + number(taken) + ". Остальные не взяты: на диске сервера кончилось место. Сообщения при этом импортированы.";
    if (!taken && !skipped) return "Файлы из архива не брались: расшифровка голосовых и разбор фото и документов выключены или подходящих файлов в архиве нет.";
    return "Файлов из архива взято на разбор: " + number(taken) + (skipped ? ", не взято: " + number(skipped) + " (нет в архиве, слишком большие или уже не нужны)" : "") + ". Разобрав файл, сервис его удаляет.";
  }

  function renderImports(imports) {
    var items = imports.items || [];
    if (imports.max_bytes) text("import-limit", megabytes(imports.max_bytes));
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
      if (!file) { note("import-error", "warn", "Выберите архив папки выгрузки (zip) или файл result.json."); return; }
      var limit = S && S.imports.max_bytes;
      if (limit && file.size > limit) { note("import-error", "warn", "Файл больше, чем сервис принимает (" + megabytes(limit) + "). Выгрузите заново без видео или с меньшим пределом размера файлов, частями — или загрузите один result.json."); return; }
      note("import-error", "warn", "");
      var xhr = new XMLHttpRequest();
      ui.upload = xhr;
      show("import-form", false); show("import-progress", true);
      $("import-bar").value = 0;
      text("import-progress-text", "Загружаю файл: 0% из " + megabytes(file.size));
      xhr.open("POST", API + "imports");
      xhr.setRequestHeader("X-Shturman-Setup", "1");
      xhr.setRequestHeader("X-Shturman-Session", key);
      xhr.setRequestHeader("Content-Type", /\.zip$/i.test(file.name) ? "application/zip" : "application/json");
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
    var bySubscription = llm.way === "subscription";
    pill("llm", llm.configured ? (bySubscription ? "Подписка ChatGPT" : "Ключ API") : "", llm.configured ? "done" : "");
    show("llm-server", locked);
    show("llm-form", !locked);
    show("llm-remove", llm.key.source === "page");
    if (ui.llmWay !== undefined && ui.llmWay !== llm.way) {
      // Способ сменился — прежние ошибки обоих способов больше не о том.
      note("llm-error", "warn", "");
      note("sub-error", "warn", "");
    }
    ui.llmWay = llm.way;
    renderSubscription(llm.subscription || {}, bySubscription);
    if (llm.configured && !bySubscription) {
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

  /* Подписка ChatGPT. Вход — «вставкой адреса»: сервер не на компьютере владельца, поэтому адрес
   * возврата http://127.0.0.1:1455/… в браузере не открывается, и владелец копирует его сюда. */
  var SUB_PILLS = { connected: ["Подключена", "done"], limit: ["Лимит исчерпан", "todo"], relogin: ["Войти заново", "todo"], denied: ["Нет доступа", "todo"] };

  function renderSubscription(sub, active) {
    var status = sub.status || "none";
    var p = active ? SUB_PILLS[status] : null;
    pill("sub", p ? p[0] : "", p ? p[1] : "");
    show("sub-locked", !sub.available);
    text("sub-locked", sub.locked_text || "");
    if (status !== "none") {
      var parts = [sub.status_text];
      if (sub.email) parts.push("Учётная запись ChatGPT: " + sub.email + ".");
      if (active && sub.model) parts.push("Модель: " + subModelName(sub) + ".");
      if (sub.problem_text) parts.push(sub.problem_text);
      note("sub-status", status === "connected" ? "ok" : status === "signed_out" ? "info" : "warn", parts.join(" "));
    } else note("sub-status", "", "");
    var pending = !!sub.attempt && sub.available;
    show("sub-paste", pending);
    show("sub-actions", sub.available && !pending);
    if (!pending) ui.subUrl = "";
    var link = $("sub-open");
    link.hidden = !ui.subUrl;
    if (ui.subUrl) link.href = ui.subUrl;
    text("sub-start", status === "connected" || status === "limit" || status === "denied" ? "Войти заново" : "Войти через ChatGPT");
    show("sub-new", !!sub.registered);
    // Адрес лимитов приходит с сервера: в разметке страницы чужих адресов нет.
    if (sub.usage_url) $("sub-usage").href = sub.usage_url;
    show("sub-usage", active && !!sub.usage_url);
    show("sub-remove", active || status === "relogin");
    var models = sub.models || [];
    show("sub-model-box", active && models.length > 0);
    renderIfChanged("sub-model", [models, sub.model], function () {
      return models.map(function (m) {
        var option = el("option", { value: m.slug, text: m.display_name || m.slug });
        if (m.slug === sub.model) option.selected = true;
        return option;
      });
    });
  }

  function subModelName(sub) {
    var found = (sub.models || []).filter(function (m) { return m.slug === sub.model; })[0];
    return found ? found.display_name : sub.model;
  }

  function startSubscription(button, fresh) {
    var body = { "new": !!fresh };
    if (S && S.llm && S.llm.key.source === "page") {
      if (!confirm("Сейчас своя модель сервиса работает по ключу API.\n\nПодписка ChatGPT его заменит: после входа ключ будет удалён с сервера. Продолжить?")) return;
      body["switch"] = true;
    }
    busy(button, true);
    note("sub-error", "warn", "");
    call("POST", "llm/chatgpt/start", body).then(function (r) {
      busy(button, false);
      if (!r.ok) { note("sub-error", "warn", r.error); return; }
      ui.subUrl = r.data.url;
      try { window.open(r.data.url, "_blank", "noopener,noreferrer"); } catch (e) { /* откроют ссылкой */ }
      refresh().then(function () { $("sub-address").focus(); });
    });
  }

  function wireSubscription() {
    $("sub-start").addEventListener("click", function () { startSubscription(this, false); });
    $("sub-new").addEventListener("click", function () {
      if (!confirm("Войти другой учётной записью ChatGPT?\n\nПосле входа сервис будет пользоваться подпиской новой учётной записи.")) return;
      startSubscription(this, true);
    });
    $("sub-paste").addEventListener("submit", function (event) {
      event.preventDefault();
      var input = $("sub-address"), address = input.value.trim();
      if (!address) { note("sub-error", "warn", "Вставьте адрес из адресной строки вкладки, где вы вошли в ChatGPT."); return; }
      input.value = "";                 // в адресе одноразовый код входа — на странице его не держим
      var button = $("sub-finish");
      busy(button, true);
      note("sub-error", "warn", "");
      call("POST", "llm/chatgpt/finish", { address: address }).then(function (r) {
        busy(button, false);
        if (!r.ok) { note("sub-error", "warn", r.error); refresh(); return; }
        ui.subUrl = "";
        toast(r.data.probe === "ok" ? "Подписка ChatGPT подключена: модель ответила." : "Подписка ChatGPT подключена. " + (r.data.probe_text || ""), r.data.probe !== "ok");
        refresh();
        refreshOverview();
      });
    });
    $("sub-cancel").addEventListener("click", function () {
      ui.subUrl = "";
      note("sub-error", "warn", "");
      act(this, "sub-error", "POST", "llm/chatgpt/cancel", {});
    });
    $("sub-model").addEventListener("change", function () {
      var model = this.value;
      act(null, "sub-error", "PUT", "llm/chatgpt/model", { model: model }, function () { toast("Модель подписки выбрана."); });
    });
    $("sub-remove").addEventListener("click", function () {
      if (!confirm("Выйти из подписки ChatGPT?\n\nСервис перестанет обращаться к модели по подписке; сессия у OpenAI будет отозвана.")) return;
      act(this, "sub-error", "DELETE", "llm/chatgpt", {}, function (data) {
        toast(data.revoked ? "Вы вышли из подписки ChatGPT." : "Вы вышли из подписки, но OpenAI не подтвердил отзыв. Отключить доступ можно в настройках ChatGPT.", !data.revoked);
      });
    });
  }

  function wireLlm() {
    wireSubscription();
    $("llm-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var key = $("llm-key"), body = { api_key: key.value.trim(), base_url: $("llm-url").value.trim(), model: $("llm-model").value.trim() };
      if (S && S.llm && S.llm.way === "subscription") {
        if (!confirm("Сейчас своя модель сервиса работает по подписке ChatGPT.\n\nКлюч API её заменит: сервис выйдет из подписки. Продолжить?")) return;
        body["switch"] = true;
      }
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
    text("faq-sending", a.sending
      ? "Отправка сообщений включена в настройках сервера. Ассистент отправляет только после вашего «Да» в боте согласований или по правилам, которые вы разрешили; с этой страницы отправку не включить и не выключить."
      : "Сейчас нет: отправка сообщений выключена в настройках сервера и с этой страницы не включается. Ассистент читает, запоминает и готовит черновики. Включить отправку — отдельное решение, которое принимаете вы.");
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
        fact("Голосовые и «кружки»", !a.voice_enabled
          ? "Не расшифровываются: ассистент видит, что было голосовое, но не его содержание. Включает оператор на сервере: ./ops/asr.sh on"
          : a.voice_problem ? "Расшифровка включена, но распознавание сейчас не отвечает. Ждут: " + number(a.voice_pending)
          : "Расшифровываются. Готово: " + number(a.voice_done) + (a.voice_pending ? ", ждут: " + number(a.voice_pending) : "")),
        fact("Фото и документы", !a.media_enabled
          ? "Не разбираются: ассистент видит, что было фото или файл, но не что в нём. Включается в шаге «Фото и документы»."
          : "Разбираются. Готово: " + number(a.media_done) + ((a.media_pending || a.media_asking) ? ", ждут: " + number((a.media_pending || 0) + (a.media_asking || 0)) : "") +
            (a.media_skipped ? ", пропущено: " + number(a.media_skipped) : "") + (a.media_failed ? ", не получилось: " + number(a.media_failed) : "")),
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

  /* ---------------------------------------------------- память ассистента
   * Не шаг настройки, а отдельная карточка после шагов. Разделы — вкладки из MEMORY_TABS: у каждой
   * id, подпись, load() — запрос данных (обещание, итог — текст ошибки или "") и draw() — узлы
   * панели; count() — число на вкладке. Новый раздел — ещё один объект в MEMORY_TABS и его маршруты
   * /memory/… на сервере (memory.py). Пустых разделов-заглушек на странице нет.
   * Всё, что пришло с сервера, выводится через textContent: ссылок из текста страниц нет. */

  var MEM_LIMIT = 20000;
  var mem = {
    tab: "people", q: "", pages: null, pending: null, person: null, personId: null,
    projects: null, project: null, projectId: null, creating: false, chats: null, profile: null,
    error: "", timer: null, seq: 0
  };

  var FLAG_WORDS = {
    frozen: ["заморожена", "Файл страницы правили вручную, и ассистент перестал понимать, где какой блок. Пока файл не исправят, страница не обновляется, а заметки здесь не сохраняются. Исправить его может тот, кто сопровождает сервер."],
    summary_not_updated: ["сводка не обновлена", "Сводка не обновилась при последней ночной сборке — показана прежняя. Обычно это исправляется следующей ночью."],
    summary_pending: ["сводка пересобирается", "Ассистент как раз пересобирает сводку. Загляните через несколько минут."],
    waiting: ["скоро обновится", "Есть новые сведения: страница обновится в ближайшие минуты."],
    no_file: ["ещё не записана", "Страница заведена, но ещё не записана: появится после ближайшей сборки, обычно ночью. Заметки можно написать уже сейчас."]
  };
  var ORIGIN_WORDS = { "сказал владелец": "сказали вы", "сказал собеседник": "сказал собеседник", "вывела модель": "вывод ассистента" };
  var CHAT_KINDS = { personal: "личный чат", group: "группа", channel: "канал" };

  var MEMORY_TABS = [
    { id: "people", label: "Люди", load: loadPeople, draw: drawPeople },
    { id: "projects", label: "Проекты", load: loadProjects, draw: drawProjects },
    { id: "profile", label: "Профиль", load: loadProfile, draw: drawProfile },
    { id: "pending", label: "Ждут решения", load: loadPending, draw: drawPending,
      count: function () { return mem.pending ? mem.pending.total : 0; } }
  ];

  function memTab(id) { return MEMORY_TABS.filter(function (t) { return t.id === id; })[0] || MEMORY_TABS[0]; }
  function memOpen() { return $("memory-details").open; }

  function day(iso) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
    if (!m) return "";
    return new Date(+m[1], +m[2] - 1, +m[3]).toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: "numeric" });
  }

  function sourcesText(item) {
    var parts = [];
    if (item.sources) parts.push(item.sources === 1 ? "сообщение" : item.sources + " " + plural(item.sources, "сообщение", "сообщения", "сообщений"));
    if (item.origin) parts.push(ORIGIN_WORDS[item.origin] || item.origin);
    return parts.join(" · ");
  }

  function memPill() {
    var n = mem.pending ? mem.pending.total : 0;
    pill("memory", n ? "ждут решения: " + n : "", n ? "todo" : "");
  }

  /* Ждущее решения нужно и для числа на свёрнутой карточке: спрашивается вместе со счётчиками. */
  function refreshMemoryCount() {
    return call("GET", "memory/pending", undefined, true).then(function (r) {
      if (!r.ok) return;
      var before = JSON.stringify(mem.pending);
      mem.pending = r.data;
      memPill();
      if (memOpen() && before !== JSON.stringify(mem.pending)) drawMemoryTabs();
    });
  }

  function drawMemoryTabs() {
    var box = $("mem-tabs");
    box.textContent = "";
    MEMORY_TABS.forEach(function (tab) {
      var n = tab.count ? tab.count() : 0;
      box.appendChild(el("button", {
        class: "tab", type: "button", role: "tab", id: "mem-tab-" + tab.id, "aria-controls": "mem-panel",
        "aria-selected": tab.id === mem.tab ? "true" : "false",
        onclick: function () {
          if (mem.tab === tab.id) return;
          mem.tab = tab.id; mem.personId = null; mem.projectId = null; mem.creating = false;
          showMemory(true);
        }
      }, [tab.label, n ? el("span", { class: "tab-count", text: String(n) }) : null]));
    });
    $("mem-panel").setAttribute("aria-labelledby", "mem-tab-" + mem.tab);
  }

  function memPanel(nodes) {
    var panel = $("mem-panel");
    panel.textContent = "";
    (Array.isArray(nodes) ? nodes : [nodes]).forEach(function (n) { if (n) panel.appendChild(n); });
  }

  /* Рисует раздел; reload — сначала заново спросить данные у сервера. */
  function showMemory(reload) {
    var tab = memTab(mem.tab), seq = ++mem.seq;
    drawMemoryTabs();
    if (!reload && !mem.error) { memPanel(tab.draw()); return Promise.resolve(); }
    memPanel(el("p", { class: "small waiting", role: "status", text: "Загружаю…" }));
    return tab.load().then(function (error) {
      if (seq !== mem.seq) return;               // пока ждали, владелец ушёл в другой раздел
      mem.error = error || "";
      drawMemoryTabs();
      memPanel(error ? el("p", { class: "note warn", role: "alert", text: error }) : tab.draw());
    });
  }

  function openAndFocus() { showMemory(true).then(function () { $("mem-panel").focus(); }); }

  /* ---- общее для страниц: блоки, факты, заметки */

  function memSection(title, hint, body) {
    return el("section", { class: "mem-block" }, [el("h4", { text: title }), hint ? el("p", { class: "small", text: hint }) : null].concat(body));
  }

  function flagPills(flags) {
    return (flags || []).map(function (f) {
      var words = FLAG_WORDS[f.code];
      return el("span", { class: "badge warn", text: words ? words[0] : f.text });
    });
  }

  function flagNotes(p) {
    var nodes = (p.flags || []).map(function (f) {
      var words = FLAG_WORDS[f.code];
      return el("p", { class: "note info", text: words ? words[1] : f.text });
    });
    if (p.problem && !(p.flags || []).some(function (f) { return f.code === "frozen"; })) nodes.push(el("p", { class: "note info", text: FLAG_WORDS.frozen[1] }));
    return nodes;
  }

  function summarySection(p, hint) {
    return memSection("Сводка", hint, [p.summary.length ? el("ul", { class: "mem-lines" }, p.summary.map(function (s) {
      if (s.note) return el("li", { class: "mem-note", text: s.text });
      return el("li", {}, [
        s.disputed ? el("span", { class: "badge warn", text: "противоречие" }) : null,
        el("span", { text: s.text }),
        el("span", { class: "mem-meta", text: sourcesText(s) })
      ]);
    })) : el("p", { class: "mem-note", text: "Сводки пока нет." })]);
  }

  function commitmentsSection(p) {
    return memSection("Договорённости", "Закрыть или перенести срок — командой ассистенту или в боте согласований.",
      [p.commitments.length ? el("table", { class: "mem-table" }, [
        el("thead", {}, [el("tr", {}, [el("th", { scope: "col", text: "Что" }), el("th", { scope: "col", text: "Срок" }), el("th", { scope: "col", text: "Статус" })])]),
        el("tbody", {}, p.commitments.map(function (c) {
          return el("tr", {}, [el("td", { "data-label": "Что", text: c.what }), el("td", { "data-label": "Срок", text: c.due }),
                               el("td", { "data-label": "Статус", text: c.status })]);
        }))
      ]) : el("p", { class: "mem-note", text: "Договорённостей нет." })]);
  }

  function timelineSection(p) {
    return memSection("Хронология", "Что и когда происходило. Строки только дописываются, старые не меняются.",
      [p.timeline.length ? el("ol", { class: "mem-timeline" }, p.timeline.slice().reverse().map(function (t) {
        return el("li", {}, [
          t.day ? el("time", { datetime: t.day, text: day(t.day) }) : null,
          el("span", { text: t.text }),
          el("span", { class: "mem-meta", text: sourcesText(t) })
        ]);
      })) : el("p", { class: "mem-note", text: "Пока пусто." })]);
  }

  /* Факты и решения с кнопкой «Неверно»: факт перестаёт действовать, а если он сменил прежний —
   * прежний снова действует. after — что сделать после (обычно перечитать раздел). */
  function factsSection(title, hint, items, empty, after) {
    var body = items.length ? el("ul", { class: "mem-facts" }, items.map(function (f) {
      var error = el("p", { class: "note warn mem-error", role: "alert", hidden: true });
      var meta = [f.since ? (f.kind === "decision" ? day(f.since) : "с " + day(f.since)) : "", ORIGIN_WORDS[f.origin] || f.origin || ""]
        .filter(Boolean).join(" · ");
      return el("li", { class: "mem-fact" }, [
        el("div", { class: "mem-fact-main" }, [
          el("span", {}, [f.slot ? el("b", { text: f.slot + ": " }) : null, f.text]),
          meta ? el("span", { class: "mem-meta", text: meta }) : null,
          error
        ]),
        el("button", { class: "btn ghost danger small-btn", type: "button", text: "Неверно",
          "aria-label": "Неверно: " + (f.slot ? f.slot + ": " : "") + f.text,
          onclick: function () {
            if (!confirm("Отметить как неверное?\n\nАссистент перестанет на это опираться. Если это сменило прежнее, прежнее снова будет действовать.")) return;
            var button = this;
            busy(button, true);
            error.hidden = true;
            call("POST", "memory/facts/" + f.id + "/retract", {}).then(function (r) {
              if (!r.ok) { busy(button, false); error.textContent = r.error; error.hidden = false; return; }
              toast("Отмечено как неверное.");
              refreshOverview();
              after();
            });
          } })
      ]);
    })) : el("p", { class: "mem-note", text: empty });
    return memSection(title, hint, [body]);
  }

  /* Поле заметок владельца: заметки о человеке и проекте, правила в профиле. */
  function ownerBlock(o) {
    var id = "mem-owner";
    var area = el("textarea", { id: id, rows: "6", maxlength: String(MEM_LIMIT), spellcheck: "true",
                                "aria-describedby": "mem-owner-hint mem-owner-count mem-owner-note", placeholder: o.placeholder });
    area.value = o.value || "";
    area.disabled = !o.editable;
    var count = el("p", { class: "hint", id: "mem-owner-count", role: "status" });
    var save = el("button", { class: "btn", id: "mem-owner-save", type: "submit", text: "Сохранить" });
    var saved = o.value || "";
    function counted() {
      text(count, "Знаков: " + number(area.value.length) + " из " + number(MEM_LIMIT));
      save.disabled = !o.editable || area.value === saved;
    }
    area.addEventListener("input", counted);
    counted();
    var form = el("form", { class: "mem-owner", id: "mem-owner-form", autocomplete: "off", novalidate: true }, [
      el("label", { for: id, text: o.label }), area, count,
      el("p", { class: "note", id: "mem-owner-note", role: "status", hidden: true }),
      el("div", { class: "row" }, [save])
    ]);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (save.disabled) return;
      var value = area.value;
      busy(save, true);
      note("mem-owner-note", "", "");
      call("PUT", o.path, { text: value }).then(function (r) {
        if (!r.ok) { note("mem-owner-note", "warn", r.error); busy(save, false); return; }
        saved = value.replace(/^\n+|\n+$/g, "");
        if (o.saved) o.saved(saved);
        counted();
        note("mem-owner-note", "ok", r.data.changed ? "Сохранено. Ассистент учтёт это в следующем ответе." : "Без изменений: такой текст уже сохранён.");
        refreshOverview();
      });
    });
    return memSection(o.title, null, [
      el("p", { class: "gives", id: "mem-owner-hint" }, [el("b", { text: o.lead + " " }), o.hint]), form
    ]);
  }

  function personNotes(p, path, saved, placeholder) {
    return ownerBlock({
      title: "Ваши заметки", label: "Текст заметок", value: p.owner, editable: p.editable, path: path, saved: saved,
      lead: "Этот блок пишете только вы.",
      hint: "Ассистент его не меняет и читает как ваши собственные слова — доверяет ему больше, чем переписке.",
      placeholder: placeholder || "Например: не писать после 19:00; решения по деньгам — только через меня."
    });
  }

  /* ---- Люди */

  function loadPeople() {
    if (mem.personId !== null) {
      return call("GET", "memory/pages/" + mem.personId).then(function (r) {
        if (r.ok) mem.person = r.data; else { mem.personId = null; mem.person = null; }
        return r.ok ? "" : r.error;
      });
    }
    return call("GET", "memory/pages" + (mem.q ? "?q=" + encodeURIComponent(mem.q) : "")).then(function (r) {
      if (r.ok) mem.pages = r.data;
      return r.ok ? "" : r.error;
    });
  }

  function drawPeople() {
    if (mem.personId !== null && mem.person) return drawPerson(mem.person);
    var input = el("input", { id: "mem-q", type: "search", autocomplete: "off", value: mem.q,
                              placeholder: "Имя или слово со страницы" });
    input.addEventListener("input", function () {
      clearTimeout(mem.timer);
      mem.timer = setTimeout(function () {
        mem.q = input.value.trim().slice(0, 200);
        var seq = ++mem.seq;
        loadPeople().then(function (error) {
          if (seq !== mem.seq) return;
          var list = $("mem-list");
          if (!list) return;
          list.replaceWith(error ? el("p", { id: "mem-list", class: "note warn", text: error }) : peopleList());
        });
      }, 300);
    });
    return [
      el("div", { class: "field search mem-search" }, [el("label", { for: "mem-q", text: "Найти человека или слово на страницах" }), input]),
      peopleList()
    ];
  }

  function peopleList() {
    var items = (mem.pages && mem.pages.pages) || [];
    if (!items.length) {
      return el("p", { id: "mem-list", class: "note", text: mem.q
        ? "Ничего не нашлось. Попробуйте другое слово или часть имени."
        : "Страниц пока нет. Они появятся после ночной обработки переписки: ассистент предложит завести страницы о людях, с кем вы больше всего переписываетесь, — в боте согласований и во вкладке «Ждут решения»." });
    }
    return el("ul", { id: "mem-list", class: "mem-list" }, items.map(function (p) {
      var meta = [p.updated ? "обновлена " + day(p.updated) : "ещё не записана"];
      if (p.match_text && p.match !== "head") meta.push("найдено в " + p.match_text);
      return el("li", {}, [memRow(p.title, meta.join(" · "), p.snippet, p.flags,
        function () { mem.personId = p.person_id; mem.person = null; openAndFocus(); })]);
    }));
  }

  function memRow(title, meta, extra, flags, open) {
    return el("button", { class: "mem-row", type: "button", onclick: open }, [
      el("span", { class: "mem-row-main" }, [
        el("span", { class: "mem-name", text: title }),
        meta ? el("span", { class: "mem-meta", text: meta }) : null,
        extra ? el("span", { class: "mem-snippet", text: extra }) : null
      ]),
      el("span", { class: "mem-flags" }, flagPills(flags)),
      el("span", { class: "arrow", "aria-hidden": "true", text: "›" })
    ]);
  }

  function drawPerson(p) {
    var nodes = [
      el("button", { class: "link mem-back", type: "button", text: "← Все люди",
                     onclick: function () { mem.personId = null; mem.person = null; showMemory(true); } }),
      el("div", { class: "mem-title" }, [
        el("h3", { text: p.title }),
        p.aliases && p.aliases.length ? el("p", { class: "small", text: "Ещё называют: " + p.aliases.join(", ") }) : null,
        el("p", { class: "small", text: p.updated ? "Страница обновлена " + day(p.updated) : "Страница ещё не записана." })
      ])
    ].concat(flagNotes(p));
    nodes.push(summarySection(p, "Пишет ассистент по переписке и пересобирает каждую ночь. Под каждой строкой — откуда она: из скольких сообщений и кто это сказал."));
    nodes.push(factsSection("Факты", "Что ассистент знает о человеке из переписки: должность, компания, телефон и подобное. Когда факт меняется, прежний уходит в хронологию.",
      p.facts || [], "Фактов пока нет.", function () { showMemory(true); }));
    nodes.push(personNotes(p, "memory/pages/" + p.person_id + "/owner-block", function (v) { p.owner = v; }));
    nodes.push(commitmentsSection(p));
    nodes.push(timelineSection(p));
    return nodes;
  }

  /* ---- Проекты */

  function loadMemoryChats() {
    if (mem.chats) return Promise.resolve("");
    return call("GET", "memory/chats").then(function (r) {
      if (r.ok) mem.chats = r.data.chats;
      return r.ok ? "" : r.error;
    });
  }

  function loadProjects() {
    if (mem.creating) return loadMemoryChats();
    if (mem.projectId !== null) {
      return Promise.all([call("GET", "memory/projects/" + mem.projectId), loadMemoryChats()]).then(function (all) {
        var r = all[0];
        if (r.ok) mem.project = r.data; else { mem.projectId = null; mem.project = null; }
        return r.ok ? all[1] : r.error;
      });
    }
    return call("GET", "memory/projects").then(function (r) {
      if (r.ok) mem.projects = r.data.projects;
      return r.ok ? "" : r.error;
    });
  }

  function projectMeta(p) {
    var parts = [];
    parts.push(p.chats.length ? p.chats.length + " " + plural(p.chats.length, "чат", "чата", "чатов") : "без чатов");
    if (p.commitments_open) parts.push("открытых договорённостей: " + p.commitments_open);
    if (p.decisions) parts.push("решений: " + p.decisions);
    parts.push(p.updated ? "обновлена " + day(p.updated) : "страница ещё не записана");
    return parts.join(" · ");
  }

  function drawProjects() {
    if (mem.creating) return drawCreateProject();
    if (mem.projectId !== null && mem.project) return drawProject(mem.project);
    var items = mem.projects || [];
    var active = items.filter(function (p) { return p.status === "active"; });
    var archived = items.filter(function (p) { return p.status === "archived"; });
    var row = function (p) {
      return el("li", {}, [memRow(p.title, projectMeta(p), p.chats.length ? "Чаты: " + p.chats.join(", ") : "", [],
        function () { mem.projectId = p.id; mem.project = null; openAndFocus(); })]);
    };
    var nodes = [
      el("p", { class: "small", text: "Проект — объект, сделка или направление работы. У проекта своя страница: сводка по его чатам, решения, факты и договорённости." }),
      el("div", { class: "row" }, [el("button", { class: "btn", id: "mem-project-new", type: "button", text: "Завести проект",
        onclick: function () { mem.creating = true; openAndFocus(); } })])
    ];
    nodes.push(active.length ? el("ul", { class: "mem-list", id: "mem-projects" }, active.map(row))
      : el("p", { class: "note", text: archived.length ? "Действующих проектов нет." : "Проектов пока нет. Заведите проект сами или дождитесь предложения ассистента — оно придёт в бот согласований и во вкладку «Ждут решения»." }));
    if (archived.length) {
      nodes.push(el("details", { class: "sub mem-archive" }, [
        el("summary", { text: "В архиве: " + archived.length }),
        el("ul", { class: "mem-list" }, archived.map(row))
      ]));
    }
    return nodes;
  }

  /* Выбор чатов: флажки со строкой поиска. chosen — номера уже выбранных. */
  function chatPicker(chosen, idPrefix) {
    var all = mem.chats || [];
    var picked = {};
    chosen.forEach(function (id) { picked[id] = true; });
    var list = el("ul", { class: "mem-chats", id: idPrefix + "-list" });
    var filter = el("input", { id: idPrefix + "-q", type: "search", autocomplete: "off", placeholder: "Начните вводить название" });
    function draw() {
      var q = filter.value.trim().toLowerCase();
      list.textContent = "";
      var shown = all.filter(function (c) { return !q || c.title.toLowerCase().indexOf(q) >= 0 || picked[c.id]; }).slice(0, 60);
      if (!shown.length) list.appendChild(el("li", { class: "mem-note", text: all.length ? "Ничего не нашлось." : "Сервис ещё не читает ни одного чата." }));
      shown.forEach(function (c) {
        var box = el("input", { type: "checkbox", value: String(c.id) });
        box.checked = !!picked[c.id];
        box.addEventListener("change", function () { if (box.checked) picked[c.id] = true; else delete picked[c.id]; });
        list.appendChild(el("li", {}, [el("label", { class: "check" }, [box,
          el("span", {}, [c.title, el("span", { class: "mem-meta", text: " · " + (CHAT_KINDS[c.kind] || "") })])])]));
      });
    }
    filter.addEventListener("input", draw);
    draw();
    return {
      nodes: [el("div", { class: "field" }, [el("label", { for: idPrefix + "-q", text: "Найти чат" }), filter]), list],
      value: function () { return all.filter(function (c) { return picked[c.id]; }).map(function (c) { return c.id; }); }
    };
  }

  function drawCreateProject() {
    var title = el("input", { id: "mem-new-title", type: "text", autocomplete: "off", maxlength: "80", placeholder: "Например: ЖК «Береговой», корпус 2" });
    var aliases = el("input", { id: "mem-new-aliases", type: "text", autocomplete: "off", placeholder: "Береговой, Берег-2" });
    var picker = chatPicker([], "mem-new-chats");
    var error = el("p", { class: "note warn", id: "mem-new-error", role: "alert", hidden: true });
    var save = el("button", { class: "btn", id: "mem-new-save", type: "submit", text: "Завести проект" });
    var form = el("form", { class: "mem-form", autocomplete: "off", novalidate: true }, [
      el("div", { class: "field" }, [el("label", { for: "mem-new-title", text: "Название" }), title]),
      el("div", { class: "field" }, [el("label", { for: "mem-new-aliases", text: "Как ещё его называют — по желанию" }), aliases,
        el("p", { class: "hint", text: "Через запятую. По этим словам ассистент узнаёт проект в переписке." })]),
      el("fieldset", { class: "mem-fieldset" }, [el("legend", { text: "Чаты проекта — по желанию" }),
        el("p", { class: "hint", text: "Сводка проекта собирается по сообщениям этих чатов, а договорённости из них относятся к проекту. Здесь только чаты, которые сервис уже читает." })]
        .concat(picker.nodes)),
      error,
      el("div", { class: "row" }, [save, el("button", { class: "btn ghost", type: "button", text: "Отмена",
        onclick: function () { mem.creating = false; showMemory(true); } })])
    ]);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var names = aliases.value.split(",").map(function (a) { return a.trim(); }).filter(Boolean);
      busy(save, true);
      error.hidden = true;
      call("POST", "memory/projects", { title: title.value, chat_ids: picker.value(), aliases: names }).then(function (r) {
        busy(save, false);
        if (!r.ok) { error.textContent = r.error; error.hidden = false; return; }
        toast("Проект заведён.");
        mem.creating = false;
        mem.projectId = r.data.project.id;
        refreshOverview();
        openAndFocus();
      });
    });
    return [
      el("button", { class: "link mem-back", type: "button", text: "← Все проекты",
                     onclick: function () { mem.creating = false; showMemory(true); } }),
      el("h3", { class: "mem-form-title", text: "Новый проект" }),
      form
    ];
  }

  function projectChats(p) {
    var error = el("p", { class: "note warn mem-error", role: "alert", hidden: true });
    function change(ids, done, button) {
      busy(button, true);
      error.hidden = true;
      call("PUT", "memory/projects/" + p.id + "/chats", { chat_ids: ids }).then(function (r) {
        busy(button, false);
        if (!r.ok) { error.textContent = r.error; error.hidden = false; return; }
        toast(done);
        refreshOverview();
        showMemory(true);
      });
    }
    var current = p.chats.map(function (c) { return c.id; });
    var list = p.chats.length ? el("ul", { class: "mem-items", id: "mem-project-chats" }, p.chats.map(function (c) {
      return el("li", { class: "mem-chat" }, [
        el("span", {}, [c.title]),
        el("button", { class: "btn ghost small-btn", type: "button", text: "Убрать", "aria-label": "Убрать чат " + c.title,
          onclick: function () { change(current.filter(function (id) { return id !== c.id; }), "Чат убран из проекта.", this); } })
      ]);
    })) : el("p", { class: "mem-note", text: "Чатов нет: сводка собирается по упоминаниям проекта в переписке." });
    var free = (mem.chats || []).filter(function (c) { return current.indexOf(c.id) < 0; });
    var select = el("select", { id: "mem-add-chat" }, [el("option", { value: "", text: "Выберите чат" })].concat(free.map(function (c) {
      return el("option", { value: String(c.id), text: c.title + " · " + (CHAT_KINDS[c.kind] || "") });
    })));
    var add = el("button", { class: "btn ghost", id: "mem-add-chat-go", type: "button", text: "Добавить",
      onclick: function () {
        var id = Number(select.value);
        if (!id) { error.textContent = "Выберите чат из списка."; error.hidden = false; return; }
        change(current.concat([id]), "Чат добавлен в проект.", this);
      } });
    return memSection("Чаты проекта", "По сообщениям этих чатов ассистент собирает сводку и договорённости проекта.", [
      list,
      free.length ? el("div", { class: "mem-add" }, [el("div", { class: "field grow" }, [el("label", { for: "mem-add-chat", text: "Добавить чат" }), select]), add]) : null,
      error
    ]);
  }

  function drawProject(p) {
    var archived = p.status === "archived";
    var nodes = [
      el("button", { class: "link mem-back", type: "button", text: "← Все проекты",
                     onclick: function () { mem.projectId = null; mem.project = null; showMemory(true); } }),
      el("div", { class: "mem-title" }, [
        el("h3", {}, [p.title, archived ? el("span", { class: "badge", text: "в архиве" }) : null]),
        p.aliases.length ? el("p", { class: "small", text: "Ещё называют: " + p.aliases.join(", ") }) : null,
        p.participants.length ? el("p", { class: "small", text: "Участники: " + p.participants.join(", ") }) : null,
        el("p", { class: "small", text: p.updated ? "Страница обновлена " + day(p.updated) : "Страница ещё не записана: появится после ближайшей сборки." })
      ])
    ].concat(flagNotes(p));
    if (archived) nodes.push(el("p", { class: "note info", text: "Проект в архиве: страница сохранена, но сводка больше не обновляется, а новые договорённости к нему не относятся." }));
    nodes.push(summarySection(p, "Пишет ассистент по сообщениям чатов проекта и упоминаниям, пересобирает каждую ночь."));
    nodes.push(personNotes(p, "memory/projects/" + p.id + "/owner-block", function (v) { p.owner = v; },
      "Например: главный по проекту — Иван; бюджет не превышать без моего согласия."));
    nodes.push(factsSection("Решения", "Что решили по проекту и когда — из переписки.", p.decisions, "Решений пока нет.", function () { showMemory(true); }));
    nodes.push(factsSection("Факты", "Цены, сроки, бюджет, статус и подобное. Когда факт меняется, прежний уходит в хронологию.", p.facts, "Фактов пока нет.", function () { showMemory(true); }));
    nodes.push(commitmentsSection(p));
    nodes.push(timelineSection(p));
    nodes.push(projectChats(p));
    if (!archived) {
      var error = el("p", { class: "note warn mem-error", role: "alert", hidden: true });
      nodes.push(memSection("Проект закончился?", "В архиве страница останется, но перестанет обновляться.", [
        el("div", { class: "row" }, [el("button", { class: "btn ghost danger", id: "mem-archive", type: "button", text: "В архив",
          onclick: function () {
            if (!confirm("Перенести проект «" + p.title + "» в архив?\n\nСтраница останется, но сводка перестанет обновляться, а новые договорённости из его чатов не будут к нему относиться.")) return;
            var button = this;
            busy(button, true);
            call("POST", "memory/projects/" + p.id + "/archive", {}).then(function (r) {
              busy(button, false);
              if (!r.ok) { error.textContent = r.error; error.hidden = false; return; }
              toast("Проект перенесён в архив.");
              refreshOverview();
              mem.projectId = null;
              mem.project = null;
              showMemory(true);
            });
          } })]),
        error
      ]));
    }
    return nodes;
  }

  /* ---- Профиль */

  function loadProfile() {
    return call("GET", "memory/profile").then(function (r) {
      if (r.ok) mem.profile = r.data;
      return r.ok ? "" : r.error;
    });
  }

  function drawProfile() {
    var p = mem.profile || { facts: [], owner: "", editable: true };
    return [
      el("p", { text: "Что ассистент знает о вас и каким правилам следует. Факты о вас появляются здесь только после вашего «✓» — в боте согласований или во вкладке «Ждут решения»." }),
      ownerBlock({
        title: "Ваши правила и указания", label: "Текст правил", value: p.owner, editable: p.editable,
        path: "memory/profile/owner-block", saved: function (v) { p.owner = v; },
        lead: "Ассистент следует этим правилам и никогда их не меняет.",
        hint: "Пишите обычным текстом: как к вам обращаться, когда не беспокоить, что решаете только вы.",
        placeholder: "Например: отвечать коротко; по выходным не беспокоить, кроме срочного по стройке; платежи — только с моего «да»."
      }),
      factsSection("Что ассистент знает о вас", "Одобренные вами факты: должность, компания, город и подобное.",
        p.facts, "Пока ничего. Когда ассистент заметит в переписке что-то о вас, он спросит — в боте согласований и во вкладке «Ждут решения».",
        function () { showMemory(true); })
    ];
  }

  /* ---- Ждут решения */

  function loadPending() {
    return call("GET", "memory/pending").then(function (r) {
      if (r.ok) { mem.pending = r.data; memPill(); }
      return r.ok ? "" : r.error;
    });
  }

  function decide(button, item, path, body, done) {
    var buttons = item.querySelectorAll("button"), error = item.querySelector(".mem-error");
    Array.prototype.forEach.call(buttons, function (b) { b.disabled = true; });
    error.hidden = true;
    call("POST", path, body).then(function (r) {
      if (!r.ok) {
        Array.prototype.forEach.call(buttons, function (b) { b.disabled = false; });
        error.textContent = r.error;
        error.hidden = false;
        if (r.status === 404 || r.status === 409) loadPending().then(function () { drawMemoryTabs(); });
        return;
      }
      toast(done);
      mem.pages = mem.projects = mem.profile = null;     // списки могли измениться
      refreshOverview();
      loadPending().then(function () { if (mem.tab === "pending") showMemory(false); });
    });
  }

  function pendingItem(head, lines, yes, no) {
    var item = el("li", { class: "mem-item" }, [
      el("div", { class: "mem-item-main" }, [el("b", { text: head })].concat(lines)),
      el("p", { class: "note warn mem-error", role: "alert", hidden: true }),
      el("div", { class: "row" }, [
        el("button", { class: "btn small-btn", type: "button", text: yes[0], onclick: function () { yes[1](this, item); } }),
        el("button", { class: "btn ghost small-btn", type: "button", text: no[0], onclick: function () { no[1](this, item); } })
      ])
    ]);
    return item;
  }

  function drawPending() {
    var d = mem.pending || {};
    var groups = { projects: d.projects || [], owner_facts: d.owner_facts || [], pages: d.pages || [], commitments: d.commitments || [] };
    var nodes = [el("p", { class: "small", text: "То же, что бот согласований присылает с кнопками ✓ и ✗. Решение здесь действует сразу — как нажатие в боте." })];
    if (!d.total) {
      nodes.push(el("p", { class: "note ok", text: "Сейчас ничего не ждёт вашего решения." }));
      return nodes;
    }
    if (groups.projects.length) {
      nodes.push(memSection("Завести проект?", "Ассистент заметил в переписке объект или сделку. У проекта будет своя страница: сводка, решения, договорённости.",
        [el("ul", { class: "mem-items" }, groups.projects.map(function (p) {
          var path = "memory/pending/projects/" + p.id;
          return pendingItem(p.title, [
            el("span", { class: "mem-meta", text: "Почему предложено: " + p.reason }),
            p.chats.length ? el("span", { class: "mem-meta", text: "Чаты: " + p.chats.join(", ") }) : null
          ],
            ["✓ Завести проект", function (b, item) { decide(b, item, path, { accept: true }, "Проект заведён."); }],
            ["✗ Не нужно", function (b, item) { decide(b, item, path, { accept: false }, "Не заводим."); }]);
        }))]));
    }
    if (groups.owner_facts.length) {
      nodes.push(memSection("Факты о вас", "Верно — ассистент запомнит это в вашем профиле. Неверно — забудет и больше не спросит об этом сообщении.",
        [el("ul", { class: "mem-items" }, groups.owner_facts.map(function (f) {
          var path = "memory/pending/owner-facts/" + f.id;
          return pendingItem(f.slot ? f.slot + ": " + f.text : f.text, [
            el("span", { class: "mem-meta", text: "с " + day(f.since) }),
            f.quote ? el("q", { class: "mem-quote", text: f.quote }) : null
          ],
            ["✓ Верно", function (b, item) { decide(b, item, path, { accept: true, fingerprint: f.fingerprint }, "Запомнено в профиле."); }],
            ["✗ Неверно", function (b, item) { decide(b, item, path, { accept: false }, "Не запоминаем."); }]);
        }))]));
    }
    if (groups.pages.length) {
      nodes.push(memSection("Завести страницу о человеке?", "Ассистент будет вести о нём сводку по переписке и опираться на неё в ответах.",
        [el("ul", { class: "mem-items" }, groups.pages.map(function (p) {
          var path = "memory/pending/pages/" + p.person_id;
          return pendingItem(p.name, [el("span", { class: "mem-meta", text: "Почему предложено: " + p.reason })],
            ["✓ Завести страницу", function (b, item) { decide(b, item, path, { accept: true }, "Страница будет заведена."); }],
            ["✗ Не нужно", function (b, item) { decide(b, item, path, { accept: false }, "Не заводим."); }]);
        }))]));
    }
    if (groups.commitments.length) {
      nodes.push(memSection("Новые договорённости из переписки", "Верно — ассистент запишет договорённость и будет напоминать о ней. Неверно — забудет.",
        [el("ul", { class: "mem-items" }, groups.commitments.map(function (c) {
          var path = "memory/pending/commitments/" + c.id;
          return pendingItem(c.who, [
            el("span", { text: c.what }),
            el("span", { class: "mem-meta", text: c.due }),
            c.quote ? el("q", { class: "mem-quote", text: c.quote }) : null
          ],
            ["✓ Верно", function (b, item) { decide(b, item, path, { accept: true, fingerprint: c.fingerprint }, "Договорённость записана."); }],
            ["✗ Неверно", function (b, item) { decide(b, item, path, { accept: false }, "Отклонено."); }]);
        }))]));
    }
    return nodes;
  }

  function wireMemory() {
    $("memory-details").addEventListener("toggle", function () {
      if (memOpen()) showMemory(true);
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

  wireLogin(); wireMode(); wireBot(); wireKeys(); wireAccounts(); wireChats(); wireImport(); wireBusiness(); wireLlm(); wireMedia(); wireMemory(); wireSession();

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
