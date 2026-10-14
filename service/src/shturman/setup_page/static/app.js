/* Mini App «Штурмана» в боте согласований. Обычный скрипт без сборки и без библиотек.
 *
 * Скрипт Telegram (telegram.org/js/telegram-web-app.js) не подключается. Он не закреплён по
 * версии и меняется без предупреждения, поэтому проверить его содержимое (SRI) нельзя, а
 * исполнять чужой код на странице, которая держит вход в решения владельца, не хотим. Нам от
 * него нужно немногое, и это сделано здесь же:
 *   - данные входа: Telegram кладёт их в часть адреса после «#» (tgWebAppData), там же версия,
 *     платформа и цвета темы — так их читает и скрипт Telegram (urlParseHashParams);
 *   - сообщения Telegram о готовности, «развернуть», «добавить на главный экран» — тем же
 *     способом, что у скрипта: TelegramWebviewProxy.postEvent на телефоне, external.notify
 *     в старых клиентах, postMessage в рамке Telegram Web — только на https://web.telegram.org.
 * Если этих путей нет (страница открыта не в Telegram), приложение просто не входит.
 *
 * Вход. Строка tgWebAppData один раз уходит телом POST на сервер, который проверяет подпись
 * токеном бота согласований, и сразу убирается из адресной строки. В ответ сервер один раз отдаёт
 * ключ сессии приложения: он живёт в памяти страницы и в sessionStorage (переживает перезагрузку
 * рамки Telegram Web, но не закрытие приложения) и уходит заголовком X-Shturman-App. Cookie нет.
 *
 * Два экрана: «Согласования» — то, что ждёт решения, с теми же кнопками, что в боте, — и «Чаты» —
 * какие чаты читает ассистент (то же, что шаг «Какие чаты читать» на странице «Переписка», Р-80).
 * Список чатов сервис отдаёт из памяти; в Telegram он ходит только по кнопке «Обновить список из
 * Telegram». Если Telegram велит подождать — отсчёт и один повтор сам, как на странице.
 *
 * Всё, что приходит с сервера, выводится только через textContent: в карточках и названиях чатов
 * чужой текст.
 */
