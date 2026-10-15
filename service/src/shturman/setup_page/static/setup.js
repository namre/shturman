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
    managedLink: "", managedClaim: "", managedDone: "", managedError: false,
    login: null, loginLink: "", chatAccount: null, chats: [], chatsTotal: 0, chatsEnabled: 0,
    chatsLoading: false, chatsKey: "", chatsPoll: null, chatsTick: null, chatsLoad: null, chatsListLoaded: false,
    chatsAge: null, chatsBusy: 0, scans: {}, scanning: {}, exclude: {}, upload: null,
    signatures: {}, fastUntil: 0, lastState: 0, lastOverview: 0, optionsFor: null,
    messages: null, importOpened: false,
    // раскладки списка чатов и сферы (Р-82): раскладка — только в памяти страницы
    chatsLayout: "", sections: [], sectionOpen: {}, sectionItems: {}, sectionBusy: {},
    sphereList: [], sph: null, sphOpen: {}, sphEdit: null,
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

  /* Подтверждение в строке вместо системного окна confirm() (С6, Р-86): вопрос и кнопки появляются
   * прямо под тем, что нажали. На телефоне системное окно выглядит как сбой браузера, а «ОК» жмут
   * не читая. Возвращает Promise: true — главная кнопка, false — «Отмена» (или открыт другой вопрос),
   * либо value дополнительной кнопки. options: {note, yes, danger (главная — красная), extra: [{text, value,
   * danger}], no}. Главная кнопка помечена data-ask="yes", отмена — data-ask="no". */
  var openAsk = null;
  function ask(anchor, question, options) {
    options = options || {};
    if (openAsk) openAsk(false);
    return new Promise(function (resolve) {
      var holder = anchor && anchor.closest ? (anchor.closest("li, .row, .actions, .field, label, p") || anchor) : anchor;
      var panel = el("div", { class: "ask", role: "group", "aria-label": question });
      function done(value) {
        if (panel.parentNode) panel.parentNode.removeChild(panel);
        if (openAsk === done) openAsk = null;
        resolve(value);
      }
      var buttons = [el("button", { class: "btn small-btn" + (options.danger ? " danger-fill" : ""), type: "button",
                                    "data-ask": "yes", text: options.yes || "Да", onclick: function () { done(true); } })];
      (options.extra || []).forEach(function (b) {
        buttons.push(el("button", { class: "btn small-btn " + (b.danger ? "danger-fill" : "ghost"), type: "button",
                                    "data-ask": b.value, text: b.text, onclick: function () { done(b.value); } }));
      });
      buttons.push(el("button", { class: "btn ghost small-btn", type: "button", "data-ask": "no",
                                  text: options.no || "Отмена", onclick: function () { done(false); } }));
      panel.appendChild(el("p", { text: question }));
      if (options.note) panel.appendChild(el("p", { class: "ask-note", text: options.note }));
      panel.appendChild(el("div", { class: "ask-buttons" }, buttons));
      if (holder && holder.parentNode) {
        if (holder.tagName === "LI") holder.appendChild(panel);
        else holder.parentNode.insertBefore(panel, holder.nextSibling);
      } else {
        document.body.appendChild(panel);
      }
      openAsk = done;
      // Фокус — на безопасной кнопке: у опасного вопроса это «Отмена».
      (options.danger ? buttons[buttons.length - 1] : buttons[0]).focus();
    });
  }

  /* Часовой пояс словами (С5, С16): «по московскому времени», а не «Europe/Moscow». Незнакомый пояс —
   * смещением от UTC, как его покажет браузер; без пояса — «по часам сервера». */
  var TZ_WORDS = {
    "Europe/Moscow": "по московскому времени", "Europe/Astrakhan": "по астраханскому времени",
    "Europe/Samara": "по самарскому времени", "Europe/Volgograd": "по волгоградскому времени",
    "Europe/Saratov": "по саратовскому времени", "Europe/Ulyanovsk": "по ульяновскому времени",
    "Europe/Kaliningrad": "по калининградскому времени", "Asia/Yekaterinburg": "по екатеринбургскому времени",
    "Asia/Omsk": "по омскому времени", "Asia/Novosibirsk": "по новосибирскому времени",
    "Asia/Krasnoyarsk": "по красноярскому времени", "Asia/Irkutsk": "по иркутскому времени",
    "Asia/Yakutsk": "по якутскому времени", "Asia/Vladivostok": "по владивостокскому времени",
    "Asia/Magadan": "по магаданскому времени", "Asia/Kamchatka": "по камчатскому времени",
    "UTC": "по всемирному времени (UTC)", "Etc/UTC": "по всемирному времени (UTC)"
  };
  function tzWords(tz) {
    if (!tz) return "по часам сервера";
    if (TZ_WORDS[tz]) return TZ_WORDS[tz];
    try {
      var parts = new Intl.DateTimeFormat("ru-RU", { timeZone: tz, timeZoneName: "shortOffset" }).formatToParts(new Date());
      var name = parts.filter(function (p) { return p.type === "timeZoneName"; })[0];
      if (name && name.value) return "по времени " + name.value.replace("GMT", "UTC");
    } catch (e) { /* пояс браузеру незнаком */ }
    return "по часам сервера";
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
    // Cookie браузер не шлёт и не принимает — кроме маршрутов входа по коду: там cookie попытки (Р-88)
    // привязывает код к этому браузеру. Её путь — только эти маршруты, а скрипту она не видна (HttpOnly).
    var withAttempt = path.indexOf("login/code") === 0;
    var options = { method: method, credentials: withAttempt ? "same-origin" : "omit", cache: "no-store", redirect: "error",
                    referrerPolicy: "no-referrer", headers: headers(body !== undefined) };
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
    wrong: "Код не подошёл. Проверьте цифры в сообщении бота.",
    expired: "Код устарел — он действует 5 минут. Запросите новый.",
    none: "Этот код здесь не действует: он погашен, уже использован или запрошен в другом браузере. Запросите новый в этом браузере.",
    locked: "Слишком много неверных попыток: вход по коду на время приостановлен. Попробуйте позже или войдите по ссылке.",
    limited: "Больше трёх кодов за 15 минут запросить нельзя. Возьмите последний код из бота или подождите.",
    port: "Здесь вход по коду недоступен. Войдите по одноразовой ссылке — попросите её у того, кто обслуживает ваш сервер."
  };
  /* Почему вход по коду выключен (session.code_login_off) — строка на экране «Нужна ссылка входа». */
  var CODE_OFF = {
    managed: "Вход по коду из бота выключен: бот согласований создан «в одно нажатие» через посредника, и его держатель " +
             "технически может читать сообщения бота — а значит и коды. Включить можно после входа по ссылке: «Бот согласований» → " +
             "«Вход на эту страницу по коду из бота», подтверждение — кнопкой в боте.",
    off: "Вход по коду из бота выключен вами. Включить — после входа по ссылке: «Бот согласований» → «Вход на эту страницу по коду из бота».",
    port: "Здесь вход по коду недоступен: страница открыта не по своему внешнему адресу (например, через туннель SSH). Войдите по ссылке."
  };

  function waitWords(seconds) {
    seconds = Number(seconds) || 0;
    if (seconds < 90) return "через минуту";
    var minutes = Math.ceil(seconds / 60);
    if (minutes < 90) return "через " + minutes + " мин";
    return "через " + Math.ceil(minutes / 60) + " ч";
  }

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
      var off = r.ok ? CODE_OFF[r.data.code_login_off] : "";
      note("login-code-off", "info", off || "");
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
        loginMessage((r.error || GENERIC[500]) + " Если вы открываете эту ссылку впервые, попросите того, кто обслуживает " +
          "ваш сервер, завершить все входы и выдать новую ссылку.", false);
      });
    });

    function requestCode(button) {
      busy(button, true);
      call("POST", "login/code/request", {}, true).then(function (r) {
        busy(button, false);
        var result = r.data && (r.data.result || r.data.code) || "";
        if (r.ok && result === "sent") {
          loginView("login-code");
          var left = r.data.left;
          text("login-code-lead", "Бот согласований отправил вам код в Telegram. Он действует 5 минут и только в этом браузере." +
            (left === 0 ? " Это последний код на ближайшие 15 минут." : ""));
          error.hidden = true;
          input.value = "";
          input.focus();
          return;
        }
        var reason = CODE_REASONS[result] || r.error || GENERIC[0];
        if ((result === "locked" || result === "limited") && r.data.retry_after) reason += " Снова — " + waitWords(r.data.retry_after) + ".";
        loginMessage(reason, result !== "port");
      });
    }
    $("login-code-send").addEventListener("click", function () { requestCode(this); });
    $("login-code-resend").addEventListener("click", function () { requestCode(this); });
    $("login-message-retry").addEventListener("click", function () { loginScreen(""); });

    input.addEventListener("input", function () {
      var digits = input.value.replace(/\D/g, "").slice(0, 6);
      input.value = digits.length > 3 ? digits.slice(0, 3) + " " + digits.slice(3) : digits;
      error.hidden = true;
    });
    $("login-code-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var digits = input.value.replace(/\D/g, ""), button = $("login-code-submit");
      if (digits.length !== 6) { error.textContent = "В коде шесть цифр."; error.hidden = false; return; }
      busy(button, true);
      call("POST", "login/code", { code: digits }, true).then(function (r) {
        busy(button, false);
        input.value = "";
        if (r.ok && r.data.key) { saveKey(r.data.key); startApp(); return; }
        var reason = r.data.code;
        if (reason === "locked" || reason === "none" || reason === "expired" || reason === "port") {
          loginMessage(CODE_REASONS[reason] + (reason === "locked" && r.data.retry_after ? " Снова — " + waitWords(r.data.retry_after) + "." : ""),
                       reason !== "locked" && reason !== "port");
          return;
        }
        error.textContent = (CODE_REASONS[reason] || r.error) +
          (reason === "wrong" && r.data.left ? " Осталось попыток: " + r.data.left + "." : "");
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
    refreshSpheres();
    if ($("spheres-details").open) showSpheres();
    if ($("notify-details").open) showNotify(); else refreshNotify();
    if ($("rehearsal-details").open) showRehearsal(); else refreshRehearsal();
    if ($("watch-details").open) showWatch(); else refreshWatch();
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
    refreshRetry();
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
    renderHalt(S.halt || {});
    renderCodeLogin(S.code_login || {}, S.bot);
    renderBusiness(S.bot);
    renderLlm(S.llm);
    renderMedia(S.media || {});
    renderProgress();
  }

  /* Шаги зависят от выбранного способа подключения:
   *   own   — «ассистент видит всё как вы»: вход в основной аккаунт, выбор чатов;
   *   staff — «ассистент — отдельный сотрудник»: бизнес-режим для личных чатов и
   *           аккаунт помощника для групп.
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
        ? (!f.helper ? "Откроется, когда будет подключён аккаунт помощника — шаг " + num("s-assistant") + "." : "")
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
      if (ok) state = id === "s-chats" ? "выбрано: " + f.chats : id === "s-mode" ? (f.sc === "own" ? "через ваш аккаунт" : "через бота и помощника") : "готово";
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

  // Названия способов — из единого словаря (Р-86); коды own и staff прежние.
  var MODE_NAMES = { own: "Через ваш аккаунт", staff: "Через бота и помощника" };

  function renderMode(sc) {
    var chosen = sc.chosen || null;
    document.body.classList.toggle("sc-own", chosen === "own");
    document.body.classList.toggle("sc-staff", chosen === "staff");
    pill("mode", chosen ? "Выбрано: " + MODE_NAMES[chosen].toLowerCase() : "Не выбран", chosen ? "done" : "");
    // На экземпляре, где что-то уже подключено, подсказываем способ — но выбирает владелец.
    note("mode-hint", "info", !chosen && sc.suggested ? "Судя по тому, что уже подключено, это способ «" +
      MODE_NAMES[sc.suggested] + "». Выберите его, чтобы страница показала оставшиеся шаги, — или другой." : "");
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
        var chosen = S && S.scenario && S.scenario.chosen, button = this;
        var asked = chosen && chosen !== name
          ? ask(button, "Сменить способ на «" + MODE_NAMES[name] + "»?", {
              yes: "Сменить способ",
              note: "Ничего не удаляется и не отключается: собранное остаётся в архиве, подключённое — подключённым. Изменится набор шагов на странице. То, что не нужно в новом способе, вы отключите сами." })
          : Promise.resolve(true);
        asked.then(function (ok) {
          if (!ok) return;
          act(button, "mode-error", "PUT", "scenario", { scenario: name }, function () {
            toast(name === "own" ? "Способ выбран. Дальше — шаги для входа в ваш аккаунт." : "Способ выбран. Дальше — бизнес-режим для личных чатов.");
          });
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
      var button = this;
      ask(button, "Включить разбор фото и документов?", {
        yes: "Включить разбор",
        note: "Фото, сканы и текст документов из переписки будут уходить модели — её провайдеру, как уже уходит текст сообщений. Сами файлы на сервере не хранятся: разобрав, сервис их удаляет."
      }).then(function (ok) {
        if (!ok) return;
        act(button, "media-error", "PUT", "media", { enabled: true }, function () {
          toast("Разбор включён. Свежие фото и документы разберутся в ближайшие минуты.");
        });
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
    show("bot-howto", editing && !ui.botToken);
    show("bot-form-cancel", ui.botEditing);
    show("bot-confirm", !!ui.botToken);
    show("bot-actions", b.configured && b.source === "page" && !ui.botEditing);
    show("shot-newbot", !b.configured || ui.botEditing);
    renderManaged(b, editing && !ui.botToken);
    if (b.configured && b.created_via === "managed") {
      note("bot-origin", "info", "Бот создан в одно нажатие через бот-менеджер посредника" + (b.manager ? " " + b.manager : "") +
           ". Его держатель технически может получить токен этого бота — а с ним входить в приложение «Штурман» вместо вас и менять, какие чаты читает ассистент. Чтобы ни от кого не зависеть, создайте бота у @BotFather и нажмите «Сменить токен»." +
           (b.restricted === false ? " Посредник не закрыл бота для посторонних: написать ему может любой, но отвечает он только вам." : ""));
    } else note("bot-origin", "", "");

    var canBind = b.configured && !!b.username && !ui.botEditing;
    show("bind", canBind);
    if (!canBind) { ui.bind = null; return; }
    if (ui.bind && b.owner_bound && b.owner_bound_at !== ui.bind.before) {
      ui.bind = null;                                  // ссылку открыли: владелец привязан
      toast("Владелец привязан" + (b.owner_name ? ": " + b.owner_name : "") + ".");
    }
    show("bind-none", !b.owner_bound);
    note("bind-creator", "info", !b.owner_bound && b.created_via === "managed"
      ? "Бот создан аккаунтом Telegram " + (b.creator_username ? "@" + b.creator_username : "") + (b.creator_name ? " «" + b.creator_name + "»" : "") + ". Владельцем можно привязать только этот аккаунт: откройте ссылку в нём." : "");
    note("bind-refused", "danger", !b.owner_bound && b.bind_refused_at
      ? "Ссылку привязки открыли в другом аккаунте Telegram, не в том, которым создан бот (" + when(b.bind_refused_at) + "). Все ссылки привязки отменены. Если это были не вы — ссылку могли подсмотреть; получите новую и откройте её только в своём аккаунте." : "");
    note("bind-done", "ok", b.owner_bound ? "Владелец привязан" + (b.owner_name ? ": " + b.owner_name : "") + "." : "");
    // С14: где решения дальше — приложение «Штурман» в чате с ботом (если у страницы есть внешний адрес https)
    show("bind-app", !!(b.owner_bound && S && S.app_available));
    show("bind-link-box", !!ui.bind);
    show("shot-start", !!ui.bind);
    show("bind-go", !ui.bind);
    text("bind-go", b.owner_bound ? "Привязать другой аккаунт" : "Показать ссылку привязки");
    if (b.bind_paused) note("bind-error", "warn", "Бот получил много неверных кодов привязки и на 10 минут перестал их принимать. Подождите и выдайте ссылку заново.");
  }

  /* «Остановить всё» (Р-87): кнопка в шапке — сразу; «Продолжить…» — карточка в бот согласований,
   * и только нажатие там снимает остановку. Пока остановлено — полоса под шапкой. */
  function renderHalt(h) {
    var stopped = !!h.stopped;
    show("halt-stop", !stopped);
    show("halt-banner", stopped);
    if (!stopped) { text("halt-note", ""); return; }
    text("halt-since", h.since ? "с " + when(h.since) + (h.via === "app" ? " (из приложения)" : h.via === "bot" ? " (в боте)" : "") : "");
    text("halt-reading", h.reading_paused
      ? "Чтение переписки тоже на паузе (аккаунтов: " + h.reading_paused + ")."
      : "Переписка по-прежнему собирается в архив, уведомления о новых сообщениях приходят.");
    show("halt-reading-stop", !h.reading_paused);
    show("halt-reading-box", !!h.reading_paused);
    var list = $("halt-resumes");
    list.textContent = "";
    (h.resumes || []).forEach(function (line) { list.appendChild(el("li", { text: line.replace(/^•\s*/, "") })); });
    if (h.resume_pending) {
      text("halt-note", "Подтверждение «Продолжить» ждёт в боте согласований — нажмите там «Да, сделать». Здесь продолжить нельзя: так ассистент не снимет остановку сам.");
      text("halt-resume", "Отправить подтверждение ещё раз");
    } else {
      text("halt-note", "Продолжить можно только нажатием в боте согласований: кнопка «Продолжить…» пришлёт туда подтверждение.");
      text("halt-resume", "Продолжить…");
    }
  }

  function wireHalt() {
    $("halt-stop").addEventListener("click", function () {
      var button = this;
      busy(button, true);
      call("POST", "halt", { reading: false }).then(function (r) {
        busy(button, false);
        if (!r.ok) { toast(r.error, true); return; }
        toast("Остановлено. Подробности — в боте согласований.");
        refresh();
      });
    });
    $("halt-reading-stop").addEventListener("click", function () {
      var button = this;
      busy(button, true);
      call("POST", "halt", { reading: true }).then(function (r) {
        busy(button, false);
        if (!r.ok) { toast(r.error, true); return; }
        toast("Чтение переписки остановлено: все аккаунты на паузе.");
        refresh();
      });
    });
    $("halt-resume").addEventListener("click", function () {
      var button = this;
      busy(button, true);
      call("POST", "halt/resume", { reading: $("halt-reading-resume").checked }).then(function (r) {
        busy(button, false);
        if (!r.ok) { toast(r.error, true); return; }
        toast("Подтвердите «Продолжить» в боте согласований — карточка уже там.");
        refresh();
      });
    });
  }

  /* Вход на страницу по коду из бота (Р-88): включён ли, почему нет, выключить — сразу, включить при
   * боте от посредника — только нажатием в боте. */
  function renderCodeLogin(c, b) {
    var visible = !!(b && b.configured && b.owner_bound);
    show("code-login", visible);
    if (!visible) return;
    text("code-login-state", c.enabled
      ? "Включён: «Получить код в боте» на экране входа. Код приходит сюда, в бот согласований, и работает 5 минут только в браузере, где его запросили." +
        (c.logins_7d ? " Входов по коду за 7 дней: " + c.logins_7d + "." : "")
      : "Выключен: входить можно только по одноразовой ссылке с сервера.");
    note("code-login-why", "info", !c.enabled && c.origin === "managed"
      ? "Бот создан «в одно нажатие» через посредника: его держатель технически может читать сообщения бота — и коды входа. Поэтому вход по коду по умолчанию выключен. Включайте, только если доверяете посреднику; подтверждение — кнопкой в боте."
      : "");
    text("code-login-toggle", c.enabled ? "Выключить вход по коду" : c.needs_bot_press ? "Включить… (подтверждение в боте)" : "Включить вход по коду");
    $("code-login-toggle").dataset.enable = c.enabled ? "0" : "1";
    note("code-login-note", "info", c.enable_pending ? "Включение ждёт вашего нажатия в боте согласований." : "");
  }

  function wireCodeLogin() {
    $("code-login-toggle").addEventListener("click", function () {
      var button = this, enable = button.dataset.enable === "1";
      busy(button, true);
      call("PUT", "code-login", { enabled: enable }).then(function (r) {
        busy(button, false);
        if (!r.ok && r.status !== 202) { toast(r.error, true); return; }
        toast(r.status === 202 ? "Подтвердите включение в боте согласований — карточка уже там." :
              enable ? "Вход по коду включён." : "Вход по коду выключен. Ждущие коды погашены.");
        refresh();
      });
    });
  }

  /* Бот в одно нажатие через бот-менеджер посредника (bot_setup.py). Страница видит только ссылку
   * t.me/newbot/… и состояние; секрет пары и токен остаются в сервисе. */
  function renderManaged(b, editing) {
    var m = b.managed || {}, a = m.attempt;
    var waiting = !!(a && (a.status === "waiting" || a.status === "checking")), applying = !!(a && a.status === "applying");
    var asking = !!(a && a.status === "confirm"), intercepted = !!(a && a.status === "intercepted");
    show("bot-managed", !!m.available && ((editing && m.ready !== false) || waiting || applying || asking || intercepted));
    if (!m.available) return;
    text("managed-host", m.host || "посредника");
    show("managed-start-row", !waiting && !applying && !asking);
    show("managed-wait", waiting || applying);
    show("managed-confirm", asking);
    note("managed-intercepted", "danger", intercepted ? a.message : "");
    if (asking) {
      var c = a.creator || {};
      text("managed-bot-name", "@" + (a.bot_username || ""));
      text("managed-creator", (c.username ? "@" + c.username : "без имени пользователя") + " (номер " + c.id +
           (c.name ? ", имя «" + c.name + "»" : "") + ")");
      text("managed-confirm-minutes", String(Math.max(1, Math.ceil((a.confirm_seconds_left || 0) / 60))));
      fast(10);
    }
    if (a && (waiting || applying)) {
      if (ui.managedLink !== a.link && a.link) {
        ui.managedLink = a.link;
        var qr = $("managed-qr");
        qr.textContent = "";
        try { qr.appendChild(window.ShturmanQR.svg(a.link, { label: "QR-код со ссылкой создания бота" })); } catch (e) { /* останется кнопка */ }
        $("managed-open").setAttribute("href", a.link);
      }
      // Подсказка для изменённого адреса бота (Р-78): только если посредник дал код привязки (0.5.0+).
      var claim = waiting && a.claim_link ? a.claim_link : "";
      show("managed-claim", !!claim);
      if (claim && ui.managedClaim !== claim) {
        ui.managedClaim = claim;
        $("managed-claim-open").setAttribute("href", claim);
        text("managed-claim-manager", a.manager || "");
      }
      text("managed-minutes", String(Math.max(1, Math.ceil(a.seconds_left / 60))));
      text("managed-status", applying ? "Бот создан, подключаю его…" : "Жду, пока вы подтвердите создание бота…");
      fast(10);
    } else if (ui.managedLink) {
      ui.managedLink = "";
      $("managed-qr").textContent = "";
      $("managed-open").setAttribute("href", "#");
    }
    if (!(a && (waiting || applying)) && ui.managedClaim) {
      ui.managedClaim = "";
      show("managed-claim", false);
      $("managed-claim-open").setAttribute("href", "#");
    }
    if (a && a.status === "failed") note("managed-error", "warn", a.message || "Не получилось.");
    else if (!ui.managedError) note("managed-error", "warn", "");
    if (a && a.status === "done" && ui.managedDone !== a.bot_username) {
      ui.managedDone = a.bot_username;
      toast("Бот @" + a.bot_username + " создан и подключён. Теперь привяжите себя владельцем.");
    }
  }

  function copyText(value, button) {
    function done() { var t = button.textContent; button.textContent = "Скопировано"; setTimeout(function () { button.textContent = t; }, 1500); }
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(value).then(done, function () {});
  }

  function suggestUsername() {
    var abc = "abcdefghijkmnpqrstuvwxyz23456789", out = "", rnd = new Uint8Array(6);
    try { crypto.getRandomValues(rnd); } catch (e) { for (var i = 0; i < 6; i++) rnd[i] = Math.floor(Math.random() * 256); }
    for (var j = 0; j < 6; j++) out += abc.charAt(rnd[j] % abc.length);
    return "soglasovaniya_" + out + "_bot";
  }

  function wireManaged() {
    text("howto-username", suggestUsername());
    // Адрес @BotFather ставит скрипт: в разметке страницы чужих адресов нет.
    $("open-botfather").setAttribute("href", "https://t.me/BotFather");
    Array.prototype.forEach.call(document.querySelectorAll("button.copy"), function (button) {
      button.addEventListener("click", function () {
        var from = button.getAttribute("data-copy-from");
        copyText(from ? $(from).textContent : button.getAttribute("data-copy"), button);
      });
    });
    $("managed-start").addEventListener("click", function () {
      var button = this, code = $("managed-code").value.trim(), body = {};
      if (code) body.code = code;
      busy(button, true);
      ui.managedError = false;
      note("managed-error", "warn", "");
      call("POST", "bot/managed", body).then(function (r) {
        busy(button, false);
        if (!r.ok) {
          ui.managedError = true;
          note("managed-error", "warn", r.error);
          if (r.data && r.data.code === "invite_required") { show("managed-code-field", true); $("managed-code").focus(); }
          return;
        }
        $("managed-code").value = "";
        fast(60);
        refresh(); refreshOverview();
      });
    });
    $("managed-yes").addEventListener("click", function () {
      act(this, "managed-error", "POST", "bot/managed/confirm", { accept: true }, function () {
        toast("Бот подключён. Теперь привяжите себя владельцем."); fast(20);
      });
    });
    $("managed-no").addEventListener("click", function () {
      act(this, "managed-error", "POST", "bot/managed/confirm", { accept: false });
    });
    $("managed-cancel").addEventListener("click", function () {
      act(this, "managed-error", "DELETE", "bot/managed", {}, function () { toast("Создание бота отменено."); });
    });
  }

  function wireBot() {
    wireManaged();
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
      var button = this;
      ask(button, "Убрать токен бота согласований?", {
        yes: "Убрать токен", danger: true,
        note: "Бот перестанет присылать карточки, а привязка владельца к нему потеряет силу. Токен можно будет ввести снова."
      }).then(function (ok) {
        if (ok) act(button, "bot-error", "DELETE", "bot/token", {}, function () { toast("Токен бота убран."); });
      });
    });
    $("bind-go").addEventListener("click", function () {
      var button = this;
      var asked = S && S.bot.owner_bound
        ? ask(button, "Владелец уже привязан. Выдать новую ссылку привязки?", {
            yes: "Выдать ссылку",
            note: "Если новую ссылку откроет другой аккаунт, владелец сменится: ждущие черновики отклонятся, список доверенных очистится." })
        : Promise.resolve(true);
      asked.then(function (ok) { if (ok) bindLink(button); });
    });
    function bindLink(button) {
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
    }
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
      var button = this;
      ask(button, "Убрать ключи приложения Telegram?", {
        yes: "Убрать ключи", danger: true, note: "Без них сервис не сможет подключать аккаунты."
      }).then(function (ok) {
        if (ok) act(button, "keys-error", "DELETE", "tg/keys", {}, function () { toast("Ключи приложения убраны."); });
      });
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
      nodes.push(el("p", { class: "note info", text: "Сначала привяжите себя к боту согласований — шаг 1. Пока сервис не знает, какой аккаунт ваш, он не сможет отличить его от аккаунта помощника." }));
      return nodes;
    }
    nodes.push(el("div", { class: "row" }, [el("button", {
      class: "btn", type: "button", text: again ? "Войти заново" : role === "owner" ? "Показать QR-код для входа" : "Подключить аккаунт помощника",
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
        var button = this;
        ask(button, "Выйти из аккаунта?", {
          yes: "Выйти из аккаунта", danger: true,
          note: "Сессия на сервере завершится. Уже сохранённые сообщения останутся в архиве. Если подключили не тот аккаунт — после выхода удалите его из архива в блоке «Аккаунты в архиве без подключения»."
        }).then(function (ok) {
          if (ok) act(button, null, "POST", "tg/accounts/" + id + "/logout", {}, function () { toast("Сессия аккаунта завершена."); });
        });
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
        var button = this;
        ask(button, "Удалить «" + name + "» из архива?", {
          yes: "Удалить из архива (сообщений: " + number(account.messages) + ")", danger: true,
          note: "Удалятся его чаты и сообщения и всё, что сервис из них извлёк. Вернуть их нельзя. Если подключить этот аккаунт снова, переписка загрузится заново."
        }).then(function (ok) {
          if (ok) act(button, null, "DELETE", "tg/accounts/" + id, {}, function () { toast("Аккаунт удалён из архива. Теперь его можно подключить заново."); });
        });
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
    text("tg-login-title", flow.role === "owner" ? "Вход в ваш аккаунт" : "Вход в аккаунт помощника");
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
    ["chats-options", "chats-counter", "chats-layout-box"].forEach(function (id) { show(id, online); });
    show("chats-list", online && !ui.chatsLayout);
    show("chats-sections", online && !!ui.chatsLayout);
    drawLayouts();
    document.querySelector("#chats-body .toolbar").hidden = !online;
    show("chats-refresh", online);
    if (!online) {
      show("chats-more", false); show("chats-bulk", false); show("chats-load", false);
      if (ui.chatsPoll) { clearTimeout(ui.chatsPoll); ui.chatsPoll = null; }
      if (ui.chatsTick) { clearInterval(ui.chatsTick); ui.chatsTick = null; }
      return;
    }
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
    return [ui.chatAccount, ui.chatsLayout, $("chats-q").value.trim(), $("chats-kind").value, $("chats-only").checked].join("|");
  }

  /* Список чатов загружается из Telegram в фоне (Р-81): запрос страницы только запускает загрузку и
     сразу отвечает тем, что уже есть в базе, вместе с ходом загрузки (`load`). Пока она идёт, страница
     раз в несколько секунд перечитывает список и показывает «загружено N из ~M»; если Telegram просит
     подождать — отсчёт; загрузка потом продолжится сама, с того же места. */
  var LOAD_POLL = 2500;

  function waitWords(seconds) {
    return seconds < 120 ? seconds + " с" : Math.ceil(seconds / 60) + " мин";
  }

  function ageWords(seconds) {
    if (seconds < 60) return "только что";
    var minutes = Math.round(seconds / 60);
    if (minutes < 60) return minutes + " мин назад";
    var hours = Math.round(minutes / 60);
    if (hours < 48) return hours + " ч назад";
    return Math.round(hours / 24) + " дн. назад";
  }

  function drawLoad() {
    var l = ui.chatsLoad || {}, box = $("chats-load");
    if (ui.chatsTick) { clearInterval(ui.chatsTick); ui.chatsTick = null; }
    $("chats-refresh").disabled = !!l.running;
    text("chats-refresh", l.running ? "Список обновляется…" : "Обновить список из Telegram");
    var visible = !!l.running || l.state === "failed" || (!ui.chatsListLoaded && l.state === "idle");
    show(box, visible);
    if (!visible) return;
    box.className = "loadbar" + (l.state === "waiting" ? " wait" : l.state === "failed" ? " bad" : !l.running ? " idle" : "");
    var part = l.expected ? Math.min(100, Math.round(100 * (l.loaded || 0) / l.expected)) : 0;
    show("chats-load-meter-box", !!l.running);
    $("chats-load-meter").style.width = (l.running ? Math.max(3, part) : 0) + "%";
    var got = "Загружено " + number(l.loaded || 0) + (l.expected ? " из ~" + number(l.expected) : "") + ".";
    if (l.state === "waiting") {
      var left = Math.max(1, l.wait_seconds || 1);
      var tick = function () {
        text("chats-load-title", "Telegram просит подождать");
        text("chats-load-text", "Продолжу сам через " + waitWords(left) + " — с того же места, нажимать ничего не нужно. " + got);
        if (left > 1) left -= 1;
      };
      tick();
      ui.chatsTick = setInterval(tick, 1000);
    } else if (l.running) {
      text("chats-load-title", "Загружаю список чатов из Telegram");
      text("chats-load-text", got + " Уже загруженные чаты можно выбирать — список дополнится сам.");
    } else if (l.state === "failed") {
      text("chats-load-title", "Список не обновился");
      text("chats-load-text", (l.error || "Telegram не ответил.") + (ui.chatsListLoaded ? " Показан прежний список." : ""));
    } else {
      text("chats-load-title", "Список чатов ещё не загружен");
      text("chats-load-text", "Нажмите «Обновить список из Telegram» — он загрузится за минуту-другую, страница покажет, как идёт.");
    }
  }

  function schedulePoll() {
    if (ui.chatsPoll) { clearTimeout(ui.chatsPoll); ui.chatsPoll = null; }
    if (!(ui.chatsLoad && ui.chatsLoad.running)) return;
    ui.chatsPoll = setTimeout(function () {
      ui.chatsPoll = null;
      if (ui.chatsBusy > 0 || ui.chatsLoading) { schedulePoll(); return; }   // переключатель ещё сохраняется
      loadChats(true, false, true);
    }, LOAD_POLL);
  }

  function loadChats(reset, refreshList, polling) {
    if (ui.chatAccount === null || ui.chatsLoading) return;
    if (ui.chatsLayout) { loadSections(refreshList, polling); return; }
    var key = chatsQueryKey(), offset = reset ? 0 : ui.chats.length;
    // при опросе — столько, сколько уже показано: «Показать ещё» не сбрасывается
    var limit = polling ? Math.min(200, Math.max(FIRST_CHATS, ui.chats.length)) : (reset ? FIRST_CHATS : MORE_CHATS);
    ui.chatsLoading = true;
    ui.chatsKey = key;
    var query = "?offset=" + offset + "&limit=" + limit + "&q=" + encodeURIComponent($("chats-q").value.trim()) +
      "&kind=" + encodeURIComponent($("chats-kind").value) + ($("chats-only").checked ? "&only=enabled" : "") +
      (refreshList ? "&refresh=1" : "");
    call("GET", "tg/accounts/" + ui.chatAccount + "/dialogs" + query).then(function (r) {
      ui.chatsLoading = false;
      if (key !== chatsQueryKey()) { loadChats(true); return; }       // пока шёл запрос, условие поменяли
      if (!r.ok) {
        if (!polling) note("chats-error", "warn", r.error);
        if (reset) ui.chatsKey = "";           // следующая отрисовка шага попробует снова
        schedulePoll();
        return;
      }
      note("chats-error", "warn", "");
      var was = !!(ui.chatsLoad && ui.chatsLoad.running);
      ui.chats = reset ? r.data.items : ui.chats.concat(r.data.items);
      ui.chatsTotal = r.data.total;
      ui.chatsEnabled = r.data.enabled_total;
      ui.chatsLoad = r.data.load || null;
      ui.chatsListLoaded = !!r.data.loaded;
      ui.chatsAge = typeof r.data.age === "number" ? r.data.age : null;
      drawChats();
      drawLoad();
      if (was && ui.chatsLoad && !ui.chatsLoad.running && ui.chatsLoad.state === "done") toast("Список чатов обновлён из Telegram.");
      else if (refreshList && ui.chatsLoad && !ui.chatsLoad.running && ui.chatsLoad.state === "done") toast("Список только что обновлён — он свежий.");
      schedulePoll();
    });
  }

  function chatRow(item, onChange) {
    var kind = KIND_NAMES[item.kind] || "чат";
    var meta = [el("span", { text: kind })];
    if (item.username) meta.push(el("span", { text: "@" + item.username }));
    if (item.excluded) meta.push(el("span", { class: "badge warn", text: "не сохраняется" }));
    sphereNames(item.spheres).forEach(function (name) { meta.push(el("span", { class: "badge sph-chip", text: name })); });
    var toggle = el("input", { type: "checkbox", checked: item.enabled, disabled: item.excluded });
    toggle.checked = !!item.enabled;
    toggle.addEventListener("change", function () {
      var want = toggle.checked;
      toggle.disabled = true;
      ui.chatsBusy += 1;
      call("POST", "tg/accounts/" + ui.chatAccount + "/sync", { enabled: want, chats: [{ peer_class: item.peer_class, tg_id: item.tg_id }] }).then(function (r) {
        ui.chatsBusy -= 1;
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
        if (onChange) onChange(want ? 1 : -1);
        fast(10);
      });
    });
    var exclude = item.locked ? el("span", { class: "small", text: "служебный чат Telegram" }) : el("button", {
      class: "btn ghost small-btn" + (item.excluded ? "" : " quiet"), type: "button",
      text: item.excluded ? "Вернуть в архив" : "Не сохранять",
      "aria-label": (item.excluded ? "Вернуть в архив чат " : "Не сохранять чат ") + (item.title || ""),
      onclick: function () {
        var button = this, body = { peer_class: item.peer_class, tg_id: item.tg_id, excluded: !item.excluded };
        function send() {
          act(button, "chats-error", "POST", "tg/accounts/" + ui.chatAccount + "/exclude", body, function (data) {
            toast(data.excluded ? "Чат исключён из архива" + (data.purged ? ": стёрто сообщений — " + data.purged + "." : ".") : "Чат возвращён. Отметьте «читать», чтобы сервис снова его сохранял.");
            loadChats(true);
          });
        }
        if (item.excluded) { send(); return; }
        // Два явных выбора вместо двух системных окон подряд (С6): стирание — отдельная красная кнопка.
        ask(button, "Не сохранять чат «" + (item.title || "без названия") + "»?", {
          yes: "Только перестать сохранять",
          extra: [{ text: "Перестать и стереть сохранённое", value: "purge", danger: true }],
          note: "Сервис перестанет принимать его сообщения из любого источника: из аккаунта, бизнес-режима и выгрузки. Уже сохранённое останется, если не выбрать «стереть»; стёртое вернуть нельзя."
        }).then(function (choice) {
          if (!choice) return;
          body.purge = choice === "purge";
          send();
        });
      }
    });
    var row = el("li", { class: "chat" + (item.excluded ? " is-excluded" : "") });
    var pick = ui.sphereList.length && !item.locked ? el("button", {
      class: "btn ghost small-btn quiet", type: "button", text: "Сферы",
      "aria-label": "Сферы чата " + (item.title || ""), "aria-expanded": "false",
      onclick: function () { spherePicker(item, row, this); }
    }) : null;
    [el("div", { class: "chat-main" }, [
      el("span", { class: "chat-title", text: item.title || "Без названия" }),
      el("span", { class: "chat-meta" }, meta)
    ]), el("label", { class: "chat-read" }, [toggle, el("span", { text: "читать" })]), pick, exclude].forEach(function (n) { if (n) row.appendChild(n); });
    return row;
  }

  function sphereNames(ids) {
    return (ids || []).map(function (id) {
      var found = ui.sphereList.filter(function (sp) { return sp.id === id; })[0];
      return found ? found.title : "";
    }).filter(Boolean);
  }

  /* Сферы одного чата: флажки по всем сферам прямо под строкой чата. */
  function spherePicker(item, row, button) {
    var old = row.querySelector(".sph-pick");
    if (old) { row.removeChild(old); button.setAttribute("aria-expanded", "false"); return; }
    button.setAttribute("aria-expanded", "true");
    var chosen = {};
    (item.spheres || []).forEach(function (id) { chosen[id] = true; });
    var box = el("div", { class: "sph-pick" }, [el("p", { class: "small", text: "В каких сферах этот чат — можно в нескольких:" })]);
    var list = el("div", { class: "sph-pick-list" });
    ui.sphereList.forEach(function (sp) {
      var input = el("input", { type: "checkbox" });
      input.checked = !!chosen[sp.id];
      input.addEventListener("change", function () { if (input.checked) chosen[sp.id] = true; else delete chosen[sp.id]; });
      list.appendChild(el("label", { class: "check" }, [input, el("span", { text: sp.title })]));
    });
    box.appendChild(list);
    var save = el("button", { class: "btn small-btn", type: "button", text: "Сохранить" });
    save.addEventListener("click", function () {
      var ids = ui.sphereList.filter(function (sp) { return chosen[sp.id]; }).map(function (sp) { return sp.id; });
      busy(save, true);
      call("PUT", "spheres/chat", { account_id: ui.chatAccount, peer_class: item.peer_class, tg_id: item.tg_id, sphere_ids: ids }).then(function (r) {
        busy(save, false);
        if (!r.ok) { toast(r.error, true); return; }
        item.spheres = r.data.spheres;
        toast(r.data.added.length || r.data.removed.length ? "Сферы чата сохранены." : "Ничего не изменилось.");
        var fresh = chatRow(item, null);
        row.parentNode.replaceChild(fresh, row);
        refreshSpheres();
        if (ui.chatsLayout === "spheres") loadChats(true);
      });
    });
    box.appendChild(el("div", { class: "row" }, [save, el("button", { class: "btn ghost small-btn", type: "button", text: "Отмена",
      onclick: function () { row.removeChild(box); button.setAttribute("aria-expanded", "false"); button.focus(); } })]));
    row.appendChild(box);
  }

  function drawCounter() {
    var words = "Выбрано для чтения: " + ui.chatsEnabled + " " + plural(ui.chatsEnabled, "чат", "чата", "чатов") + "." +
      (ui.chatsLayout ? " Разделов: " + ui.sections.length + "." : " Показано " + number(ui.chats.length) + " из " + number(ui.chatsTotal) + ".");
    if (ui.chatsAge !== null && !(ui.chatsLoad && ui.chatsLoad.running)) words += " Список из Telegram обновлён " + ageWords(ui.chatsAge) + ".";
    text("chats-counter", words);
  }

  function drawChats() {
    show("chats-list", !ui.chatsLayout);
    show("chats-sections", !!ui.chatsLayout);
    if (ui.chatsLayout) { show("chats-more", false); show("chats-bulk", false); drawCounter(); return; }
    var list = $("chats-list");
    list.textContent = "";
    ui.chats.forEach(function (item) { list.appendChild(chatRow(item)); });
    if (!ui.chats.length) {
      var filtered = $("chats-q").value.trim() || $("chats-kind").value || $("chats-only").checked;
      var loading = !!(ui.chatsLoad && ui.chatsLoad.running);
      if (filtered) note("chats-error", "info", loading ? "Пока ничего не найдено — список ещё загружается." : "Ничего не найдено. Измените поиск или вид чатов.");
      else if (ui.chatsListLoaded && !loading) note("chats-error", "info", "В этом аккаунте нет чатов.");
    }
    drawCounter();
    var left = ui.chatsTotal - ui.chats.length;
    show("chats-more", left > 0);
    if (left > 0) text("chats-more", "Показать ещё " + Math.min(left, MORE_CHATS) + " (осталось " + number(left) + ")");
    var kind = $("chats-kind").value, bulk = $("chats-bulk");
    show(bulk, !!kind && !$("chats-q").value.trim() && !$("chats-only").checked && ui.chatsTotal > 0);
    if (kind) text(bulk, "Читать все " + KIND_BULK[kind] + " (" + number(ui.chatsTotal) + ")");
  }

  /* ---- раскладки списка: папки Telegram, сферы, активность, вид (Р-82) */

  var LAYOUTS = [["", "Все чаты"], ["folders", "Папки Telegram"], ["spheres", "Сферы"], ["activity", "По активности"], ["kind", "По виду"]];
  var SECTION_FIRST = 20, SECTION_MORE = 30;
  var EMPTY_LAYOUT = {
    folders: "В Telegram нет папок — или список ещё не обновлялся. Нажмите «Обновить список из Telegram».",
    spheres: "Сфер пока нет. Заведите их в разделе «Сферы» ниже на этой странице.",
    activity: "Ничего не найдено.", kind: "Ничего не найдено."
  };

  function drawLayouts() {
    renderIfChanged("chats-layout", [ui.chatsLayout], function () {
      return LAYOUTS.map(function (l) {
        return el("button", {
          class: "tab", type: "button", role: "tab", id: "chats-layout-" + (l[0] || "all"),
          "aria-selected": ui.chatsLayout === l[0] ? "true" : "false", text: l[1],
          onclick: function () {
            if (ui.chatsLayout === l[0]) return;
            ui.chatsLayout = l[0];
            ui.sectionItems = {};
            ui.sections = [];
            $("chats-sections").textContent = "";        // прежние разделы не показываем, пока грузятся новые
            if (l[0]) $("chats-sections").appendChild(el("p", { class: "small waiting", text: "Раскладываю чаты…" }));
            drawLayouts();
            drawChats();
            loadChats(true);
          }
        });
      });
    });
  }

  function filterQuery() {
    return "&q=" + encodeURIComponent($("chats-q").value.trim()) + "&kind=" + encodeURIComponent($("chats-kind").value) +
      ($("chats-only").checked ? "&only=enabled" : "");
  }

  function loadSections(refreshList, polling) {
    var key = chatsQueryKey(), account = ui.chatAccount;
    ui.chatsLoading = true;
    ui.chatsKey = key;
    call("GET", "tg/accounts/" + account + "/dialogs?offset=0&limit=1" + filterQuery() + (refreshList ? "&refresh=1" : "")).then(function (r) {
      if (!r.ok) {
        ui.chatsLoading = false;
        if (!polling) note("chats-error", "warn", r.error);
        ui.chatsKey = "";
        return;
      }
      var was = !!(ui.chatsLoad && ui.chatsLoad.running);
      ui.chatsLoad = r.data.load || null;
      ui.chatsListLoaded = !!r.data.loaded;
      ui.chatsAge = typeof r.data.age === "number" ? r.data.age : null;
      ui.chatsEnabled = r.data.enabled_total;
      if (!r.data.loaded) {
        ui.chatsLoading = false;
        ui.sections = [];
        drawSections(); drawLoad(); drawCounter(); schedulePoll();
        return;
      }
      return call("GET", "tg/accounts/" + account + "/sections?layout=" + ui.chatsLayout + filterQuery()).then(function (s) {
        ui.chatsLoading = false;
        if (key !== chatsQueryKey()) { loadChats(true); return; }
        if (!s.ok) { if (!polling) note("chats-error", "warn", s.error); schedulePoll(); return; }
        note("chats-error", "warn", "");
        ui.sections = s.data.sections;
        ui.sphereList = s.data.spheres;
        drawSections();
        drawLoad();
        drawCounter();
        ui.sections.forEach(function (sec) { if (ui.sectionOpen[sec.id]) loadSection(sec, true); });
        if (was && ui.chatsLoad && !ui.chatsLoad.running && ui.chatsLoad.state === "done") toast("Список чатов обновлён из Telegram.");
        schedulePoll();
      });
    });
  }

  function drawSections() {
    var box = $("chats-sections");
    box.textContent = "";
    if (!ui.sections.length) {
      if (ui.chatsListLoaded) box.appendChild(el("p", { class: "note info", text: EMPTY_LAYOUT[ui.chatsLayout] || "Ничего не найдено." }));
      return;
    }
    ui.sections.forEach(function (sec) { box.appendChild(sectionNode(sec)); });
  }

  function sectionCount(sec) {
    return "читается " + number(sec.enabled) + " из " + number(sec.total);
  }

  function sectionNode(sec) {
    var det = el("details", { class: "sect", "data-section": sec.id });
    det.open = !!ui.sectionOpen[sec.id];
    var count = el("span", { class: "sect-count" + (sec.enabled ? " on" : ""), text: sectionCount(sec) });
    var waiting = (sec.proposals || []).reduce(function (n, p) { return n + p.count; }, 0);
    det.appendChild(el("summary", {}, [
      el("span", { class: "sect-title" }, [(sec.emoticon ? sec.emoticon + " " : "") + sec.title]),
      waiting ? el("span", { class: "badge warn", text: "новые: " + waiting }) : null,
      count
    ]));
    var body = el("div", { class: "sect-body" });
    det.appendChild(body);
    sec._count = count;
    sec._body = body;
    drawSectionBody(sec);
    det.addEventListener("toggle", function () {
      ui.sectionOpen[sec.id] = det.open;
      if (det.open && !ui.sectionItems[sec.id]) loadSection(sec, true);
    });
    return det;
  }

  function sectionChanged(sec, delta) {
    sec.enabled += delta;
    sec.can_enable = Math.max(0, sec.can_enable - delta);
    if (sec._count) { sec._count.textContent = sectionCount(sec); sec._count.className = "sect-count" + (sec.enabled ? " on" : ""); }
  }

  function loadSection(sec, reset) {
    if (ui.sectionBusy[sec.id]) return;
    var have = ui.sectionItems[sec.id];
    var offset = reset ? 0 : (have ? have.items.length : 0);
    var limit = reset ? Math.max(SECTION_FIRST, have ? Math.min(200, have.items.length) : 0) : SECTION_MORE;
    ui.sectionBusy[sec.id] = true;
    call("GET", "tg/accounts/" + ui.chatAccount + "/dialogs?section=" + encodeURIComponent(sec.id) + "&offset=" + offset + "&limit=" + limit + filterQuery()).then(function (r) {
      ui.sectionBusy[sec.id] = false;
      if (!r.ok) { toast(r.error, true); return; }
      var items = reset || !have ? r.data.items : have.items.concat(r.data.items);
      ui.sectionItems[sec.id] = { items: items, total: r.data.total };
      drawSectionBody(sec);
    });
  }

  function bulkSection(sec, enabled, button) {
    var depth = $("opt-depth"), depthText = depth.options[depth.selectedIndex] ? depth.options[depth.selectedIndex].textContent : "";
    var n = enabled ? sec.can_enable : sec.enabled;
    var question = enabled
      ? "Читать все чаты раздела «" + sec.title + "» (" + n + ")?"
      : "Выключить чтение всех чатов раздела «" + sec.title + "» (" + n + ")?";
    var detail = enabled
      ? "Сервис загрузит их историю (глубина: " + depthText + ") и будет сохранять новые сообщения. Исключённые и служебные чаты останутся выключенными."
      : "Уже сохранённые сообщения останутся в архиве.";
    var body = { section: sec.id, enabled: enabled, q: $("chats-q").value.trim(), kind: $("chats-kind").value, only: $("chats-only").checked };
    ask(button, question, { yes: enabled ? "Читать все (" + n + ")" : "Выключить все (" + n + ")", note: detail }).then(function (ok) {
      if (!ok) return;
      act(button, "chats-error", "POST", "tg/accounts/" + ui.chatAccount + "/bulk", body, function (data) {
        toast((enabled ? "Включено чатов: " : "Выключено чатов: ") + data.changed + (data.skipped ? ". Пропущено исключённых: " + data.skipped : "") + ".");
        ui.sectionItems[sec.id] = null;
        loadChats(true);
      });
    });
  }

  function folderTools(sec) {
    var folderId = Number(sec.id.split(":")[1]);
    var parts = [];
    var made = (sec.spheres || [])[0];
    var linked = (sec.spheres || []).filter(function (x) { return x.propose; })[0];
    parts.push(el("button", { class: "btn ghost small-btn", type: "button",
      text: made ? "Дополнить сферу «" + made.title + "»" : "Сделать сферу из папки",
      onclick: function () { makeFromFolder(ui.chatAccount, folderId, this, function () { loadChats(true); }); } }));
    if (linked) {
      parts.push(el("span", { class: "small", text: "Новые чаты папки предлагаются в сферу «" + linked.title + "»." }));
    } else if (ui.sphereList.length) {
      var select = el("select", { "aria-label": "Сфера для новых чатов папки «" + sec.title + "»" },
        ui.sphereList.map(function (sp) { return el("option", { value: String(sp.id), text: sp.title }); }));
      if (made) select.value = String(made.id);
      parts.push(el("div", { class: "sect-link" }, [el("span", { class: "sect-link-label", text: "Новые чаты папки предлагать в сферу" }),
        select, el("button", { class: "btn ghost small-btn", type: "button", text: "Предлагать",
        onclick: function () { linkFolder(Number(select.value), ui.chatAccount, folderId, true, this, function () { loadChats(true); }); } })]));
    }
    return el("div", { class: "row sect-folder" }, parts);
  }

  function drawSectionBody(sec) {
    var body = sec._body;
    if (!body) return;
    body.textContent = "";
    if (sec.description) body.appendChild(el("p", { class: "small", text: sec.description }));
    (sec.notes || []).forEach(function (n) { body.appendChild(el("p", { class: "hint", text: n })); });
    (sec.proposals || []).forEach(function (p) {
      body.appendChild(proposalNode({ sphere_id: p.sphere_id, sphere_title: p.sphere_title, count: p.count, account_id: ui.chatAccount,
                                      folder_id: Number(sec.id.split(":")[1]), folder_title: sec.title }, function () { loadChats(true); refreshSpheres(); }));
    });
    var actions = [];
    if (sec.can_enable > 0) actions.push(el("button", { class: "btn small-btn", type: "button", text: "Включить все (" + number(sec.can_enable) + ")",
      onclick: function () { bulkSection(sec, true, this); } }));
    if (sec.enabled > 0) actions.push(el("button", { class: "btn ghost small-btn", type: "button", text: "Выключить все (" + number(sec.enabled) + ")",
      onclick: function () { bulkSection(sec, false, this); } }));
    if (actions.length) body.appendChild(el("div", { class: "row" }, actions));
    if (sec.id.indexOf("folder:") === 0 && sec.id !== "folder:none") body.appendChild(folderTools(sec));
    var got = ui.sectionItems[sec.id];
    if (!got) { body.appendChild(el("p", { class: "small waiting", text: "Загружаю…" })); return; }
    if (!got.items.length) { body.appendChild(el("p", { class: "mem-note", text: "В этом разделе нет чатов." })); return; }
    var list = el("ul", { class: "chatlist" });
    got.items.forEach(function (item) { list.appendChild(chatRow(item, function (delta) { sectionChanged(sec, delta); })); });
    body.appendChild(list);
    var left = got.total - got.items.length;
    if (left > 0) body.appendChild(el("button", { class: "btn ghost", type: "button",
      text: "Показать ещё " + Math.min(left, SECTION_MORE) + " (осталось " + number(left) + ")",
      onclick: function () { loadSection(sec, false); } }));
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
      var button = this;
      ask(button, "Читать все " + KIND_BULK[kind] + " этого аккаунта (" + ui.chatsTotal + ")?", {
        yes: "Читать все",
        note: "Сервис загрузит их историю на выбранную глубину и будет сохранять новые сообщения. Исключённые чаты останутся исключёнными."
      }).then(function (ok) {
        if (!ok) return;
        act(button, "chats-error", "POST", "tg/accounts/" + ui.chatAccount + "/sync", { enabled: true, kind: kind }, function (data) {
          toast("Включено чатов: " + data.enabled + ".");
          loadChats(true);
        });
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
    // Расширение (флажок включают) — после вопроса в строке; сужение — сразу.
    function autoOption(box, field, question, detail) {
      box.addEventListener("change", function () {
        if (!box.checked) { saveOptions(makeBody(false), box); return; }
        box.checked = false;
        ask(box, question, { yes: "Да, брать", note: detail }).then(function (ok) {
          if (!ok) return;
          box.checked = true;
          saveOptions(makeBody(true), box);
        });
      });
      function makeBody(on) { var b = {}; b[field] = on; return b; }
    }
    autoOption($("opt-auto-personal"), "auto_personal", "Брать новые личные чаты в архив без вашего выбора?",
      "Любой, кто впервые напишет на этот аккаунт, попадёт в архив сам.");
    autoOption($("opt-auto-groups"), "auto_groups", "Брать новые группы и каналы в архив без вашего выбора?",
      "Группа, в которую добавят этот аккаунт, попадёт в архив сама.");
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
        var button = this;
        var asked = item.state === "running"
          ? ask(button, "Остановить импорт?", { yes: "Остановить импорт", note: "Уже записанные сообщения останутся в архиве." })
          : Promise.resolve(true);
        asked.then(function (ok) {
          if (!ok) return;
          delete ui.scans[id]; delete ui.exclude[id];
          act(button, "import-error", "DELETE", "imports/" + id, {});
        });
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
      ask(button, "Сейчас своя модель сервиса работает по ключу API. Заменить его подпиской ChatGPT?", {
        yes: "Заменить подпиской", note: "После входа ключ будет удалён с сервера."
      }).then(function (ok) {
        if (!ok) return;
        body["switch"] = true;
        goSubscription(button, body);
      });
      return;
    }
    goSubscription(button, body);
  }

  function goSubscription(button, body) {
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
      var button = this;
      ask(button, "Войти другой учётной записью ChatGPT?", {
        yes: "Войти другой", note: "После входа сервис будет пользоваться подпиской новой учётной записи."
      }).then(function (ok) { if (ok) startSubscription(button, true); });
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
      var button = this;
      ask(button, "Выйти из подписки ChatGPT?", {
        yes: "Выйти из подписки", danger: true,
        note: "Сервис перестанет обращаться к модели по подписке; сессия у OpenAI будет отозвана."
      }).then(function (ok) {
        if (!ok) return;
        act(button, "sub-error", "DELETE", "llm/chatgpt", {}, function (data) {
          toast(data.revoked ? "Вы вышли из подписки ChatGPT." : "Вы вышли из подписки, но OpenAI не подтвердил отзыв. Отключить доступ можно в настройках ChatGPT.", !data.revoked);
        });
      });
    });
  }

  function wireLlm() {
    wireSubscription();
    $("llm-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var key = $("llm-key"), body = { api_key: key.value.trim(), base_url: $("llm-url").value.trim(), model: $("llm-model").value.trim() };
      function save() {
        key.value = "";
        act($("llm-save"), "llm-error", "PUT", "llm", body, function (data) {
          ui.signatures.llm = "";
          toast("Модель ответила, ключ сохранён" + (data.model ? ": " + data.model : "") + ".");
        });
      }
      if (S && S.llm && S.llm.way === "subscription") {
        ask($("llm-save"), "Сейчас своя модель сервиса работает по подписке ChatGPT. Заменить её ключом API?", {
          yes: "Заменить ключом", note: "Сервис выйдет из подписки."
        }).then(function (ok) {
          if (!ok) return;
          body["switch"] = true;
          save();
        });
        return;
      }
      save();
    });
    $("llm-remove").addEventListener("click", function () {
      var button = this;
      ask(button, "Убрать свою модель сервиса?", {
        yes: "Убрать модель", danger: true, note: "Сервис перестанет обращаться к модели сам."
      }).then(function (ok) {
        if (ok) act(button, "llm-error", "DELETE", "llm", {}, function () { ui.signatures.llm = ""; toast("Своя модель убрана."); });
      });
    });
  }

  /* --------------------------------------------------- 8. что собрано */

  /* admin — команда для того, кто обслуживает сервер: свёрнутой строкой мелко, не в тексте владельцу. */
  function fact(title, value, admin) {
    return el("div", { class: "fact" }, [el("b", { text: title }), el("span", { class: "value", text: value }),
      admin ? el("details", { class: "for-admin" }, [el("summary", { text: "Для того, кто обслуживает сервер" }),
                                                     el("p", {}, [el("code", { text: admin })])]) : null]);
  }

  /* Что сейчас с ответами без отправки — по способу работы (Х4, Р-86). */
  function sendingFaq(sending) {
    var mode = S && S.scenario && S.scenario.chosen;
    if (sending) {
      return "Отправка сообщений включена на сервере. Ассистент отправляет только после вашего «Да» в боте согласований или по правилам, которые вы разрешили; отсюда отправку не включить и не выключить.";
    }
    var own = "В способе «Через ваш аккаунт» пока только репетиция: ассистент пишет, что ответил бы, вы оцениваете; ничего не отправляется.";
    var staff = "В способе «Через бота и помощника» — черновики ответов, если вы их включите (в боте согласований командой /prepare on): ассистент готовит ответ и показывает вам; отправка — только после вашего нажатия и когда она включена на сервере.";
    return "Сейчас нет: отправка сообщений выключена на сервере и отсюда не включается. " +
      (mode === "own" ? own : mode === "staff" ? staff : own + " " + staff) +
      " Включить отправку — отдельное решение, которое принимаете вы.";
  }

  function renderSummary(data) {
    var a = data.archive || {};
    $("sending-note").hidden = !!a.sending;
    text("faq-sending", sendingFaq(a.sending));
    text("collected", number(a.messages));
    renderIfChanged("tiles", a, function () {
      return [
        ["Сообщений в архиве", a.messages], ["Чатов", a.chats], ["Исключено чатов", a.chats_excluded], ["Аккаунтов", a.accounts]
      ].map(function (t) { return el("div", { class: "tile" }, [el("b", { text: number(t[1]) }), el("span", { text: t[0] })]); });
    });
    renderIfChanged("facts", a, function () {
      var ASK_ADMIN = " Включает тот, кто обслуживает сервер, — попросите его.";
      var search = !a.embeddings_enabled
        ? "Выключен: поиск идёт по словам." + ASK_ADMIN
        : a.embeddings_problem ? "Включён, но модель не отвечает. Попросите того, кто обслуживает сервер, проверить."
        : "Включён. Посчитано сообщений: " + number(a.embeddings_embedded) + (a.embeddings_left ? ", осталось: " + number(a.embeddings_left) : "");
      var guard = !a.guard_enabled
        ? "Выключена: входящие сообщения ассистент читает без проверки." + ASK_ADMIN
        : "Включена" + (a.guard_model_used ? "" : " (без модели — только правила)") + ". Проверено: " + number(a.guard_checked) + ", скрыто: " + number(a.guard_hidden) +
          (a.guard_problem ? ". Модель сейчас не отвечает." : "");
      return [
        fact("Последнее новое сообщение", a.last_message_seen_at ? when(a.last_message_seen_at) : "ещё не было"),
        fact("Очередь заданий", "ждут: " + number(a.jobs_waiting) + ", не выполнено: " + number(a.jobs_failed)),
        fact("Поиск по смыслу", search, !a.embeddings_enabled ? "./ops/embeddings.sh on" : a.embeddings_problem ? "./ops/embeddings.sh status" : null),
        fact("Защита от внедрённых инструкций", guard, !a.guard_enabled ? "./ops/guard.sh on" : null),
        fact("Голосовые и «кружки»", !a.voice_enabled
          ? "Не расшифровываются: ассистент видит, что было голосовое, но не его содержание." + ASK_ADMIN
          : a.voice_problem ? "Расшифровка включена, но распознавание сейчас не отвечает. Ждут: " + number(a.voice_pending)
          : "Расшифровываются. Готово: " + number(a.voice_done) + (a.voice_pending ? ", ждут: " + number(a.voice_pending) : ""),
          !a.voice_enabled ? "./ops/asr.sh on" : null),
        fact("Фото и документы", !a.media_enabled
          ? "Не разбираются: ассистент видит, что было фото или файл, но не что в нём. Включается в шаге «Фото и документы»."
          : "Разбираются. Готово: " + number(a.media_done) + ((a.media_pending || a.media_asking) ? ", ждут: " + number((a.media_pending || 0) + (a.media_asking || 0)) : "") +
            (a.media_skipped ? ", пропущено: " + number(a.media_skipped) : "") + (a.media_failed ? ", не получилось: " + number(a.media_failed) : "")),
        fact("Отправка сообщений", a.sending ? "Включена на сервере." : "Выключена. Включается только на сервере, не здесь.")
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
    { id: "pending", label: "Согласования", load: loadPending, draw: drawPending,
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
    pill("memory", n ? "на согласовании: " + n : "", n ? "todo" : "");
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
            var button = this;
            ask(button, "Отметить как неверное?", {
              yes: "Неверно",
              note: "Ассистент перестанет на это опираться. Если это сменило прежнее, прежнее снова будет действовать."
            }).then(function (ok) {
              if (!ok) return;
              busy(button, true);
              error.hidden = true;
              call("POST", "memory/facts/" + f.id + "/retract", {}).then(function (r) {
                if (!r.ok) { busy(button, false); error.textContent = r.error; error.hidden = false; return; }
                toast("Отмечено как неверное.");
                refreshOverview();
                after();
              });
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
        : "Страниц пока нет. Они появятся после ночной обработки переписки: ассистент предложит завести страницы о людях, с кем вы больше всего переписываетесь, — в боте согласований и во вкладке «Согласования»." });
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

  function projectSphere(p) {
    var select = el("select", { id: "mem-project-sphere" }, [el("option", { value: "", text: "Без сферы" })].concat(
      ui.sphereList.map(function (sp) { return el("option", { value: String(sp.id), text: sp.title }); })));
    select.value = p.sphere_id ? String(p.sphere_id) : "";
    var save = el("button", { class: "btn ghost", id: "mem-project-sphere-save", type: "button", text: "Сохранить",
      onclick: function () {
        busy(save, true);
        call("PUT", "memory/projects/" + p.id + "/sphere", { sphere_id: select.value ? Number(select.value) : null }).then(function (r) {
          busy(save, false);
          if (!r.ok) { toast(r.error, true); return; }
          toast(r.data.changed ? "Сфера проекта сохранена." : "Ничего не изменилось.");
          refreshSpheres();
          showMemory(true);
        });
      } });
    return memSection("Сфера проекта", ui.sphereList.length ? "Ассистент ищет по проекту в его сфере и помечает её в ответах. У проекта — не больше одной сферы."
                                                             : "Сфер пока нет — заведите их в разделе «Сферы» на этой странице.", ui.sphereList.length ? [
      el("div", { class: "mem-add" }, [el("div", { class: "field grow" }, [el("label", { for: "mem-project-sphere", text: "Сфера" }), select]), save])
    ] : []);
  }

  function projectMeta(p) {
    var parts = [];
    if (p.sphere) parts.push("сфера «" + p.sphere + "»");
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
      : el("p", { class: "note", text: archived.length ? "Действующих проектов нет." : "Проектов пока нет. Заведите проект сами или дождитесь предложения ассистента — оно придёт в бот согласований и во вкладку «Согласования»." }));
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
    nodes.push(projectSphere(p));
    if (!archived) {
      var error = el("p", { class: "note warn mem-error", role: "alert", hidden: true });
      nodes.push(memSection("Проект закончился?", "В архиве страница останется, но перестанет обновляться.", [
        el("div", { class: "row" }, [el("button", { class: "btn ghost danger", id: "mem-archive", type: "button", text: "В архив",
          onclick: function () {
            var button = this;
            ask(button, "Перенести проект «" + p.title + "» в архив?", {
              yes: "В архив",
              note: "Страница останется, но сводка перестанет обновляться, а новые договорённости из его чатов не будут к нему относиться."
            }).then(function (ok) {
              if (!ok) return;
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
      el("p", { text: "Что ассистент знает о вас и каким правилам следует. Факты о вас появляются здесь только после вашего «Запомнить» — в боте согласований или во вкладке «Согласования»." }),
      ownerBlock({
        title: "Ваши правила и указания", label: "Текст правил", value: p.owner, editable: p.editable,
        path: "memory/profile/owner-block", saved: function (v) { p.owner = v; },
        lead: "Ассистент следует этим правилам и никогда их не меняет.",
        hint: "Пишите обычным текстом: как к вам обращаться, когда не беспокоить, что решаете только вы.",
        placeholder: "Например: отвечать коротко; по выходным не беспокоить, кроме срочного по стройке; платежи — только с моего «да»."
      }),
      factsSection("Что ассистент знает о вас", "Одобренные вами факты: должность, компания, город и подобное.",
        p.facts, "Пока ничего. Когда ассистент заметит в переписке что-то о вас, он спросит — в боте согласований и во вкладке «Согласования».",
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
    var nodes = [el("p", { class: "small", text: "То же, что бот согласований присылает карточками с кнопками. Решение здесь действует сразу — как нажатие в боте." })];
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
            ["Завести проект", function (b, item) { decide(b, item, path, { accept: true }, "Проект заведён."); }],
            ["Не нужно", function (b, item) { decide(b, item, path, { accept: false }, "Не заводим."); }]);
        }))]));
    }
    if (groups.owner_facts.length) {
      nodes.push(memSection("Факты о вас", "«Запомнить» — ассистент запомнит это в вашем профиле. «Не запоминать» — забудет и больше не спросит об этом сообщении.",
        [el("ul", { class: "mem-items" }, groups.owner_facts.map(function (f) {
          var path = "memory/pending/owner-facts/" + f.id;
          return pendingItem(f.slot ? f.slot + ": " + f.text : f.text, [
            el("span", { class: "mem-meta", text: "с " + day(f.since) + (f.said ? "; " + f.said : "") }),
            f.quote ? el("q", { class: "mem-quote", text: f.quote }) : null
          ],
            ["Запомнить", function (b, item) { decide(b, item, path, { accept: true, fingerprint: f.fingerprint }, "Запомнено в профиле."); }],
            ["Не запоминать", function (b, item) { decide(b, item, path, { accept: false }, "Не запоминаем."); }]);
        }))]));
    }
    if (groups.pages.length) {
      nodes.push(memSection("Завести страницу о человеке?", "Ассистент будет вести о нём сводку по переписке и опираться на неё в ответах.",
        [el("ul", { class: "mem-items" }, groups.pages.map(function (p) {
          var path = "memory/pending/pages/" + p.person_id;
          return pendingItem(p.name, [el("span", { class: "mem-meta", text: "Почему предложено: " + p.reason })],
            ["Завести страницу", function (b, item) { decide(b, item, path, { accept: true }, "Страница будет заведена."); }],
            ["Не нужно", function (b, item) { decide(b, item, path, { accept: false }, "Не заводим."); }]);
        }))]));
    }
    if (groups.commitments.length) {
      nodes.push(memSection("Новые договорённости из переписки", "«Верно» — ассистент запишет договорённость и будет напоминать о ней. «Не так» — забудет.",
        [el("ul", { class: "mem-items" }, groups.commitments.map(function (c) {
          var path = "memory/pending/commitments/" + c.id;
          return pendingItem(c.who, [
            el("span", { text: c.what }),
            el("span", { class: "mem-meta", text: c.due }),
            c.quote ? el("q", { class: "mem-quote", text: c.quote }) : null
          ],
            ["Верно", function (b, item) { decide(b, item, path, { accept: true, fingerprint: c.fingerprint }, "Договорённость записана."); }],
            ["Не так", function (b, item) { decide(b, item, path, { accept: false }, "Отклонено."); }]);
        }))]));
    }
    return nodes;
  }

  /* ------------------------------------------------------------- сферы (Р-82) */

  /* Список сфер нужен и списку чатов (подписи в строках, выбор сфер чата), и памяти (сфера проекта):
     спрашивается при входе и после каждого изменения сфер. */
  function refreshSpheres() {
    return call("GET", "spheres", undefined, true).then(function (r) {
      if (!r.ok) return;
      ui.sph = r.data;
      ui.sphereList = r.data.spheres.map(function (sp) { return { id: sp.id, title: sp.title }; });
      var waiting = r.data.proposals.reduce(function (n, p) { return n + p.count; }, 0);
      pill("spheres", waiting ? "новые чаты: " + waiting : r.data.spheres.length ? "сфер: " + r.data.spheres.length : "", waiting ? "todo" : "");
      if ($("spheres-details").open) drawSpheres();
    });
  }

  function sphError(message) { note("sph-error", "warn", message || ""); }

  function proposalNode(p, done) {
    var names = (p.chats || []).map(function (c) { return c.title || "без названия"; }).slice(0, 8);
    var add = el("button", { class: "btn small-btn", type: "button", text: "Добавить в сферу" });
    var no = el("button", { class: "btn ghost small-btn", type: "button", text: "Не предлагать" });
    function decide(accept, button) {
      busy(add, true); busy(no, true);
      call("POST", "spheres/" + p.sphere_id + "/proposals", { account_id: p.account_id, folder_id: p.folder_id, accept: accept }).then(function (r) {
        busy(add, false); busy(no, false);
        if (!r.ok) { toast(r.error, true); return; }
        toast(accept ? "Добавлено в сферу: " + r.data.added + "." : "Больше не предложу: " + r.data.declined + ".");
        refreshSpheres();
        if (done) done();
      });
    }
    add.addEventListener("click", function () { decide(true, add); });
    no.addEventListener("click", function () { decide(false, no); });
    return el("div", { class: "note info sph-offer" }, [
      el("p", {}, [el("b", { text: "Новые чаты в папке «" + p.folder_title + "»: " + p.count + "." }),
        " Добавить в сферу «" + p.sphere_title + "»?"]),
      names.length ? el("p", { class: "small", text: names.join(", ") + (p.count > names.length ? " и ещё " + (p.count - names.length) : "") }) : null,
      el("div", { class: "row" }, [add, no])
    ]);
  }

  function makeFromFolder(account, folderId, button, done) {
    busy(button, true);
    call("POST", "spheres/folders/make", { account_id: account, folder_id: folderId }).then(function (r) {
      busy(button, false);
      if (!r.ok) { toast(r.error, true); return; }
      toast((r.data.created ? "Сфера «" + r.data.sphere.title + "» заведена" : "Сфера «" + r.data.sphere.title + "» дополнена") +
            ": добавлено чатов — " + r.data.added + ".");
      refreshSpheres();
      if (done) done();
    });
  }

  function linkFolder(sphereId, account, folderId, link, button, done) {
    busy(button, true);
    call("POST", "spheres/" + sphereId + "/folders", { account_id: account, folder_id: folderId, link: link }).then(function (r) {
      busy(button, false);
      if (!r.ok) { toast(r.error, true); return; }
      toast(link ? "Папка связана: новые чаты в ней будут предлагаться" + (r.data.proposed ? " (сейчас — " + r.data.proposed + ")." : ".") : "Папка отвязана.");
      refreshSpheres();
      if (done) done();
    });
  }

  function loadSphereFolders() {
    return call("GET", "spheres/folders").then(function (r) { if (r.ok) ui.sphFolders = r.data.folders; });
  }

  function drawSpheres() {
    var data = ui.sph;
    if (!data) return;
    var offers = $("sph-proposals");
    offers.textContent = "";
    data.proposals.forEach(function (p) { offers.appendChild(proposalNode(p, null)); });
    var list = $("sph-list");
    list.textContent = "";
    if (!data.spheres.length) list.appendChild(el("li", { class: "mem-note sph-empty", text: "Сфер пока нет. Заведите первую ниже или сделайте её из папки Telegram." }));
    data.spheres.forEach(function (sp, i) { list.appendChild(sphereItem(sp, i, data.spheres)); });
    drawSphereFolders();
  }

  function sphereMeta(sp) {
    var parts = [sp.chats + " " + plural(sp.chats, "чат", "чата", "чатов")];
    if (sp.projects) parts.push(sp.projects + " " + plural(sp.projects, "проект", "проекта", "проектов"));
    sp.folders.forEach(function (f) {
      parts.push("папка «" + (f.title || "№ " + f.folder_id) + "»" + (f.present ? "" : " — её больше нет в Telegram") + (f.propose ? ": новые предлагаются" : ""));
    });
    return parts.join(" · ");
  }

  function moveSphere(all, i, step, button) {
    var ids = all.map(function (sp) { return sp.id; });
    var j = i + step;
    if (j < 0 || j >= ids.length) return;
    var t = ids[i]; ids[i] = ids[j]; ids[j] = t;
    busy(button, true);
    call("PUT", "spheres/order", { ids: ids }).then(function (r) {
      busy(button, false);
      if (!r.ok) { toast(r.error, true); refreshSpheres(); return; }
      refreshSpheres();
    });
  }

  function sphereItem(sp, i, all) {
    var li = el("li", { class: "sph", "data-sphere": String(sp.id) });
    if (ui.sphEdit === sp.id) { li.appendChild(sphereEditForm(sp)); return li; }
    li.appendChild(el("div", { class: "sph-head" }, [
      el("div", { class: "sph-main" }, [
        el("span", { class: "sph-name", text: sp.title }),
        sp.description ? el("span", { class: "sph-desc", text: sp.description }) : null,
        el("span", { class: "mem-meta", text: sphereMeta(sp) })
      ]),
      el("div", { class: "sph-order" }, [
        el("button", { class: "btn ghost small-btn quiet", type: "button", text: "↑", "aria-label": "Выше: " + sp.title, disabled: i === 0,
          onclick: function () { moveSphere(all, i, -1, this); } }),
        el("button", { class: "btn ghost small-btn quiet", type: "button", text: "↓", "aria-label": "Ниже: " + sp.title, disabled: i === all.length - 1,
          onclick: function () { moveSphere(all, i, 1, this); } })
      ])
    ]));
    var open = !!ui.sphOpen[sp.id];
    li.appendChild(el("div", { class: "row" }, [
      el("button", { class: "btn ghost small-btn", type: "button", "aria-expanded": open ? "true" : "false",
        text: open ? "Скрыть чаты" : "Чаты (" + sp.chats + ")",
        onclick: function () { ui.sphOpen[sp.id] = !open; drawSpheres(); } }),
      el("button", { class: "btn ghost small-btn", type: "button", text: "Изменить",
        onclick: function () { ui.sphEdit = sp.id; drawSpheres(); } }),
      el("button", { class: "btn ghost small-btn danger", type: "button", text: "Удалить",
        onclick: function () {
          var button = this;
          ask(button, "Удалить сферу «" + sp.title + "»?", {
            yes: "Удалить сферу", danger: true,
            note: "Она будет снята с чатов (" + sp.chats + ") и проектов (" + sp.projects + "). Сами чаты, сохранённые сообщения и проекты останутся."
          }).then(function (ok) {
            if (!ok) return;
            busy(button, true);
            call("DELETE", "spheres/" + sp.id).then(function (r) {
              busy(button, false);
              if (!r.ok) { toast(r.error, true); return; }
              toast("Сфера удалена. Чаты и проекты остались.");
              refreshSpheres();
              if (ui.chatsLayout) loadChats(true);
            });
          });
        } })
    ]));
    if (open) li.appendChild(sphereChats(sp));
    return li;
  }

  function sphereEditForm(sp) {
    var title = el("input", { type: "text", maxlength: "60", id: "sph-edit-title", value: sp.title });
    var about = el("input", { type: "text", maxlength: "300", id: "sph-edit-description", value: sp.description || "" });
    var save = el("button", { class: "btn small-btn", type: "submit", text: "Сохранить" });
    var form = el("form", { class: "sph-edit", novalidate: true }, [
      el("div", { class: "grid2" }, [
        el("div", { class: "field" }, [el("label", { for: "sph-edit-title", text: "Название" }), title]),
        el("div", { class: "field" }, [el("label", { for: "sph-edit-description", text: "Коротко, что в ней" }), about])
      ]),
      el("div", { class: "row" }, [save, el("button", { class: "btn ghost small-btn", type: "button", text: "Отмена",
        onclick: function () { ui.sphEdit = null; drawSpheres(); } })])
    ]);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      busy(save, true);
      call("PUT", "spheres/" + sp.id, { title: title.value, description: about.value }).then(function (r) {
        busy(save, false);
        if (!r.ok) { toast(r.error, true); return; }
        ui.sphEdit = null;
        toast("Сфера сохранена.");
        refreshSpheres();
      });
    });
    setTimeout(function () { title.focus(); }, 0);
    return form;
  }

  function sphereChats(sp) {
    var box = el("div", { class: "sph-chats" }, [el("p", { class: "small waiting", text: "Загружаю чаты…" })]);
    call("GET", "spheres/" + sp.id + "/chats").then(function (r) {
      box.textContent = "";
      if (!r.ok) { box.appendChild(el("p", { class: "note warn", text: r.error })); return; }
      var items = r.data.chats;
      if (!items.length) box.appendChild(el("p", { class: "mem-note", text: "В сфере пока нет чатов. Добавьте их здесь, в строке чата в шаге «Какие чаты читать» или сделайте сферу из папки Telegram." }));
      else box.appendChild(el("ul", { class: "mem-items" }, items.map(function (c) {
        var meta = [c.enabled ? "читается" : c.excluded ? "не сохраняется" : "не читается"];
        if (c.account_role === "assistant") meta.push("аккаунт помощника");
        return el("li", { class: "mem-chat" }, [
          el("span", {}, [c.title || "без названия", el("span", { class: "mem-meta", text: " · " + meta.join(" · ") })]),
          el("button", { class: "btn ghost small-btn", type: "button", text: "Убрать", "aria-label": "Убрать из сферы чат " + (c.title || ""),
            onclick: function () {
              var button = this;
              busy(button, true);
              call("POST", "spheres/" + sp.id + "/chats", { account_id: c.account_id, chats: [{ peer_class: c.peer_class, tg_id: c.tg_id }], add: false }).then(function (x) {
                busy(button, false);
                if (!x.ok) { toast(x.error, true); return; }
                toast("Чат убран из сферы.");
                refreshSpheres();
              });
            } })
        ]);
      })));
      box.appendChild(sphereAdd(sp));
    });
    return box;
  }

  /* Добавить чат в сферу: поиск по списку диалогов того аккаунта, что выбран в шаге «Какие чаты читать». */
  function sphereAdd(sp) {
    if (ui.chatAccount === null) return el("p", { class: "small", text: "Добавлять чаты можно, когда подключён аккаунт Telegram." });
    var input = el("input", { type: "search", autocomplete: "off", placeholder: "Найти чат по названию", "aria-label": "Найти чат для сферы «" + sp.title + "»" });
    var found = el("ul", { class: "mem-items sph-found" });
    var timer = null;
    input.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        var q = input.value.trim();
        found.textContent = "";
        if (!q) return;
        call("GET", "tg/accounts/" + ui.chatAccount + "/dialogs?offset=0&limit=8&q=" + encodeURIComponent(q)).then(function (r) {
          found.textContent = "";
          if (!r.ok) { found.appendChild(el("li", { class: "mem-note", text: r.error })); return; }
          if (!r.data.items.length) found.appendChild(el("li", { class: "mem-note", text: "Ничего не нашлось." }));
          r.data.items.forEach(function (item) {
            var inside = (item.spheres || []).indexOf(sp.id) >= 0;
            found.appendChild(el("li", { class: "mem-chat" }, [
              el("span", {}, [item.title || "без названия", el("span", { class: "mem-meta", text: " · " + (KIND_NAMES[item.kind] || "чат") })]),
              inside ? el("span", { class: "badge ok", text: "уже в сфере" }) : el("button", { class: "btn ghost small-btn", type: "button", text: "Добавить",
                onclick: function () {
                  var button = this;
                  busy(button, true);
                  call("POST", "spheres/" + sp.id + "/chats", { account_id: ui.chatAccount, chats: [{ peer_class: item.peer_class, tg_id: item.tg_id }], add: true }).then(function (x) {
                    busy(button, false);
                    if (!x.ok) { toast(x.error, true); return; }
                    toast("Чат добавлен в сферу.");
                    refreshSpheres();
                  });
                } })
            ]));
          });
        });
      }, 300);
    });
    return el("div", { class: "field sph-add" }, [el("label", { text: "Добавить чат" }), input, found]);
  }

  function drawSphereFolders() {
    var list = $("sph-folders");
    list.textContent = "";
    var folders = ui.sphFolders || [];
    show("sph-folders-empty", !folders.length);
    folders.forEach(function (f) {
      var made = f.spheres[0];
      var linked = f.spheres.filter(function (x) { return x.propose; })[0];
      var parts = [el("button", { class: "btn ghost small-btn", type: "button",
        text: made ? "Дополнить «" + made.title + "»" : "Сделать сферу",
        onclick: function () { makeFromFolder(f.account_id, f.folder_id, this, function () { loadSphereFolders().then(drawSphereFolders); }); } })];
      if (linked) {
        parts.push(el("button", { class: "btn ghost small-btn quiet", type: "button", text: "Не предлагать новые",
          onclick: function () { linkFolder(linked.id, f.account_id, f.folder_id, false, this, function () { loadSphereFolders().then(drawSphereFolders); }); } }));
      } else if (ui.sphereList.length) {
        var select = el("select", { "aria-label": "Сфера для новых чатов папки «" + f.title + "»" },
          ui.sphereList.map(function (sp) { return el("option", { value: String(sp.id), text: sp.title }); }));
        if (made) select.value = String(made.id);
        parts.push(el("div", { class: "sect-link" }, [el("span", { class: "sect-link-label", text: "Новые чаты — в сферу" }),
          select, el("button", { class: "btn ghost small-btn", type: "button", text: "Предлагать новые",
          onclick: function () { linkFolder(Number(select.value), f.account_id, f.folder_id, true, this, function () { loadSphereFolders().then(drawSphereFolders); }); } })]));
      }
      var meta = f.count + " " + plural(f.count, "чат", "чата", "чатов") + (f.account_role === "assistant" ? " · аккаунт помощника" : "");
      if (made) meta += " · сфера «" + made.title + "»" + (linked ? ", новые предлагаются" : "");
      list.appendChild(el("li", { class: "sph-folder" }, [
        el("div", { class: "sph-main" }, [
          el("span", { class: "sph-name", text: (f.emoticon ? f.emoticon + " " : "") + f.title }),
          el("span", { class: "mem-meta", text: meta })
        ].concat(f.notes.map(function (n) { return el("span", { class: "hint", text: n }); }))),
        el("div", { class: "row" }, parts)
      ]));
    });
  }

  function showSpheres() {
    return Promise.all([refreshSpheres(), loadSphereFolders()]).then(drawSpheres);
  }

  function wireSpheres() {
    $("spheres-details").addEventListener("toggle", function () { if ($("spheres-details").open) showSpheres(); });
    $("sph-new").addEventListener("submit", function (event) {
      event.preventDefault();
      var save = $("sph-new-save");
      busy(save, true);
      sphError("");
      call("POST", "spheres", { title: $("sph-new-title").value, description: $("sph-new-description").value }).then(function (r) {
        busy(save, false);
        if (!r.ok) { sphError(r.error); return; }
        $("sph-new-title").value = "";
        $("sph-new-description").value = "";
        toast("Сфера «" + r.data.sphere.title + "» заведена.");
        ui.sphOpen[r.data.sphere.id] = true;
        showSpheres();
      });
    });
  }

  /* ------------------------------------------------------- уведомления (Р-84) */

  var nt = { data: null, people: [], timer: null };

  function radioValue(name) {
    var node = document.querySelector('input[name="' + name + '"]:checked');
    return node ? node.value : "";
  }
  function setRadio(name, value) {
    Array.prototype.forEach.call(document.querySelectorAll('input[name="' + name + '"]'), function (node) { node.checked = node.value === value; });
  }

  function refreshNotify() {
    return call("GET", "notify", undefined, true).then(function (r) {
      if (!r.ok) return;
      nt.data = r.data;
      var s = r.data.settings, on = s.personal !== "off" || s.groups;
      pill("notify", !r.data.own_bot ? "нет бота" : on ? (r.data.default ? "включены по умолчанию" : "включены") : "выключены",
           !r.data.own_bot ? "todo" : on ? "done" : "");
    });
  }

  function drawNotify() {
    var d = nt.data;
    if (!d) return;
    var s = d.settings;
    show("notify-nobot", !d.own_bot);
    show("notify-default", d.default);
    setRadio("nt-personal", s.personal);
    setRadio("nt-mode", s.mode);
    $("nt-groups").checked = s.groups;
    $("nt-preview").checked = s.preview;
    $("nt-quiet").checked = s.quiet;
    $("nt-aliases").value = s.aliases.join(", ");
    $("nt-gap").value = String(s.gap_minutes);
    if ($("nt-gap").value !== String(s.gap_minutes)) {
      $("nt-gap").appendChild(el("option", { value: String(s.gap_minutes), text: s.gap_minutes + " мин" }));
      $("nt-gap").value = String(s.gap_minutes);
    }
    $("nt-digest").value = String(s.digest_minutes);
    $("nt-quiet-from").value = s.quiet_from;
    $("nt-quiet-to").value = s.quiet_to;
    text("nt-tz", tzWords(d.timezone));
    nt.people = d.people.slice();
    var list = $("nt-spheres");
    list.textContent = "";
    d.spheres.forEach(function (sp) {
      list.appendChild(el("label", { class: "check" }, [
        el("input", { type: "checkbox", value: String(sp.id), checked: s.spheres.indexOf(sp.id) >= 0 }), el("span", { text: sp.title })]));
    });
    show("nt-spheres-empty", !d.spheres.length);
    drawNtMuted(d);
    drawNtPeople();
    ntToggle();
    var st = d.status;
    text("notify-stats", "За сутки отправлено: " + number(st.sent_24h) + (st.waiting ? ", ждут отправки: " + number(st.waiting) : "") +
         (st.answered_24h ? ", не понадобилось (вы уже ответили): " + number(st.answered_24h) : ""));
  }

  function ntToggle() {
    var personal = radioValue("nt-personal"), mode = radioValue("nt-mode");
    show("nt-spheres-box", personal === "spheres");
    show("nt-people-box", personal === "people");
    $("nt-gap").disabled = mode !== "instant";
    $("nt-digest").disabled = mode !== "digest";
    $("nt-quiet-from").disabled = $("nt-quiet-to").disabled = !$("nt-quiet").checked;
  }

  function personName(p) { return p.name || (p.username ? "@" + p.username : "№ " + p.tg_id); }

  /* «Не сообщать об этом чате» (кнопка под уведомлением в боте, Б14): вернуть — только здесь. */
  function drawNtMuted(d) {
    var muted = d.muted || [], list = $("nt-muted");
    show("nt-muted-box", muted.length > 0);
    list.textContent = "";
    muted.forEach(function (c) {
      list.appendChild(el("li", { class: "mem-chat" }, [
        el("span", { text: c.title || "Чат № " + c.id }),
        el("button", { class: "btn ghost small-btn", type: "button", text: "Вернуть", "aria-label": "Снова сообщать о чате " + (c.title || c.id),
          onclick: function () {
            var rest = d.settings.muted_chats.filter(function (id) { return id !== c.id; }), button = this;
            busy(button, true);
            call("PUT", "notify", { muted_chats: rest }).then(function (r) {
              busy(button, false);
              if (!r.ok) { toast(r.error, true); return; }
              nt.data = r.data;
              drawNotify();
              toast("Снова сообщаю об этом чате.");
            });
          } })
      ]));
    });
  }

  function drawNtPeople() {
    var list = $("nt-people");
    list.textContent = "";
    if (!nt.people.length) list.appendChild(el("li", { class: "mem-chat mem-note", text: "Пока никого. Найдите человека ниже." }));
    nt.people.forEach(function (p, i) {
      list.appendChild(el("li", { class: "mem-chat" }, [
        el("span", {}, [personName(p), el("span", { class: "mem-meta", text: " · № " + p.tg_id })]),
        el("button", { class: "btn ghost small-btn", type: "button", text: "Убрать", "aria-label": "Убрать " + personName(p),
          onclick: function () { nt.people.splice(i, 1); drawNtPeople(); } })
      ]));
    });
  }

  function searchNtPeople() {
    var q = $("nt-people-q").value.trim(), found = $("nt-people-found");
    found.textContent = "";
    if (!q) return;
    call("GET", "notify/people?limit=8&q=" + encodeURIComponent(q)).then(function (r) {
      found.textContent = "";
      if (!r.ok) { found.appendChild(el("li", { class: "mem-note", text: r.error })); return; }
      if (!r.data.items.length) found.appendChild(el("li", { class: "mem-chat mem-note", text: "Ничего не нашлось среди ваших личных чатов." }));
      r.data.items.forEach(function (p) {
        var chosen = nt.people.some(function (x) { return x.tg_id === p.tg_id; });
        found.appendChild(el("li", { class: "mem-chat" }, [
          el("span", {}, [personName(p), p.username && p.name ? el("span", { class: "mem-meta", text: " · @" + p.username }) : null]),
          chosen ? el("span", { class: "badge ok", text: "выбран" }) : el("button", { class: "btn ghost small-btn", type: "button", text: "Добавить",
            onclick: function () { nt.people.push(p); drawNtPeople(); searchNtPeople(); } })
        ]));
      });
    });
  }

  function notifyBody() {
    var spheres = Array.prototype.map.call(document.querySelectorAll("#nt-spheres input:checked"), function (n) { return Number(n.value); });
    var aliases = $("nt-aliases").value.split(",").map(function (a) { return a.trim(); }).filter(Boolean);
    return {
      personal: radioValue("nt-personal"), spheres: spheres, people: nt.people.map(function (p) { return p.tg_id; }),
      groups: $("nt-groups").checked, aliases: aliases, preview: $("nt-preview").checked,
      mode: radioValue("nt-mode"), gap_minutes: Number($("nt-gap").value), digest_minutes: Number($("nt-digest").value),
      quiet: $("nt-quiet").checked, quiet_from: $("nt-quiet-from").value, quiet_to: $("nt-quiet-to").value
    };
  }

  function showNotify() { showTriage(); return refreshNotify().then(drawNotify); }

  function wireNotify() {
    wireTriage();
    $("notify-details").addEventListener("toggle", function () { if ($("notify-details").open) showNotify(); });
    $("notify-form").addEventListener("change", ntToggle);
    $("nt-people-q").addEventListener("input", function () { clearTimeout(nt.timer); nt.timer = setTimeout(searchNtPeople, 300); });
    $("notify-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var save = $("notify-save"), body = notifyBody();
      busy(save, true);
      note("notify-error", "warn", "");
      call("PUT", "notify", body).then(function (r) {
        busy(save, false);
        if (!r.ok) { note("notify-error", "warn", r.error); return; }
        nt.data = r.data;
        drawNotify();
        refreshNotify();
        toast(body.personal === "off" && !body.groups ? "Уведомления выключены." : "Уведомления сохранены.");
      });
    });
  }

  /* ------------------------------------------------------- разбор входящих (Р-92) */
  /* Блок внутри карточки «Уведомления». Включить — только нажатием в боте согласований: кнопка здесь
   * присылает туда карточку. Выключить и правила — сразу. */

  var tr = { data: null, people: [], chats: [], timer: null, chatTimer: null };
  var TR_LEVELS = [["important", "⭐ Важный"], ["lower", "🔕 Реже беспокоить"], ["quiet", "💤 Тихий"]];
  var TR_PEOPLE = { important: "important_people", lower: "lower_people", quiet: "quiet_people" };
  var TR_CHATS = { important: "important_chats", lower: "lower_chats", quiet: "quiet_chats" };

  function trHours(minutes) {
    var h = Math.floor(minutes / 60), m = minutes % 60;
    return h && m ? h + " ч " + m + " мин" : h ? h + " ч" : m + " мин";
  }

  function trNum(value, digits) { return Number(value).toFixed(digits).replace(".", ","); }

  function refreshTriage() {
    return call("GET", "triage", undefined, true).then(function (r) {
      if (!r.ok) return;
      tr.data = r.data;
      var on = r.data.settings.enabled;
      pill("triage", !r.data.own_bot ? "нет бота" : r.data.stopped && on ? "на паузе — остановлено" : on ? "включён" :
           r.data.pending ? "ждёт нажатия в боте" : "выключен", !r.data.own_bot ? "todo" : on ? "done" : "");
    });
  }

  function drawTriage() {
    var d = tr.data;
    if (!d) return;
    var s = d.settings, on = s.enabled;
    show("tr-nobot", !d.own_bot);
    show("tr-stopped", d.stopped && on);
    show("tr-pending", !on && !!d.pending);
    show("tr-enable", !on && d.own_bot);
    $("tr-enable").disabled = !!d.pending || d.stopped;
    $("tr-enable").textContent = d.pending ? "Ждёт нажатия в боте" : "Включить разбор…";
    show("tr-disable", on);
    text("tr-remind-words", trHours(s.remind_minutes));
    var goes = $("tr-goes");
    goes.textContent = "";
    (d.what_goes || []).forEach(function (line) { goes.appendChild(el("li", { text: line })); });
    var e = d.estimate;
    note("tr-estimate", "info", "Сколько примерно: за последние 7 дней — " + number(e.incoming_7d) + " " +
         plural(e.incoming_7d, "входящее личное", "входящих личных", "входящих личных") + " (≈ " + number(e.batches_7d) +
         " " + plural(e.batches_7d, "разбор", "разбора", "разборов") + ") и " + number(e.mentions_7d) + " " +
         plural(e.mentions_7d, "обращение", "обращения", "обращений") + " в группах. Это ≈ " + trNum(e.per_day, 1) +
         " разбора в сутки, ≈ " + number(Math.round(e.tokens_per_day / 1000)) + " тыс. токенов в сутки, ≈ " +
         trNum(e.tokens_per_month / 1e6, 1) + " млн в месяц. Цена — по тарифу вашей модели: при 1 $ за миллион " +
         "токенов это около " + trNum(e.tokens_per_month / 1e6, 1) + " $ в месяц.");
    var c = d.counts;
    text("tr-stats", on ? "За сутки: разобрано " + number(c.analyzed_24h) + ", показано " + number(c.shown_24h + c.reminded_24h) +
         ", ждут ответа " + number(c.waiting) + (c.failed_24h ? ", модель не ответила " + number(c.failed_24h) +
         " (они пришли обычными уведомлениями)" : "") + "." : "");
    tr.people = d.people.slice();
    tr.chats = d.chats.slice();
    drawTrPeople();
    drawTrChats();
    var list = $("tr-spheres");
    list.textContent = "";
    d.spheres.forEach(function (sp) {
      list.appendChild(el("label", { class: "check" }, [
        el("input", { type: "checkbox", value: String(sp.id), checked: s.important_spheres.indexOf(sp.id) >= 0 }),
        el("span", { text: sp.title })]));
    });
    show("tr-spheres-empty", !d.spheres.length);
    var remind = $("tr-remind");
    remind.textContent = "";
    d.remind_choices.forEach(function (m) {
      remind.appendChild(el("option", { value: String(m), text: trHours(m), selected: m === s.remind_minutes }));
    });
  }

  function trChatName(c) { return c.title || "Группа № " + c.id; }

  function drawTrPeople() {
    drawTrList("tr-people", tr.people, personName, "Пока никого. Найдите человека ниже.", drawTrPeople);
  }

  function drawTrChats() {
    drawTrList("tr-chats", tr.chats, trChatName,
               "Пока нет. «Не важно» под карточкой из группы добавит её сюда; найти группу можно и ниже.", drawTrChats);
  }

  function drawTrList(id, items, name, empty, redraw) {
    var list = $(id);
    list.textContent = "";
    if (!items.length) list.appendChild(el("li", { class: "mem-chat mem-note", text: empty }));
    items.forEach(function (item, i) {
      var select = el("select", { "aria-label": "Отметка: " + name(item), onchange: function () { item.level = this.value; } },
        TR_LEVELS.map(function (l) { return el("option", { value: l[0], text: l[1], selected: item.level === l[0] }); }));
      list.appendChild(el("li", { class: "mem-chat" }, [
        el("span", { class: "tr-name" }, [name(item)]), select,
        el("button", { class: "btn ghost small-btn", type: "button", text: "Убрать", "aria-label": "Убрать " + name(item),
          onclick: function () { items.splice(i, 1); redraw(); } })
      ]));
    });
  }

  function trFound(box, found, items, key, name, added) {
    found.forEach(function (x) {
      var chosen = items.some(function (y) { return y[key] === x[key]; });
      box.appendChild(el("li", { class: "mem-chat" }, [
        el("span", { class: "tr-name" }, [name(x)]),
        chosen ? el("span", { class: "badge ok", text: "в списке" }) : el("span", { class: "tr-add" }, TR_LEVELS.map(function (l) {
          return el("button", { class: "btn ghost small-btn", type: "button", text: l[1],
            onclick: function () { items.push(Object.assign({}, x, { level: l[0] })); added(); } });
        }))
      ]));
    });
  }

  function searchTrPeople() {
    var q = $("tr-people-q").value.trim(), found = $("tr-people-found");
    found.textContent = "";
    if (!q) return;
    call("GET", "notify/people?limit=8&q=" + encodeURIComponent(q)).then(function (r) {
      found.textContent = "";
      if (!r.ok) { found.appendChild(el("li", { class: "mem-note", text: r.error })); return; }
      if (!r.data.items.length) { found.appendChild(el("li", { class: "mem-chat mem-note", text: "Ничего не нашлось среди ваших личных чатов." })); return; }
      trFound(found, r.data.items, tr.people, "tg_id", personName, function () { drawTrPeople(); searchTrPeople(); });
    });
  }

  function searchTrChats() {
    var q = $("tr-chats-q").value.trim(), found = $("tr-chats-found");
    found.textContent = "";
    if (!q) return;
    call("GET", "watch/chats?limit=8&q=" + encodeURIComponent(q)).then(function (r) {
      found.textContent = "";
      if (!r.ok) { found.appendChild(el("li", { class: "mem-note", text: r.error })); return; }
      var groups = r.data.items.filter(function (c) { return c.type !== "public_channel" && c.type !== "private_channel"; });
      if (!groups.length) { found.appendChild(el("li", { class: "mem-chat mem-note", text: "Такой группы нет в архиве." })); return; }
      trFound(found, groups, tr.chats, "id", trChatName, function () { drawTrChats(); searchTrChats(); });
    });
  }

  function triageBody() {
    var body = { remind_minutes: Number($("tr-remind").value),
                 important_spheres: Array.prototype.map.call(document.querySelectorAll("#tr-spheres input:checked"), function (n) { return Number(n.value); }) };
    Object.keys(TR_PEOPLE).forEach(function (level) {
      body[TR_PEOPLE[level]] = tr.people.filter(function (p) { return p.level === level; }).map(function (p) { return p.tg_id; });
      body[TR_CHATS[level]] = tr.chats.filter(function (c) { return c.level === level; }).map(function (c) { return c.id; });
    });
    return body;
  }

  function showTriage() { return refreshTriage().then(drawTriage); }

  function wireTriage() {
    $("tr-people-q").addEventListener("input", function () { clearTimeout(tr.timer); tr.timer = setTimeout(searchTrPeople, 300); });
    $("tr-chats-q").addEventListener("input", function () { clearTimeout(tr.chatTimer); tr.chatTimer = setTimeout(searchTrChats, 300); });
    $("tr-enable").addEventListener("click", function () {
      var button = this;
      ask(button, "Включить разбор входящих?", { yes: "Прислать карточку в бот",
        note: "Новые входящие и последние сообщения их чатов начнут уходить провайдеру вашей модели. Включается только нажатием «Да, сделать» в боте согласований." }).then(function (yes) {
        if (!yes) return;
        busy(button, true);
        call("POST", "triage/enable", {}).then(function (r) {
          busy(button, false);
          if (!r.ok) { toast(r.error, true); return; }
          tr.data = r.data;
          drawTriage();
          refreshTriage();
          toast("Карточка в боте согласований. Разбор включится, когда вы нажмёте «Да, сделать».");
        });
      });
    });
    $("tr-disable").addEventListener("click", function () {
      var button = this;
      busy(button, true);
      call("PUT", "triage", { enabled: false }).then(function (r) {
        busy(button, false);
        if (!r.ok) { toast(r.error, true); return; }
        tr.data = r.data;
        drawTriage();
        refreshTriage();
        toast("Разбор выключен: снова приходят уведомления «Вам написали».");
      });
    });
    $("triage-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var save = $("triage-save");
      busy(save, true);
      note("triage-error", "warn", "");
      call("PUT", "triage", triageBody()).then(function (r) {
        busy(save, false);
        if (!r.ok) { note("triage-error", "warn", r.error); return; }
        tr.data = r.data;
        drawTriage();
        toast("Правила разбора сохранены.");
      });
    });
  }

  /* ------------------------------------------------------- репетиция ответов (Р-85) */

  var rh = { data: null, people: [], timer: null };

  function refreshRehearsal() {
    return call("GET", "rehearsal", undefined, true).then(function (r) {
      if (!r.ok) return;
      rh.data = r.data;
      var s = r.data.settings;
      pill("rehearsal", !r.data.own_bot ? "нет бота" : !s.enabled ? "выключена" :
           !s.people.length ? "никого в списке" : "включена · " + s.people.length + " " + plural(s.people.length, "человек", "человека", "человек"),
           !r.data.own_bot ? "todo" : s.enabled && s.people.length ? "done" : "");
    });
  }

  function drawRehearsal() {
    var d = rh.data;
    if (!d) return;
    var s = d.settings;
    show("rh-nobot", !d.own_bot);
    show("rh-noaccount", d.own_bot && !d.main_account);
    $("rh-enabled").checked = s.enabled;
    $("rh-cap").value = String(s.daily_cap);
    $("rh-pause").value = String(s.pause_seconds);
    rh.people = d.people.slice();
    drawRhPeople();
    drawRhStats(d.stats);
  }

  function drawRhStats(st) {
    var box = $("rh-stats");
    box.textContent = "";
    function tile(value, label, kind) {
      box.appendChild(el("div", { class: "rh-tile" + (kind ? " " + kind : "") }, [
        el("span", { class: "rh-num", text: number(value) }), el("span", { class: "rh-label", text: label })]));
    }
    tile(st.shown, plural(st.shown, "карточка", "карточки", "карточек"));
    tile(st.good, "годится", "ok");
    tile(st.almost, "почти", "mid");
    tile(st.bad, "не годится", "bad");
    tile(st.unrated, "без оценки");
    var parts = [];
    if (st.shown) {
      parts.push("Ассистент предложил ответ — " + number(st.reply) + ", не стал бы отвечать — " + number(st.decline) +
                 ", спросил бы вас — " + number(st.ask_owner) + (st.need_source ? ", не хватило сведений — " + number(st.need_source) : "") + ".");
    } else {
      parts.push("Карточек пока не было.");
    }
    if (st.limited) parts.push("Не уложилось в предел или паузу: " + number(st.limited) + ".");
    if (st.failed) parts.push("Не получилось (модель не ответила): " + number(st.failed) + ".");
    parts.push("Сегодня уже: " + number(st.asked_24h) + " из " + number(rh.data.settings.daily_cap) + ".");
    text("rh-stats-note", parts.join(" "));
  }

  function drawRhPeople() {
    var list = $("rh-people");
    list.textContent = "";
    if (!rh.people.length) list.appendChild(el("li", { class: "mem-chat mem-note", text: "Пока никого. Найдите человека ниже." }));
    rh.people.forEach(function (p, i) {
      list.appendChild(el("li", { class: "mem-chat" }, [
        el("span", {}, [personName(p), el("span", { class: "mem-meta", text: " · № " + p.tg_id })]),
        el("button", { class: "btn ghost small-btn", type: "button", text: "Убрать", "aria-label": "Убрать " + personName(p),
          onclick: function () { rh.people.splice(i, 1); drawRhPeople(); } })
      ]));
    });
  }

  function searchRhPeople() {
    var q = $("rh-people-q").value.trim(), found = $("rh-people-found");
    found.textContent = "";
    if (!q) return;
    call("GET", "notify/people?limit=8&q=" + encodeURIComponent(q)).then(function (r) {
      found.textContent = "";
      if (!r.ok) { found.appendChild(el("li", { class: "mem-note", text: r.error })); return; }
      if (!r.data.items.length) found.appendChild(el("li", { class: "mem-chat mem-note", text: "Ничего не нашлось среди ваших личных чатов." }));
      r.data.items.forEach(function (p) {
        var chosen = rh.people.some(function (x) { return x.tg_id === p.tg_id; });
        found.appendChild(el("li", { class: "mem-chat" }, [
          el("span", {}, [personName(p), p.username && p.name ? el("span", { class: "mem-meta", text: " · @" + p.username }) : null]),
          chosen ? el("span", { class: "badge ok", text: "выбран" }) : el("button", { class: "btn ghost small-btn", type: "button", text: "Добавить",
            onclick: function () { rh.people.push(p); drawRhPeople(); searchRhPeople(); } })
        ]));
      });
    });
  }

  function showRehearsal() { return refreshRehearsal().then(drawRehearsal); }

  function wireRehearsal() {
    $("rehearsal-details").addEventListener("toggle", function () { if ($("rehearsal-details").open) showRehearsal(); });
    $("rh-people-q").addEventListener("input", function () { clearTimeout(rh.timer); rh.timer = setTimeout(searchRhPeople, 300); });
    $("rehearsal-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var save = $("rehearsal-save");
      var body = { enabled: $("rh-enabled").checked, people: rh.people.map(function (p) { return p.tg_id; }),
                   daily_cap: Math.round(Number($("rh-cap").value)), pause_seconds: Math.round(Number($("rh-pause").value)) };
      busy(save, true);
      note("rehearsal-error", "warn", "");
      call("PUT", "rehearsal", body).then(function (r) {
        busy(save, false);
        if (!r.ok) { note("rehearsal-error", "warn", r.error); return; }
        rh.data = r.data;
        drawRehearsal();
        refreshRehearsal();
        toast(!body.enabled ? "Репетиция выключена." : body.people.length ? "Репетиция включена. Ничего не отправляется." :
              "Репетиция включена, но в списке никого — добавьте людей.");
      });
    });
  }

  /* ------------------------------------------------------- наблюдатель (Р-84) */

  var wt = { data: null, editing: null, hits: [], hitsRule: null, chatTimer: null };
  var HIT_KIND = { relevant: "ok", not_relevant: "", duplicate: "", limited: "warn", failed: "warn", no_answer: "warn", checking: "" };

  function refreshWatch() {
    return call("GET", "watch", undefined, true).then(function (r) {
      if (!r.ok) return;
      wt.data = r.data;
      var on = r.data.rules.filter(function (x) { return x.enabled; }).length;
      pill("watch", r.data.rules.length ? "правил: " + on + (on < r.data.rules.length ? " из " + r.data.rules.length : "") : "", "");
    });
  }

  function loadHits() {
    return call("GET", "watch/hits?limit=20" + (wt.hitsRule ? "&rule_id=" + wt.hitsRule : "")).then(function (r) { if (r.ok) wt.hits = r.data.items; });
  }

  function showWatch() { return Promise.all([refreshWatch(), loadHits()]).then(drawWatch); }

  function wordsLine(list, limit) {
    return list.slice(0, limit).map(function (w) { return "«" + w + "»"; }).join(", ") + (list.length > limit ? " и ещё " + (list.length - limit) : "");
  }

  function ruleWhere(rule) {
    var parts = [];
    if (rule.spheres.length) parts.push((rule.spheres.length > 1 ? "сферы " : "сфера ") + rule.spheres.map(function (s) { return "«" + (s.title || "удалена") + "»"; }).join(", "));
    if (rule.chats.length) parts.push(rule.chats.length > 3 ? rule.chats.length + " " + plural(rule.chats.length, "чат", "чата", "чатов")
      : rule.chats.map(function (c) { return "«" + (c.title || "без названия") + "»"; }).join(", "));
    return parts.join(" и ") || "нигде";
  }

  function drawWatch() {
    var d = wt.data;
    if (!d) return;
    var tpl = $("watch-templates");
    tpl.textContent = "";
    tpl.appendChild(el("span", { class: "hint", text: "Готовые правила:" }));
    d.templates.forEach(function (t) {
      tpl.appendChild(el("button", { class: "btn ghost small-btn", type: "button", text: "+ " + t.name,
        onclick: function () { editRule({ name: t.name, description: t.description, keywords: t.keywords.slice(), regexes: [], use_lemmas: t.use_lemmas,
                                          chat_ids: [], chats: [], sphere_ids: [], enabled: true, ask_name: !!t.ask_name }); } }));
    });
    var list = $("watch-list");
    list.textContent = "";
    if (!d.rules.length) list.appendChild(el("li", { class: "mem-note sph-empty", text: "Правил пока нет. Начните с готового — выше — или заведите своё." }));
    d.rules.forEach(function (rule) { list.appendChild(ruleItem(rule)); });
    drawRuleForm();
    drawHits();
  }

  function ruleItem(rule) {
    var meta = [ruleWhere(rule)];
    if (rule.hits.notified_7d) meta.push("за неделю сообщено: " + rule.hits.notified_7d);
    else if (rule.hits.total) meta.push("срабатываний: " + rule.hits.total);
    return el("li", { class: "sph watch-rule" + (rule.enabled ? "" : " off") }, [
      el("div", { class: "sph-head" }, [
        el("div", { class: "sph-main" }, [
          el("span", { class: "sph-name" }, [rule.name, " ", el("span", { class: rule.enabled ? "badge ok" : "badge", text: rule.enabled ? "включено" : "выключено" })]),
          el("span", { class: "sph-desc", text: "Слова: " + (wordsLine(rule.keywords, 8) || "—") + (rule.regexes.length ? "; выражений: " + rule.regexes.length : "") }),
          el("span", { class: "mem-meta", text: "Где: " + meta.join(" · ") })
        ])
      ]),
      el("div", { class: "row" }, [
        el("button", { class: "btn ghost small-btn", type: "button", text: "Изменить", onclick: function () { editRule(rule); } }),
        el("button", { class: "btn ghost small-btn", type: "button", text: rule.enabled ? "Выключить" : "Включить",
          onclick: function () { saveRule(rule.id, { enabled: !rule.enabled }, this, rule.enabled ? "Правило выключено." : "Правило включено."); } }),
        el("button", { class: "btn ghost small-btn", type: "button", text: "Срабатывания",
          onclick: function () { wt.hitsRule = wt.hitsRule === rule.id ? null : rule.id; loadHits().then(drawHits); } }),
        el("button", { class: "btn ghost small-btn danger", type: "button", text: "Удалить",
          onclick: function () {
            var button = this;
            ask(button, "Удалить правило «" + rule.name + "» вместе с журналом его срабатываний?", {
              yes: "Удалить правило", danger: true,
              note: "Если нужно только остановить его — нажмите «Выключить»."
            }).then(function (ok) {
              if (!ok) return;
              busy(button, true);
              call("DELETE", "watch/" + rule.id).then(function (r) {
                busy(button, false);
                if (!r.ok) { toast(r.error, true); return; }
                if (wt.hitsRule === rule.id) wt.hitsRule = null;
                toast("Правило удалено.");
                showWatch();
              });
            });
          } })
      ])
    ]);
  }

  function editRule(rule) {
    wt.editing = JSON.parse(JSON.stringify(rule));
    wt.editing.chats = (rule.chats || []).filter(Boolean).map(function (c) { return { id: c.id, title: c.title }; });
    drawRuleForm();
    var form = $("watch-form");
    form.scrollIntoView({ block: "start" });
    var first = form.querySelector(rule.ask_name ? "#wr-keywords" : "#wr-name");
    if (first) first.focus();
  }

  function drawRuleForm() {
    var form = $("watch-form"), r = wt.editing;
    form.textContent = "";
    show(form, !!r);
    show("watch-new", !r);
    if (!r) return;
    var spheres = (wt.data && wt.data.spheres) || [];
    var chatList = el("ul", { class: "mem-items", id: "wr-chats" });
    function drawChats() {
      chatList.textContent = "";
      if (!r.chats.length) chatList.appendChild(el("li", { class: "mem-chat mem-note", text: "Чаты не выбраны." }));
      r.chats.forEach(function (c, i) {
        chatList.appendChild(el("li", { class: "mem-chat" }, [el("span", { text: c.title || "без названия" }),
          el("button", { class: "btn ghost small-btn", type: "button", text: "Убрать", onclick: function () { r.chats.splice(i, 1); drawChats(); } })]));
      });
    }
    drawChats();
    var search = el("input", { type: "search", id: "wr-chat-q", autocomplete: "off", placeholder: "Название группы или канала" });
    var found = el("ul", { class: "mem-items sph-found" });
    search.addEventListener("input", function () {
      clearTimeout(wt.chatTimer);
      wt.chatTimer = setTimeout(function () {
        var q = search.value.trim();
        found.textContent = "";
        if (!q) return;
        call("GET", "watch/chats?limit=8&q=" + encodeURIComponent(q)).then(function (x) {
          found.textContent = "";
          if (!x.ok) { found.appendChild(el("li", { class: "mem-note", text: x.error })); return; }
          if (!x.data.items.length) found.appendChild(el("li", { class: "mem-chat mem-note", text: "Не нашлось среди читаемых групп и каналов." }));
          x.data.items.forEach(function (c) {
            var inside = r.chats.some(function (y) { return y.id === c.id; });
            found.appendChild(el("li", { class: "mem-chat" }, [el("span", {}, [c.title || "без названия", el("span", { class: "mem-meta", text: " · " + (/channel$/.test(c.type || "") ? "канал" : "группа") })]),
              inside ? el("span", { class: "badge ok", text: "выбран" }) : el("button", { class: "btn ghost small-btn", type: "button", text: "Добавить",
                onclick: function () { r.chats.push({ id: c.id, title: c.title }); drawChats(); search.value = ""; found.textContent = ""; search.focus(); } })]));
          });
        });
      }, 300);
    });
    var sphereBoxes = spheres.map(function (sp) {
      return el("label", { class: "check" }, [el("input", { type: "checkbox", value: String(sp.id), checked: r.sphere_ids.indexOf(sp.id) >= 0 }), el("span", { text: sp.title })]);
    });
    form.appendChild(el("h3", { class: "mem-form-title", text: r.id ? "Правило «" + r.name + "»" : "Новое правило" }));
    form.appendChild(el("div", { class: "field inline" }, [el("label", { for: "wr-name", text: "Название" }),
      el("input", { id: "wr-name", type: "text", maxlength: "100", value: r.name || "" })]));
    form.appendChild(el("div", { class: "field" }, [el("label", { for: "wr-keywords", text: "Слова и выражения" }),
      el("textarea", { id: "wr-keywords", rows: "2", class: "wr-text", text: (r.keywords || []).join(", "),
                       placeholder: r.ask_name ? "Ваше имя и варианты: Евгений, Женя, Эрман" : "Через запятую" }),
      el("span", { class: "hint", text: r.ask_name ? "Впишите своё имя и как вас ещё называют — через запятую." : "Через запятую. Регистр и «ё» не важны." })]));
    form.appendChild(el("label", { class: "check" }, [el("input", { type: "checkbox", id: "wr-lemmas", checked: r.use_lemmas !== false }),
      el("span", { text: "Учитывать формы слов («счёт» найдёт «счета», «счетов»)" })]));
    form.appendChild(el("div", { class: "field" }, [el("label", { for: "wr-description", text: "Что для вас важно — это прочитает модель" }),
      el("textarea", { id: "wr-description", rows: "3", maxlength: "1000", class: "wr-text", text: r.description || "" })]));
    form.appendChild(el("fieldset", { class: "mem-fieldset" }, [el("legend", { text: "Где искать" }),
      spheres.length ? el("div", { class: "sph-pick-list", id: "wr-spheres" }, sphereBoxes) : el("p", { class: "hint", text: "Сфер пока нет — можно выбрать отдельные чаты." }),
      spheres.length ? el("p", { class: "hint", text: "Сфера — все её группы и каналы, в том числе добавленные в неё позже. Личные чаты сферы наблюдатель не смотрит." }) : null,
      el("span", { class: "hint", text: "Отдельные группы и каналы:" }), chatList,
      el("div", { class: "field" }, [el("label", { for: "wr-chat-q", text: "Добавить группу или канал" }), search]), found]));
    var regex = el("details", { class: "sub" }, [el("summary", { text: "Для опытных: регулярные выражения" }),
      el("div", { class: "field" }, [el("label", { for: "wr-regexes", text: "По одному в строке" }),
        el("textarea", { id: "wr-regexes", rows: "2", class: "wr-text", text: (r.regexes || []).join("\n") })])]);
    if ((r.regexes || []).length) regex.open = true;
    form.appendChild(regex);
    form.appendChild(el("label", { class: "check" }, [el("input", { type: "checkbox", id: "wr-enabled", checked: r.enabled !== false }), el("span", { text: "Правило включено" })]));
    form.appendChild(el("div", { class: "row" }, [el("button", { class: "btn", type: "submit", id: "wr-save", text: r.id ? "Сохранить" : "Завести правило" }),
      el("button", { class: "btn ghost", type: "button", text: "Отмена", onclick: function () { wt.editing = null; note("watch-error", "warn", ""); drawRuleForm(); } })]));
  }

  function ruleBody() {
    var r = wt.editing;
    var split = function (value, sep) { return value.split(sep).map(function (x) { return x.trim(); }).filter(Boolean); };
    return {
      name: $("wr-name").value.trim(), description: $("wr-description").value.trim(),
      keywords: split($("wr-keywords").value, ","), regexes: split($("wr-regexes").value, "\n"),
      use_lemmas: $("wr-lemmas").checked, enabled: $("wr-enabled").checked,
      chat_ids: r.chats.map(function (c) { return c.id; }),
      sphere_ids: Array.prototype.map.call(document.querySelectorAll("#wr-spheres input:checked"), function (n) { return Number(n.value); })
    };
  }

  function saveRule(id, body, button, done) {
    busy(button, true);
    note("watch-error", "warn", "");
    return call(id ? "PUT" : "POST", id ? "watch/" + id : "watch", body).then(function (r) {
      busy(button, false);
      if (!r.ok) { if (wt.editing) note("watch-error", "warn", r.error); else toast(r.error, true); return r; }
      wt.editing = null;
      toast(done);
      showWatch();
      return r;
    });
  }

  function drawHits() {
    var list = $("watch-hits");
    list.textContent = "";
    var rule = wt.hitsRule && wt.data ? wt.data.rules.filter(function (x) { return x.id === wt.hitsRule; })[0] : null;
    text(document.querySelector("#watch-hits-box h4"), rule ? "Срабатывания правила «" + rule.name + "»" : "Последние срабатывания");
    if (!wt.hits.length) { list.appendChild(el("li", { class: "mem-chat mem-note", text: "Пока ничего не нашлось." })); return; }
    wt.hits.forEach(function (h) {
      var line = [when(h.at), h.rule_name, h.chat_title || "без названия"].join(" · ");
      list.appendChild(el("li", { class: "mem-item watch-hit" }, [
        el("div", { class: "mem-item-main" }, [
          el("span", {}, [el("span", { class: "badge " + (HIT_KIND[h.status] || ""), text: h.status_text + (h.notified ? " · сообщено" : "") }), " ", el("span", { class: "mem-meta", text: line })]),
          el("span", { class: "small", text: "Совпало: " + wordsLine(h.matched, 6) + (h.reason ? ". Модель: " + h.reason : "") }),
          h.link ? el("a", { href: h.link, target: "_blank", rel: "noopener noreferrer", class: "small", text: "Открыть в Telegram" }) : null
        ])
      ]));
    });
  }

  function wireWatch() {
    $("watch-details").addEventListener("toggle", function () { if ($("watch-details").open) showWatch(); });
    $("watch-new").addEventListener("click", function () {
      editRule({ name: "", description: "", keywords: [], regexes: [], use_lemmas: true, chat_ids: [], chats: [], sphere_ids: [], enabled: true });
    });
    $("watch-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var r = wt.editing;
      saveRule(r.id, ruleBody(), $("wr-save"), r.id ? "Правило сохранено." : "Правило заведено.");
    });
  }

  /* ------------------------------------------------ повтор после сбоя (Р-84) */

  var rt = { data: null };

  function refreshRetry() {
    return call("GET", "retry", undefined, true).then(function (r) {
      if (!r.ok) return;
      rt.data = r.data;
      var d = r.data, last = d.last, any = d.media + d.requests > 0;
      var recent = last && Date.now() - new Date(last.at).getTime() < 3 * 86400000;
      show("s-retry", any || !!recent);
      pill("retry", any ? "можно повторить: " + number(d.media + d.requests) : "", any ? "todo" : "");
      var items = [];
      if (d.media) items.push("Фото и документы: " + number(d.media) + (d.media_enabled ? " — разберутся заново в ближайшие минуты." : " — разбор сейчас выключен, они не вернутся, пока вы его не включите."));
      if (d.requests) items.push("Разбор переписки (договорённости, факты, проекты): " + number(d.requests) + " " + plural(d.requests, "запрос", "запроса", "запросов") + " — в следующий прогон, ночью.");
      if (d.pages) items.push("Страницы памяти без свежей сводки: " + number(d.pages) + " — пересоберутся сами, повторять не нужно.");
      if (!any) items.push("Сейчас повторять нечего.");
      var list = $("retry-list");
      list.textContent = "";
      items.forEach(function (t) { list.appendChild(el("li", { text: t })); });
      $("retry-run").disabled = !any;
      ["retry-since-row", "retry-run-row", "retry-cost"].forEach(function (id) { show(id, any); });
      text("retry-lead", any ? "Пока задания модели не выполнялись, часть работы честно сдалась после нескольких попыток."
                             : "Всё, что не получилось из-за сбоя модели, уже возвращено в работу.");
      note("retry-status", "", last ? "Последний повтор " + when(last.at) + (last.by === "cli" ? " (из терминала)" : "") + ": вложений — " +
           number(last.media) + ", запросов разбора — " + number(last.requests) + "." : "");
    });
  }

  function wireRetry() {
    $("retry-run").addEventListener("click", function () {
      var d = rt.data || { media: 0, requests: 0 }, button = this;
      ask(button, "Повторить то, что не получилось?", {
        yes: "Повторить",
        note: "Фото и документы (" + d.media + ") и разбор переписки (" + d.requests + ") снова уйдут провайдеру модели и потратят запросы."
      }).then(function (ok) {
        if (!ok) return;
        var since = $("retry-since").value;
        act(button, "retry-error", "POST", "retry", since ? { since: since } : {}, function (out) {
          toast("Возвращено в работу: вложений — " + out.media + ", запросов разбора — " + out.requests + ".");
          refreshRetry();
        });
      });
    });
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
      ask(this, "Завершить все входы в «Настройки Штурмана» — на всех устройствах?", {
        yes: "Выйти везде", danger: true,
        note: "Войти снова можно будет по коду из бота согласований или по новой ссылке от того, кто обслуживает сервер."
      }).then(function (ok) {
        if (ok) call("POST", "logout-all", {}, true).then(function () { saveKey(""); loginScreen(""); });
      });
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

  wireLogin(); wireHalt(); wireCodeLogin(); wireMode(); wireBot(); wireKeys(); wireAccounts(); wireChats(); wireImport(); wireBusiness(); wireLlm(); wireMedia(); wireSpheres(); wireNotify(); wireRehearsal(); wireWatch(); wireRetry(); wireMemory(); wireSession();

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