(function () {
  "use strict";

  var API = "../api/app/";
  var KEY_NAME = "shturman-app-session";
  var THEME_NAME = "shturman-app-theme";
  var TAB_NAME = "shturman-app-tab";
  var TELEGRAM_WEB = "https://web.telegram.org";
  var key = "";
  var params = parseHash(location.hash);
  var initData = params.tgWebAppData || "";       // только в памяти: годится один раз
  var busy = false;
  var toastTimer = null;
  var listTimer = null;
  var listShown = false;                         // список уже был на экране хотя бы раз
  var signedIn = false;                          // на экране — разделы, а не вход или его неудача
  var tab = "approvals";                         // открытый раздел: approvals | chats

  function $(id) { return document.getElementById(id); }
  function show(id, on) { $(id).hidden = !on; }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  /* ------------------------------------------------------------ параметры запуска */

  function decode(value) {
    try { return decodeURIComponent(value.replace(/\+/g, "%20")); } catch (e) { return value; }
  }

  function parseHash(hash) {
    var out = {};
    hash = (hash || "").replace(/^#/, "");
    var q = hash.indexOf("?");
    if (q >= 0) hash = hash.slice(q + 1);
    if (hash.indexOf("=") < 0) return out;
    hash.split("&").forEach(function (pair) {
      var at = pair.indexOf("=");
      if (at <= 0) return;
      out[decode(pair.slice(0, at))] = decode(pair.slice(at + 1));
    });
    return out;
  }

  function versionAtLeast(want) {
    var have = String(params.tgWebAppVersion || "0").split(".");
    var need = want.split(".");
    for (var i = 0; i < Math.max(have.length, need.length); i++) {
      var a = parseInt(have[i] || "0", 10) || 0, b = parseInt(need[i] || "0", 10) || 0;
      if (a !== b) return a > b;
    }
    return true;
  }

  /* ------------------------------------------------------------ связь с Telegram */

  var inFrame = false;
  try { inFrame = window.parent !== window; } catch (e) { inFrame = true; }

  function postEvent(type, data) {
    var payload = data === undefined ? "" : data;
    try {
      if (window.TelegramWebviewProxy && typeof window.TelegramWebviewProxy.postEvent === "function") {
        window.TelegramWebviewProxy.postEvent(type, JSON.stringify(payload));
      } else if (window.external && "notify" in window.external) {
        window.external.notify(JSON.stringify({ eventType: type, eventData: payload }));
      } else if (inFrame) {
        window.parent.postMessage(JSON.stringify({ eventType: type, eventData: payload }), TELEGRAM_WEB);
      }
    } catch (e) { /* вне Telegram сообщать некому */ }
  }

  function receiveEvent(type, data) {
    if (type === "theme_changed" && data && data.theme_params) {
      applyTheme(data.theme_params, true);
    } else if (type === "home_screen_checked" && data) {
      show("home-screen", data.status === "missed" || data.status === "unknown");
    } else if (type === "home_screen_added") {
      show("home-screen", false);
    }
  }

  // Так приложения Telegram на телефоне и компьютере передают события странице.
  window.Telegram = { WebView: { receiveEvent: receiveEvent } };
  window.TelegramGameProxy_receiveEvent = receiveEvent;

  if (inFrame) {
    window.addEventListener("message", function (event) {
      if (event.origin !== TELEGRAM_WEB || event.source !== window.parent) return;
      var message;
      try { message = JSON.parse(event.data); } catch (e) { return; }
      if (!message || typeof message.eventType !== "string") return;
      if (message.eventType === "reload_iframe") {
        postEvent("iframe_will_reload");
        location.reload();
      } else {
        receiveEvent(message.eventType, message.eventData);
      }
    });
    postEvent("iframe_ready", { reload_supported: true });
  }

  /* ------------------------------------------------------------ тема */

  var COLOR = /^#[0-9a-fA-F]{6}$/;

  function applyTheme(theme, remember) {
    if (!theme || typeof theme !== "object") return;
    Object.keys(theme).forEach(function (name) {
      if (!/^[a-z_]{1,40}$/.test(name) || !COLOR.test(String(theme[name]))) return;
      document.documentElement.style.setProperty("--tg-theme-" + name.replace(/_/g, "-"), theme[name]);
    });
    if (remember) {
      try { sessionStorage.setItem(THEME_NAME, JSON.stringify(theme)); } catch (e) { /* не страшно */ }
    }
  }

  (function () {
    var theme = null;
    if (params.tgWebAppThemeParams) {
      try { theme = JSON.parse(params.tgWebAppThemeParams); } catch (e) { theme = null; }
    }
    if (!theme) {
      try { theme = JSON.parse(sessionStorage.getItem(THEME_NAME) || "null"); } catch (e) { theme = null; }
    }
    applyTheme(theme, Boolean(params.tgWebAppThemeParams));
  })();

  // Данные входа больше не нужны в адресной строке: они уйдут на сервер один раз.
  if (location.hash) {
    try { history.replaceState(null, "", location.pathname + location.search); } catch (e) { /* пусть */ }
  }

  /* ------------------------------------------------------------ сессия и запросы */

  function loadKey() {
    try { return sessionStorage.getItem(KEY_NAME) || ""; } catch (e) { return ""; }
  }
  function saveKey(value) {
    key = value || "";
    try {
      if (key) sessionStorage.setItem(KEY_NAME, key); else sessionStorage.removeItem(KEY_NAME);
    } catch (e) { /* останется в памяти */ }
  }

  function request(method, path, body) {
    var headers = {};
    if (key) headers["X-Shturman-App"] = key;
    var options = { method: method, headers: headers, credentials: "omit", cache: "no-store" };
    if (method !== "GET") {
      headers["Content-Type"] = "application/json";
      headers["X-Shturman-Setup"] = "1";
      options.body = JSON.stringify(body || {});
    }
    return fetch(API + path, options).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        return { status: response.status, data: data || {} };
      });
    });
  }

  // Экран целиком: ожидание, вне Telegram, отказ во входе, сбой — или разделы («main»).
  function screen(name) {
    signedIn = name === "main";
    ["screen-wait", "screen-outside", "screen-failed", "screen-error"].forEach(function (id) {
      show(id, id === name);
    });
    show("tabs", signedIn);
    show("screen-approvals", signedIn && tab === "approvals");
    show("screen-chats", signedIn && tab === "chats");
    show("refresh", signedIn && tab === "approvals");
  }

  function openTab(name) {
    tab = name === "chats" ? "chats" : "approvals";
    try { sessionStorage.setItem(TAB_NAME, tab); } catch (e) { /* не страшно */ }
    ["approvals", "chats"].forEach(function (one) {
      $("tab-" + one).setAttribute("aria-selected", one === tab ? "true" : "false");
    });
    if (signedIn) screen("main");
    if (tab === "chats") chatsOpened();
  }

  // Ключ сессии больше не действует (срок, «выйти везде», смена владельца): войти заново можно,
  // только открыв приложение снова — данные входа от Telegram годятся один раз.
  function sessionLost(data) {
    saveKey("");
    $("failed-text").textContent = (data && data.error) || "Вход устарел. Закройте приложение и откройте его снова.";
    screen("screen-failed");
  }

  function toast(message) {
    var node = $("toast");
    node.textContent = message;
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { node.hidden = true; }, 4000);
  }

  function signIn() {
    var data = initData;
    initData = "";                       // второй раз те же данные сервер всё равно не примет
    if (!data) {
      screen(params.tgWebAppVersion ? "screen-failed" : "screen-outside");
      return Promise.resolve(false);
    }
    return request("POST", "login", { init_data: data }).then(function (got) {
      if (got.status === 200 && got.data.key) {
        saveKey(got.data.key);
        return true;
      }
      if (got.status !== 401 && got.status !== 400) {
        // Не отказ во входе, а сбой по пути: сервер не ответил или ответил не он (прокси, перезапуск).
        $("failed-text").textContent = "Сервер сейчас не отвечает. Закройте приложение и откройте его снова " +
          "через минуту кнопкой «Штурман» в боте согласований.";
      }
      screen("screen-failed");
      return false;
    }, function () {
      $("failed-text").textContent = "Нет связи с сервером. Закройте приложение и откройте его снова.";
      screen("screen-failed");
      return false;
    });
  }

  /* ------------------------------------------------------------ «Согласования» */

  function laterText(later) {
    var words = {
      commitment: "обязательств", owner_fact: "фактов о вас", page: "страниц о людях",
      project: "проектов", hidden: "скрытых сообщений"
    };
    var parts = [];
    Object.keys(words).forEach(function (name) {
      var n = later && later[name];
      if (n) parts.push(words[name] + ": " + n);
    });
    if (!parts.length) return "";
    return "Ещё ждут показа — " + parts.join(", ") + ". Они придут со следующей сводкой в боте; " +
      "решить их сразу можно на странице «Переписка», раздел «Память ассистента».";
  }

  // Срок решения по-человечески, по часам телефона: «сегодня в 14:30», «завтра в 9:05», «15 октября в 9:05».
  function deadline(value) {
    if (!value) return "";
    var at = new Date(value);
    if (isNaN(at.getTime())) return "";
    function two(n) { return (n < 10 ? "0" : "") + n; }
    var time = at.getHours() + ":" + two(at.getMinutes());
    var today = new Date();
    var tomorrow = new Date(today.getFullYear(), today.getMonth(), today.getDate() + 1);
    function sameDay(a, b) {
      return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
    }
    if (sameDay(at, today)) return "сегодня в " + time;
    if (sameDay(at, tomorrow)) return "завтра в " + time;
    var months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
                  "октября", "ноября", "декабря"];
    var date = at.getDate() + " " + months[at.getMonth()];
    if (at.getFullYear() !== today.getFullYear()) date += " " + at.getFullYear();
    return date + " в " + time;
  }

  function card(item) {
    var node = el("article", "card");
    node.appendChild(el("div", "kind", item.kind_title));
    if (item.title && item.title !== item.kind_title) node.appendChild(el("h2", null, item.title));
    if (item.head && item.head.length) {
      var lines = el("ul", "lines");
      item.head.forEach(function (line) { lines.appendChild(el("li", null, line)); });
      node.appendChild(lines);
    }
    if (item.kind === "draft") {
      node.appendChild(el("div", "verbatim-label", "Текст сообщения дословно:"));
      node.appendChild(el("div", "verbatim", item.text));
    } else if (item.text) {
      node.appendChild(el("div", "body", item.text));
    }
    if (item.note) node.appendChild(el("div", "note", item.note));
    var until = deadline(item.expires_at);
    if (until) node.appendChild(el("div", "note", "Ждёт решения до " + until + "."));
    var actions = el("div", "actions");
    (item.actions || []).forEach(function (action) {
      var button = el("button", action.primary ? "primary" : "secondary", action.label);
      button.type = "button";
      button.addEventListener("click", function () { decide(action.ref, actions); });
      actions.appendChild(button);
    });
    node.appendChild(actions);
    return node;
  }

  function render(data) {
    var items = data.items || [];
    var list = $("list");
    while (list.firstChild) list.removeChild(list.firstChild);
    items.forEach(function (item) { list.appendChild(card(item)); });
    $("count").textContent = items.length ? String(items.length) : "";
    show("empty", !items.length);
    var later = laterText(data.later);
    $("later").textContent = later;
    show("later", Boolean(later));
    var hasDrafts = items.some(function (item) { return item.kind === "draft"; });
    show("sending-off", hasDrafts && data.sending !== "on");
    $("tab-count").textContent = items.length ? String(items.length) : "";
    show("tab-count", items.length > 0);
    var first = !listShown;
    listShown = true;
    if (first || !signedIn) screen("main");
    if (first && tab === "chats") chatsOpened();
  }

  // Список не пришёл. Если на экране уже есть список — короткое сообщение внизу; если ещё нет —
  // отдельный экран с кнопкой «Повторить», а не бесконечное «Открываю…».
  function loadFailed(message) {
    if (listShown) { toast(message); return; }
    $("error-text").textContent = message;
    screen("screen-error");
  }

  function load() {
    return request("GET", "approvals").then(function (got) {
      if (got.status === 200) { render(got.data); return; }
      if (got.status === 401) {
        saveKey("");
        if (initData) return signIn().then(function (ok) { if (ok) return load(); });
        sessionLost(got.data);
        return;
      }
      loadFailed(got.data.error || "Сервер не ответил. Попробуйте ещё раз через минуту.");
    }, function () { loadFailed("Нет связи с сервером. Проверьте интернет и попробуйте ещё раз."); });
  }

  function decide(ref, actions) {
    if (busy) return;
    busy = true;
    Array.prototype.forEach.call(actions.querySelectorAll("button"), function (b) { b.disabled = true; });
    request("POST", "approvals/decide", { ref: ref }).then(function (got) {
      busy = false;
      if (got.status === 401) { saveKey(""); return load(); }
      toast((got.data && (got.data.answer || got.data.error)) || "Готово.");
      return load();
    }, function () {
      busy = false;
      Array.prototype.forEach.call(actions.querySelectorAll("button"), function (b) { b.disabled = false; });
      toast("Нет связи с сервером. Решение не передано — попробуйте ещё раз.");
    });
  }

  /* ------------------------------------------------------------ «Чаты» */

  // Тот же выбор, что в шаге «Какие чаты читать» на странице «Переписка» (setup.js: loadChats,
  // chatRow, waitAndRetry): поиск, вид, «только выбранные», глубина истории, исключение. Отличия:
  // список — из памяти сервиса (в Telegram — только кнопкой «Обновить список из Telegram»), чаты
  // включаются по одному, исключение не стирает уже сохранённое.

  var FIRST_CHATS = 20, MORE_CHATS = 30;
  var WAIT_AUTO_MAX = 120;                       // дольше двух минут сами не ждём — пишем, через сколько
  var KIND_NAMES = { personal: "личный", group: "группа", channel: "канал" };
  var chats = {
    accounts: [], account: null, mine: null, items: [], total: 0, enabledTotal: 0, loaded: true, age: null,
    loading: false, key: "", wait: null, opened: false, depthFor: null
  };

  function plural(n, one, few, many) {
    var a = Math.abs(n) % 100, b = a % 10;
    if (a > 10 && a < 20) return many;
    if (b === 1) return one;
    if (b >= 2 && b <= 4) return few;
    return many;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function chatsPanel(name) {
    ["chats-loading", "chats-none", "chats-offline", "chats-failed", "chats-body"].forEach(function (id) {
      show(id, id === name);
    });
  }

  function chatsStatus(kind, message) {
    var node = $("chats-status");
    node.textContent = message || "";
    node.className = "status" + (kind ? " " + kind : "");
    node.hidden = !message;
  }

  function chatsFailed(message) {
    $("chats-failed-text").textContent = message;
    chatsPanel("chats-failed");
  }

  function chatsOpened() {
    if (!signedIn || !key || chats.opened) return;
    chats.opened = true;
    loadAccounts(false);
  }

  function currentAccount() {
    return chats.accounts.filter(function (a) { return a.account_id === chats.account; })[0] || null;
  }

  function loadAccounts(quiet) {
    if (!quiet) chatsPanel("chats-loading");
    return request("GET", "chats").then(function (got) {
      if (got.status === 401) { sessionLost(got.data); return; }
      if (got.status !== 200) {
        chatsFailed(got.data.error || "Сервер не ответил. Попробуйте ещё раз через минуту.");
        return;
      }
      // сначала ваш аккаунт, потом помощника
      chats.accounts = (got.data.accounts || []).slice().sort(function (a, b) {
        return (a.role === "owner" ? 0 : 1) - (b.role === "owner" ? 0 : 1);
      });
      chats.mine = got.data.mine || null;
      drawAccounts();
      if (!chats.accounts.length) { chatsPanel("chats-none"); return; }
      if (!currentAccount()) {
        var preferred = chats.accounts.filter(function (a) { return a.role === chats.mine; })[0];
        var running = chats.accounts.filter(function (a) { return a.status === "running"; })[0];
        chats.account = (preferred || running || chats.accounts[0]).account_id;
        chats.key = "";
        drawAccounts();
      }
      showAccount();
    }, function () { chatsFailed("Нет связи с сервером. Проверьте интернет и попробуйте ещё раз."); });
  }

  function accountName(a) {
    return (a.role === "owner" ? "Ваш" : "Ассистента") + (a.label ? ": " + a.label : "");
  }

  function drawAccounts() {
    var box = $("chats-accounts");
    clear(box);
    box.hidden = chats.accounts.length < 2;
    chats.accounts.forEach(function (a) {
      var button = el("button", "seg");
      button.type = "button";
      button.appendChild(el("span", "seg-role", a.role === "owner" ? "Ваш аккаунт" : "Аккаунт ассистента"));
      button.appendChild(el("span", "seg-label", a.label || ""));
      button.setAttribute("aria-label", accountName(a));
      button.setAttribute("role", "tab");
      button.setAttribute("aria-selected", a.account_id === chats.account ? "true" : "false");
      button.addEventListener("click", function () {
        if (a.account_id === chats.account) return;
        chats.account = a.account_id;
        chats.key = "";
        chats.items = [];
        drawAccounts();
        showAccount();
      });
      box.appendChild(button);
    });
  }

  function setDepth(months) {
    var select = $("chats-depth");
    var value = months === null || months === undefined ? "all" : String(months);
    if (!Array.prototype.some.call(select.options, function (o) { return o.value === value; })) {
      var option = el("option", null, value + " мес.");
      option.value = value;
      select.insertBefore(option, select.querySelector('option[value="all"]'));
    }
    select.value = value;
  }

  function depthValue() {
    var value = $("chats-depth").value;
    return value === "all" ? null : Number(value);
  }

  function depthLabel() {
    var select = $("chats-depth");
    return select.options[select.selectedIndex] ? select.options[select.selectedIndex].textContent : "";
  }

  function showAccount() {
    var a = currentAccount();
    stopWait();
    chatsStatus("", "");
    if (!a) { chatsPanel("chats-none"); return; }
    $("chats-count").textContent = a.chats_enabled ? "выбрано: " + a.chats_enabled : "";
    if (a.status !== "running") {
      $("chats-offline-text").textContent = (a.role === "owner" ? "Ваш аккаунт" : "Аккаунт ассистента") +
        (a.label ? " «" + a.label + "»" : "") + " сейчас не подключён, поэтому список его чатов недоступен. " +
        "Войдите в него или снимите его с паузы на странице «Переписка» в браузере.";
      chatsPanel("chats-offline");
      return;
    }
    if (chats.depthFor !== a.account_id) {         // глубина по умолчанию — как в настройке аккаунта
      chats.depthFor = a.account_id;
      setDepth(a.backfill_months);
    }
    chatsPanel("chats-body");
    loadDialogs(true);
  }

  function queryKey() {
    return [chats.account, $("chats-q").value.trim(), $("chats-kind").value, $("chats-only").checked].join("|");
  }

  /* Telegram велел подождать: отсчёт и один повтор сам, без новых нажатий — как на странице. */
  function waitAndRetry(seconds, again) {
    stopWait();
    var left = Math.max(1, Math.ceil(seconds));
    if (left > WAIT_AUTO_MAX) {
      chatsStatus("warn", "Telegram просит подождать " + Math.ceil(left / 60) + " мин. Список можно обновить позже, " +
        "а выбирать чаты из уже загруженного — сейчас.");
      return;
    }
    $("chats-refresh").disabled = true;
    function tick() {
      if (left <= 0) {
        stopWait();
        chatsStatus("info", "Обновляю список…");
        again();
        return;
      }
      chatsStatus("wait", "Telegram просит подождать. Повторю сам через " + left + " с — нажимать ничего не нужно.");
      left -= 1;
    }
    tick();
    chats.wait = setInterval(tick, 1000);
  }

  function stopWait() {
    if (chats.wait) { clearInterval(chats.wait); chats.wait = null; }
    $("chats-refresh").disabled = false;
  }

  function loadDialogs(reset, refreshList, retried) {
    if (chats.account === null || chats.loading) return;
    if (!retried) stopWait();
    var account = chats.account, queried = queryKey(), offset = reset ? 0 : chats.items.length;
    chats.loading = true;
    chats.key = queried;
    if (refreshList) {
      $("chats-refresh").disabled = true;
      chatsStatus("info", "Обновляю список из Telegram…");
    }
    var query = "?offset=" + offset + "&limit=" + (reset ? FIRST_CHATS : MORE_CHATS) +
      "&q=" + encodeURIComponent($("chats-q").value.trim()) + "&kind=" + encodeURIComponent($("chats-kind").value) +
      ($("chats-only").checked ? "&only=enabled" : "") + (refreshList ? "&refresh=1" : "");
    request("GET", "chats/" + account + "/dialogs" + query).then(function (got) {
      chats.loading = false;
      if (got.status === 401) { sessionLost(got.data); return; }
      if (account !== chats.account || queried !== queryKey()) {   // пока шёл запрос, условие поменяли
        if (refreshList) $("chats-refresh").disabled = false;
        loadDialogs(true);
        return;
      }
      if (got.status === 429 && got.data.retry_after && !retried) {
        waitAndRetry(got.data.retry_after, function () {
          if (chats.account === account && queried === queryKey()) loadDialogs(reset, refreshList, true);
          else chatsStatus("", "");
        });
        return;
      }
      if (refreshList) $("chats-refresh").disabled = false;
      if (got.status === 409 && (got.data.code === "offline" || /не подключён/.test(got.data.error || ""))) {
        loadAccounts(true);
        return;
      }
      if (got.status !== 200) {
        chatsStatus("warn", got.data.error || "Список не загрузился. Попробуйте ещё раз.");
        return;
      }
      chats.loaded = got.data.loaded !== false;
      chats.age = typeof got.data.age === "number" ? got.data.age : null;
      chats.items = reset ? (got.data.items || []) : chats.items.concat(got.data.items || []);
      chats.total = got.data.total || 0;
      chats.enabledTotal = got.data.enabled_total || 0;
      if (!chats.loaded) {
        chatsStatus("info", "Список чатов ещё не загружен из Telegram. Нажмите «Обновить список из Telegram».");
      } else {
        chatsStatus("", "");
        if (refreshList) toast("Список обновлён из Telegram.");
      }
      drawChats();
    }, function () {
      chats.loading = false;
      if (refreshList) $("chats-refresh").disabled = false;
      chatsStatus("warn", "Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.");
    });
  }

  function ageText(seconds) {
    if (seconds < 60) return "только что";
    var minutes = Math.round(seconds / 60);
    if (minutes < 60) return minutes + " мин назад";
    var hours = Math.round(minutes / 60);
    return hours + " ч назад";
  }

  function drawCounter() {
    var n = chats.enabledTotal;
    var words = "Выбрано для чтения: " + n + " " + plural(n, "чат", "чата", "чатов") + ".";
    if (chats.loaded) words += " Показано " + chats.items.length + " из " + chats.total + ".";
    if (chats.loaded && chats.age !== null) words += " Список из Telegram — " + ageText(chats.age) + ".";
    $("chats-counter").textContent = words;
    $("chats-count").textContent = n ? "выбрано: " + n : "";
  }

  function exclude(item, excluded, button) {
    button.disabled = true;
    request("POST", "chats/" + chats.account + "/exclude",
            { peer_class: item.peer_class, tg_id: item.tg_id, excluded: excluded }).then(function (got) {
      if (got.status === 401) { sessionLost(got.data); return; }
      if (got.status !== 200) {
        button.disabled = false;
        toast(got.data.error || "Не получилось изменить чат.");
        return;
      }
      toast(excluded ? "Чат больше не сохраняется. Уже сохранённое осталось в архиве."
                     : "Чат возвращён в архив. Включите «Читать», чтобы сервис снова его сохранял.");
      loadDialogs(true);
    }, function () {
      button.disabled = false;
      toast("Нет связи с сервером. Проверьте интернет и обновите список.");
    });
  }

  function confirmBox(item, row, button) {
    var old = row.querySelector(".confirm");
    if (old) { row.removeChild(old); return; }
    var box = el("div", "confirm");
    box.appendChild(el("p", null, "Не сохранять этот чат? Сервис перестанет принимать его сообщения из любого источника: " +
      "из аккаунта, бизнес-режима и выгрузки. Уже сохранённое останется — стереть его можно только на странице «Переписка»."));
    var actions = el("div", "actions");
    var yes = el("button", "danger", "Не сохранять");
    yes.type = "button";
    var no = el("button", "secondary", "Отмена");
    no.type = "button";
    yes.addEventListener("click", function () { no.disabled = true; exclude(item, true, yes); });
    no.addEventListener("click", function () { row.removeChild(box); button.focus(); });
    actions.appendChild(yes);
    actions.appendChild(no);
    box.appendChild(actions);
    row.appendChild(box);
    yes.focus();
  }

  function chatRow(item) {
    var row = el("li", "chat" + (item.excluded ? " is-excluded" : ""));
    var main = el("div", "chat-main");
    main.appendChild(el("div", "chat-title", item.title || "Без названия"));
    var meta = el("div", "chat-meta");
    meta.appendChild(el("span", null, KIND_NAMES[item.kind] || "чат"));
    if (item.username) meta.appendChild(el("span", null, "@" + item.username));
    if (item.excluded) meta.appendChild(el("span", "tag", item.locked ? "служебный чат Telegram" : "не сохраняется"));
    main.appendChild(meta);
    if (!item.locked) {
      var action = el("button", "link", item.excluded ? "Вернуть в архив" : "Не сохранять");
      action.type = "button";
      action.setAttribute("aria-label", (item.excluded ? "Вернуть в архив чат " : "Не сохранять чат ") + (item.title || ""));
      action.addEventListener("click", function () {
        if (item.excluded) exclude(item, false, action);
        else confirmBox(item, row, action);
      });
      main.appendChild(action);
    }
    row.appendChild(main);

    var label = el("label", "switch");
    var input = el("input");
    input.type = "checkbox";
    input.setAttribute("role", "switch");
    input.checked = !!item.enabled;
    input.disabled = !!item.excluded;
    input.setAttribute("aria-label", "Читать чат " + (item.title || ""));
    label.appendChild(el("span", "switch-text", "Читать"));
    label.appendChild(input);
    label.appendChild(el("span", "track"));
    input.addEventListener("change", function () {
      var want = input.checked;
      var body = { peer_class: item.peer_class, tg_id: item.tg_id, enabled: want };
      if (want) body.months = depthValue();
      var depth = depthLabel();
      input.disabled = true;
      request("POST", "chats/" + chats.account + "/sync", body).then(function (got) {
        if (got.status === 401) { sessionLost(got.data); return; }
        input.disabled = false;
        var result = got.status === 200 && got.data.chats && got.data.chats[0];
        if (!result || result.enabled !== want) {
          input.checked = !want;
          toast((result && result.error) || got.data.error || "Не получилось изменить чат.");
          if (result && result.excluded) loadDialogs(true);
          return;
        }
        item.enabled = want;
        chats.enabledTotal = got.data.enabled_total;
        drawCounter();
        toast(want ? "Чтение включено, глубина истории — " + depth + ". Бот согласований пришлёт об этом сообщение."
                   : "Чтение выключено. Уже сохранённое осталось в архиве.");
      }, function () {
        input.disabled = false;
        input.checked = !want;
        toast("Нет связи с сервером. Проверьте интернет и обновите список.");
      });
    });
    row.appendChild(label);
    return row;
  }

  function drawChats() {
    var list = $("chats-list");
    clear(list);
    chats.items.forEach(function (item) { list.appendChild(chatRow(item)); });
    if (!chats.items.length && chats.loaded) {
      var filtered = $("chats-q").value.trim() || $("chats-kind").value || $("chats-only").checked;
      list.appendChild(el("li", "chats-empty", filtered ? "Ничего не найдено. Измените поиск или вид чатов."
                                                         : "В этом аккаунте нет чатов."));
    }
    drawCounter();
    var left = chats.total - chats.items.length;
    show("chats-more", chats.loaded && left > 0);
    if (left > 0) $("chats-more").textContent = "Показать ещё " + Math.min(left, MORE_CHATS) + " (осталось " + left + ")";
  }

  function wireChats() {
    var timer = null;
    $("chats-q").addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(function () { loadDialogs(true); }, 300);
    });
    $("chats-kind").addEventListener("change", function () { loadDialogs(true); });
    $("chats-only").addEventListener("change", function () { loadDialogs(true); });
    $("chats-more").addEventListener("click", function () { loadDialogs(false); });
    $("chats-refresh").addEventListener("click", function () { loadDialogs(true, true); });
    ["chats-none-retry", "chats-offline-retry", "chats-failed-retry"].forEach(function (id) {
      $(id).addEventListener("click", function () { loadAccounts(false); });
    });
  }


  /* ------------------------------------------------------------ запуск */

  $("refresh").addEventListener("click", function () { load(); });
  $("tab-approvals").addEventListener("click", function () { openTab("approvals"); });
  $("tab-chats").addEventListener("click", function () { openTab("chats"); });
  wireChats();
  try { tab = sessionStorage.getItem(TAB_NAME) === "chats" ? "chats" : "approvals"; } catch (e) { tab = "approvals"; }
  openTab(tab);
  $("retry").addEventListener("click", function () {
    screen("screen-wait");
    load();
  });
  $("home-screen").addEventListener("click", function () { postEvent("web_app_add_to_home_screen"); });

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState !== "visible" || !key) return;
    load();
    // Список чатов — из памяти сервиса, без запроса к Telegram: можно перечитать при возвращении.
    if (tab === "chats" && chats.opened && !chats.loading && !chats.wait) loadAccounts(true);
  });

  postEvent("web_app_ready");
  postEvent("web_app_expand");
  // Ярлык на главном экране — Bot API 8.0 и только на телефоне; уже добавленный не предлагаем.
  if (versionAtLeast("8.0") && /^(android|ios)$/.test(String(params.tgWebAppPlatform || ""))) {
    postEvent("web_app_check_home_screen");
  }

  key = loadKey();
  (key ? load() : signIn().then(function (ok) { if (ok) return load(); })).then(function () {
    clearInterval(listTimer);
    listTimer = setInterval(function () {
      if (key && document.visibilityState === "visible" && !busy) load();
    }, 60000);
  });
})();
