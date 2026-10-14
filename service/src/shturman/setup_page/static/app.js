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
 * Список чатов сервис отдаёт из базы; обновить его из Telegram можно только кнопкой «Обновить список
 * из Telegram». Загрузка идёт в фоне (Р-81): экран показывает её ход, а если Telegram велит
 * подождать — отсчёт; потом загрузка продолжится сама, с того же места.
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
  var tab = "approvals";                         // открытый раздел: approvals | chats | spheres | notify

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
    show("screen-spheres", signedIn && tab === "spheres");
    show("screen-notify", signedIn && tab === "notify");
    show("refresh", signedIn && tab === "approvals");
  }

  function openTab(name) {
    tab = name === "chats" || name === "spheres" || name === "notify" ? name : "approvals";
    try { sessionStorage.setItem(TAB_NAME, tab); } catch (e) { /* не страшно */ }
    ["approvals", "chats", "spheres", "notify"].forEach(function (one) {
      $("tab-" + one).setAttribute("aria-selected", one === tab ? "true" : "false");
    });
    if (signedIn) screen("main");
    if (tab === "chats") { chatsOpened(); schedulePoll(); }
    if (tab === "spheres" && signedIn) loadSpheres(false);
    if (tab === "notify" && signedIn) loadNotify(false);
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
    if (first && tab === "spheres") loadSpheres(false);
    if (first && tab === "notify") loadNotify(false);
    if (first) refreshSphereList();
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
  // chatRow, drawLoad): поиск, вид, «только выбранные», глубина истории, исключение. Отличия:
  // список — из базы сервиса (обновить из Telegram — только кнопкой «Обновить список из Telegram»),
  // чаты включаются по одному, исключение не стирает уже сохранённое.

  var FIRST_CHATS = 20, MORE_CHATS = 30;
  var KIND_NAMES = { personal: "личный", group: "группа", channel: "канал" };
  var chats = {
    accounts: [], account: null, mine: null, items: [], total: 0, enabledTotal: 0, loaded: true, age: null,
    loading: false, key: "", opened: false, depthFor: null,
    load: null, poll: null, tick: null, busy: 0     // фоновая загрузка списка: ход, опрос, отсчёт, сохранения
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
    stopPoll();
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
    return [chats.account, $("chats-layout").value, $("chats-q").value.trim(), $("chats-kind").value, $("chats-only").checked].join("|");
  }

  /* Список загружается из Telegram в фоне (Р-81): «Обновить» только запускает загрузку, ответ приходит
     сразу — с тем, что уже есть, и с ходом загрузки (`load`). Пока она идёт, экран раз в несколько секунд
     перечитывает список; если Telegram просит подождать — отсчёт, потом загрузка продолжится сама. */
  var LOAD_POLL = 2500;

  function number(value) { return (Number(value) || 0).toLocaleString("ru-RU"); }

  function waitWords(seconds) {
    return seconds < 120 ? seconds + " с" : Math.ceil(seconds / 60) + " мин";
  }

  function stopPoll() {
    if (chats.poll) { clearTimeout(chats.poll); chats.poll = null; }
    if (chats.tick) { clearInterval(chats.tick); chats.tick = null; }
  }

  function drawLoad() {
    var l = chats.load || {}, box = $("chats-load");
    if (chats.tick) { clearInterval(chats.tick); chats.tick = null; }
    $("chats-refresh").disabled = !!l.running;
    $("chats-refresh").textContent = l.running ? "Список обновляется…" : "Обновить список из Telegram";
    var visible = !!l.running || l.state === "failed" || (!chats.loaded && l.state === "idle");
    show("chats-load", visible);
    if (!visible) return;
    box.className = "load" + (l.state === "waiting" ? " wait" : l.state === "failed" ? " warn" : !l.running ? " idle" : "");
    var part = l.expected ? Math.min(100, Math.round(100 * (l.loaded || 0) / l.expected)) : 0;
    show("chats-load-meter-box", !!l.running);
    $("chats-load-meter").style.width = (l.running ? Math.max(3, part) : 0) + "%";
    var got = "Загружено " + number(l.loaded || 0) + (l.expected ? " из ~" + number(l.expected) : "") + ".";
    var title = $("chats-load-title"), words = $("chats-load-text");
    if (l.state === "waiting") {
      var left = Math.max(1, l.wait_seconds || 1);
      var tick = function () {
        title.textContent = "Telegram просит подождать";
        words.textContent = "Продолжу сам через " + waitWords(left) + " — с того же места, нажимать ничего не нужно. " + got;
        if (left > 1) left -= 1;
      };
      tick();
      chats.tick = setInterval(tick, 1000);
    } else if (l.running) {
      title.textContent = "Загружаю список из Telegram";
      words.textContent = got + " Уже загруженные чаты можно выбирать — список дополнится сам.";
    } else if (l.state === "failed") {
      title.textContent = "Список не обновился";
      words.textContent = (l.error || "Telegram не ответил.") + (chats.loaded ? " Показан прежний список." : "");
    } else {
      title.textContent = "Список чатов ещё не загружен";
      words.textContent = "Нажмите «Обновить список из Telegram» — он загрузится за минуту-другую.";
    }
  }

  function schedulePoll() {
    if (chats.poll) { clearTimeout(chats.poll); chats.poll = null; }
    if (!(chats.load && chats.load.running) || tab !== "chats") return;
    chats.poll = setTimeout(function () {
      chats.poll = null;
      if (chats.busy > 0 || chats.loading || document.visibilityState !== "visible") { schedulePoll(); return; }
      loadDialogs(true, false, true);
    }, LOAD_POLL);
  }

  function loadDialogs(reset, refreshList, polling) {
    if (chats.account === null || chats.loading) return;
    if ($("chats-layout").value) { loadSections(refreshList, polling); return; }
    var account = chats.account, queried = queryKey(), offset = reset ? 0 : chats.items.length;
    // при опросе — столько, сколько уже показано: «Показать ещё» не сбрасывается
    var limit = polling ? Math.min(200, Math.max(FIRST_CHATS, chats.items.length)) : (reset ? FIRST_CHATS : MORE_CHATS);
    chats.loading = true;
    chats.key = queried;
    if (refreshList) $("chats-refresh").disabled = true;
    var query = "?offset=" + offset + "&limit=" + limit +
      "&q=" + encodeURIComponent($("chats-q").value.trim()) + "&kind=" + encodeURIComponent($("chats-kind").value) +
      ($("chats-only").checked ? "&only=enabled" : "") + (refreshList ? "&refresh=1" : "");
    request("GET", "chats/" + account + "/dialogs" + query).then(function (got) {
      chats.loading = false;
      if (got.status === 401) { sessionLost(got.data); return; }
      if (account !== chats.account || queried !== queryKey()) {   // пока шёл запрос, условие поменяли
        loadDialogs(true);
        return;
      }
      if (got.status === 409 && (got.data.code === "offline" || /не подключён/.test(got.data.error || ""))) {
        stopPoll();
        loadAccounts(true);
        return;
      }
      if (got.status !== 200) {
        if (!polling) chatsStatus("warn", got.data.error || "Список не загрузился. Попробуйте ещё раз.");
        drawLoad();
        schedulePoll();
        return;
      }
      var was = !!(chats.load && chats.load.running);
      chats.loaded = got.data.loaded !== false;
      chats.age = typeof got.data.age === "number" ? got.data.age : null;
      chats.load = got.data.load || null;
      chats.items = reset ? (got.data.items || []) : chats.items.concat(got.data.items || []);
      chats.total = got.data.total || 0;
      chats.enabledTotal = got.data.enabled_total || 0;
      if (!polling) chatsStatus("", "");
      drawChats();
      drawLoad();
      var finished = chats.load && !chats.load.running && chats.load.state === "done";
      if (was && finished) toast("Список обновлён из Telegram.");
      else if (refreshList && finished) toast("Список только что обновлён — он свежий.");
      schedulePoll();
    }, function () {
      chats.loading = false;
      drawLoad();
      if (!polling) chatsStatus("warn", "Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.");
      schedulePoll();
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
    if (chats.loaded && $("chats-layout").value) words += " Разделов: " + sections.list.length + ".";
    else if (chats.loaded) words += " Показано " + number(chats.items.length) + " из " + number(chats.total) + ".";
    if (chats.loaded && chats.age !== null && !(chats.load && chats.load.running)) {
      words += " Список из Telegram — " + ageText(chats.age) + ".";
    }
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
    sphereNames(item.spheres).forEach(function (name) { meta.appendChild(el("span", "chip", name)); });
    main.appendChild(meta);
    var links = el("div", "chat-links");
    if (!item.locked) {
      var action = el("button", "link", item.excluded ? "Вернуть в архив" : "Не сохранять");
      action.type = "button";
      action.setAttribute("aria-label", (item.excluded ? "Вернуть в архив чат " : "Не сохранять чат ") + (item.title || ""));
      action.addEventListener("click", function () {
        if (item.excluded) exclude(item, false, action);
        else confirmBox(item, row, action);
      });
      links.appendChild(action);
    }
    if (sph.list.length && !item.locked) {
      var pickButton = el("button", "link", "Сферы");
      pickButton.type = "button";
      pickButton.setAttribute("aria-label", "Сферы чата " + (item.title || ""));
      pickButton.addEventListener("click", function () { spherePicker(item, row); });
      links.appendChild(pickButton);
    }
    if (links.firstChild) main.appendChild(links);
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
      chats.busy += 1;
      request("POST", "chats/" + chats.account + "/sync", body).then(function (got) {
        chats.busy -= 1;
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
        if (row.sectionChanged) row.sectionChanged(want ? 1 : -1);
        toast(want ? "Чтение включено, глубина истории — " + depth + ". Бот согласований пришлёт об этом сообщение."
                   : "Чтение выключено. Уже сохранённое осталось в архиве.");
      }, function () {
        chats.busy -= 1;
        input.disabled = false;
        input.checked = !want;
        toast("Нет связи с сервером. Проверьте интернет и обновите список.");
      });
    });
    row.appendChild(label);
    return row;
  }

  function drawChats() {
    var layout = $("chats-layout").value;
    show("chats-list", !layout);
    show("chats-sections", !!layout);
    if (layout) { show("chats-more", false); drawCounter(); return; }
    var list = $("chats-list");
    clear(list);
    chats.items.forEach(function (item) { list.appendChild(chatRow(item)); });
    var running = !!(chats.load && chats.load.running);
    if (!chats.items.length && chats.loaded) {
      var filtered = $("chats-q").value.trim() || $("chats-kind").value || $("chats-only").checked;
      list.appendChild(el("li", "chats-empty", filtered ? (running ? "Пока ничего не найдено — список ещё загружается."
                                                                   : "Ничего не найдено. Измените поиск или вид чатов.")
                                                         : "В этом аккаунте нет чатов."));
    }
    drawCounter();
    var left = chats.total - chats.items.length;
    show("chats-more", chats.loaded && left > 0);
    if (left > 0) $("chats-more").textContent = "Показать ещё " + Math.min(left, MORE_CHATS) + " (осталось " + number(left) + ")";
  }

  function wireChats() {
    var timer = null;
    $("chats-q").addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        // список по этому запросу уже запрошен (ответ на прежний запрос перезапросил его сам) —
        // второй раз не перерисовываем: иначе пропадёт открытое подтверждение в строке
        if (!chats.loading && chats.key === queryKey()) return;
        loadDialogs(true);
      }, 300);
    });
    $("chats-kind").addEventListener("change", function () { loadDialogs(true); });
    $("chats-layout").addEventListener("change", function () {
      sections.items = {};
      sections.list = [];
      clear($("chats-sections"));               // прежние разделы не показываем, пока грузятся новые
      if ($("chats-layout").value) $("chats-sections").appendChild(el("p", "chats-empty", "Раскладываю чаты…"));
      drawChats();
      loadDialogs(true);
    });
    $("chats-only").addEventListener("change", function () { loadDialogs(true); });
    $("chats-more").addEventListener("click", function () { loadDialogs(false); });
    $("chats-refresh").addEventListener("click", function () { loadDialogs(true, true); });
    ["chats-none-retry", "chats-offline-retry", "chats-failed-retry"].forEach(function (id) {
      $(id).addEventListener("click", function () { loadAccounts(false); });
    });
  }


  /* ------------------------------------------------- раскладки и сферы (Р-82) */

  // Те же разделы, что на странице «Переписка» (setup.js: loadSections, drawSectionBody): папки Telegram,
  // сферы, активность, вид — со счётчиком «читается X из Y». Отличие от страницы: «Включить все» здесь
  // нет (Р-80 — включать чтение из приложения можно только по одному чату), «Выключить все» есть, и
  // о нём бот согласований пришлёт сообщение.
  var SECTION_FIRST = 20, SECTION_MORE = 30;
  var EMPTY_LAYOUT = {
    folders: "В Telegram нет папок — или список ещё не обновлялся из Telegram.",
    spheres: "Сфер пока нет. Заведите их на вкладке «Сферы»."
  };
  var sections = { list: [], open: {}, items: {}, busy: {} };
  var sph = { list: [], data: null, folders: [], open: {}, edit: null, loaded: false };

  function filterQuery() {
    return "&q=" + encodeURIComponent($("chats-q").value.trim()) + "&kind=" + encodeURIComponent($("chats-kind").value) +
      ($("chats-only").checked ? "&only=enabled" : "");
  }

  function sphereNames(ids) {
    return (ids || []).map(function (id) {
      var found = sph.list.filter(function (s) { return s.id === id; })[0];
      return found ? found.title : "";
    }).filter(Boolean);
  }

  function loadSections(refreshList, polling) {
    var account = chats.account, queried = queryKey();
    chats.loading = true;
    chats.key = queried;
    if (refreshList) $("chats-refresh").disabled = true;
    request("GET", "chats/" + account + "/dialogs?offset=0&limit=1" + filterQuery() + (refreshList ? "&refresh=1" : "")).then(function (got) {
      if (got.status === 401) { chats.loading = false; sessionLost(got.data); return; }
      if (got.status !== 200) {
        chats.loading = false;
        if (!polling) chatsStatus("warn", got.data.error || "Список не загрузился. Попробуйте ещё раз.");
        drawLoad(); schedulePoll();
        return;
      }
      chats.loaded = got.data.loaded !== false;
      chats.age = typeof got.data.age === "number" ? got.data.age : null;
      chats.load = got.data.load || null;
      chats.enabledTotal = got.data.enabled_total || 0;
      if (!chats.loaded) { chats.loading = false; sections.list = []; drawSections(); drawLoad(); drawCounter(); schedulePoll(); return; }
      return request("GET", "chats/" + account + "/sections?layout=" + $("chats-layout").value + filterQuery()).then(function (s) {
        chats.loading = false;
        if (s.status === 401) { sessionLost(s.data); return; }
        if (account !== chats.account || queried !== queryKey()) { loadDialogs(true); return; }
        if (s.status !== 200) { if (!polling) chatsStatus("warn", s.data.error || "Разделы не загрузились."); schedulePoll(); return; }
        if (!polling) chatsStatus("", "");
        sections.list = s.data.sections || [];
        sph.list = s.data.spheres || [];
        drawSections(); drawLoad(); drawCounter();
        sections.list.forEach(function (sec) { if (sections.open[sec.id]) loadSection(sec, true); });
        schedulePoll();
      });
    }, function () {
      chats.loading = false;
      if (!polling) chatsStatus("warn", "Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.");
      schedulePoll();
    });
  }

  function drawSections() {
    var box = $("chats-sections");
    clear(box);
    if (!sections.list.length) {
      if (chats.loaded) box.appendChild(el("p", "chats-empty", EMPTY_LAYOUT[$("chats-layout").value] || "Ничего не найдено."));
      return;
    }
    sections.list.forEach(function (sec) { box.appendChild(sectionNode(sec)); });
  }

  function sectionCount(sec) { return "читается " + number(sec.enabled) + " из " + number(sec.total); }

  function sectionNode(sec) {
    var det = el("details", "sect");
    det.setAttribute("data-section", sec.id);
    det.open = !!sections.open[sec.id];
    var summary = el("summary");
    summary.appendChild(el("span", "sect-title", (sec.emoticon ? sec.emoticon + " " : "") + sec.title));
    var waiting = (sec.proposals || []).reduce(function (n, p) { return n + p.count; }, 0);
    if (waiting) summary.appendChild(el("span", "chip", "новые: " + waiting));
    var count = el("span", "sect-count" + (sec.enabled ? " on" : ""), sectionCount(sec));
    summary.appendChild(count);
    det.appendChild(summary);
    var body = el("div", "sect-body");
    det.appendChild(body);
    sec.countNode = count;
    sec.body = body;
    drawSectionBody(sec);
    det.addEventListener("toggle", function () {
      sections.open[sec.id] = det.open;
      if (det.open && !sections.items[sec.id]) loadSection(sec, true);
    });
    return det;
  }

  function loadSection(sec, reset) {
    if (sections.busy[sec.id]) return;
    var have = sections.items[sec.id];
    var offset = reset ? 0 : (have ? have.items.length : 0);
    var limit = reset ? Math.max(SECTION_FIRST, have ? Math.min(200, have.items.length) : 0) : SECTION_MORE;
    sections.busy[sec.id] = true;
    request("GET", "chats/" + chats.account + "/dialogs?section=" + encodeURIComponent(sec.id) + "&offset=" + offset +
            "&limit=" + limit + filterQuery()).then(function (got) {
      sections.busy[sec.id] = false;
      if (got.status === 401) { sessionLost(got.data); return; }
      if (got.status !== 200) { toast(got.data.error || "Раздел не загрузился."); return; }
      var items = reset || !have ? (got.data.items || []) : have.items.concat(got.data.items || []);
      sections.items[sec.id] = { items: items, total: got.data.total || 0 };
      drawSectionBody(sec);
    }, function () { sections.busy[sec.id] = false; toast("Нет связи с сервером."); });
  }

  function button(cls, text, onClick) {
    var b = el("button", cls, text);
    b.type = "button";
    b.addEventListener("click", onClick);
    return b;
  }

  function disableSection(sec, b) {
    var box = sec.body.querySelector(".confirm");
    if (box) { box.parentNode.removeChild(box); return; }
    box = el("div", "confirm");
    box.appendChild(el("p", null, "Выключить чтение всех чатов раздела «" + sec.title + "» (" + sec.enabled + ")? Уже сохранённое " +
      "останется в архиве. Бот согласований пришлёт об этом сообщение."));
    var actions = el("div", "actions");
    var yes = button("danger", "Выключить все", function () {
      yes.disabled = true;
      request("POST", "chats/" + chats.account + "/bulk", { section: sec.id, enabled: false, q: $("chats-q").value.trim(),
        kind: $("chats-kind").value, only: $("chats-only").checked }).then(function (got) {
        if (got.status === 401) { sessionLost(got.data); return; }
        if (got.status !== 200) { yes.disabled = false; toast(got.data.error || "Не получилось."); return; }
        toast("Выключено чатов: " + got.data.changed + ". Бот согласований пришлёт об этом сообщение.");
        sections.items[sec.id] = null;
        loadDialogs(true);
      }, function () { yes.disabled = false; toast("Нет связи с сервером."); });
    });
    actions.appendChild(yes);
    actions.appendChild(button("secondary", "Отмена", function () { box.parentNode.removeChild(box); b.focus(); }));
    box.appendChild(actions);
    b.parentNode.parentNode.insertBefore(box, b.parentNode.nextSibling);
  }

  function drawSectionBody(sec) {
    var body = sec.body;
    if (!body) return;
    clear(body);
    if (sec.description) body.appendChild(el("p", "sect-note", sec.description));
    (sec.notes || []).forEach(function (n) { body.appendChild(el("p", "sect-note", n)); });
    var folderId = sec.id.indexOf("folder:") === 0 && sec.id !== "folder:none" ? Number(sec.id.split(":")[1]) : null;
    (sec.proposals || []).forEach(function (p) {
      body.appendChild(offerNode({ sphere_id: p.sphere_id, sphere_title: p.sphere_title, count: p.count, account_id: chats.account,
                                   folder_id: folderId, folder_title: sec.title }, function () { loadDialogs(true); }));
    });
    var actions = el("div", "actions");
    if (sec.enabled > 0) {
      var off = button("secondary", "Выключить все (" + number(sec.enabled) + ")", function () { disableSection(sec, off); });
      actions.appendChild(off);
    }
    if (folderId !== null) {
      var made = (sec.spheres || [])[0];
      actions.appendChild(button("secondary", made ? "Дополнить «" + made.title + "»" : "Сделать сферу", function () {
        makeFromFolder(chats.account, folderId, this, function () { loadDialogs(true); });
      }));
    }
    if (actions.firstChild) body.appendChild(actions);
    if (folderId !== null) {
      var linked = (sec.spheres || []).filter(function (x) { return x.propose; })[0];
      if (linked) body.appendChild(el("p", "sect-note", "Новые чаты папки предлагаются в сферу «" + linked.title + "»."));
      else if (sph.list.length) body.appendChild(linkRow(chats.account, folderId, sec.title, (sec.spheres || [])[0], function () { loadDialogs(true); }));
    }
    var got = sections.items[sec.id];
    if (!got) { body.appendChild(el("p", "sect-note", "Загружаю…")); return; }
    if (!got.items.length) { body.appendChild(el("p", "sect-note", "В этом разделе нет чатов.")); return; }
    var list = el("ul", "chats");
    got.items.forEach(function (item) {
      var row = chatRow(item);
      row.sectionChanged = function (delta) {
        sec.enabled += delta;
        sec.countNode.textContent = sectionCount(sec);
        sec.countNode.className = "sect-count" + (sec.enabled ? " on" : "");
      };
      list.appendChild(row);
    });
    body.appendChild(list);
    var left = got.total - got.items.length;
    if (left > 0) {
      var more = button("secondary wide", "Показать ещё " + Math.min(left, SECTION_MORE) + " (осталось " + number(left) + ")",
                        function () { loadSection(sec, false); });
      body.appendChild(more);
    }
  }

  function linkRow(account, folderId, folderTitle, made, done) {
    var row = el("div", "sect-link");
    var select = el("select");
    select.setAttribute("aria-label", "Сфера для новых чатов папки «" + folderTitle + "»");
    sph.list.forEach(function (s) { var o = el("option", null, s.title); o.value = String(s.id); select.appendChild(o); });
    if (made) select.value = String(made.id);
    row.appendChild(select);
    row.appendChild(button("secondary", "Предлагать новые", function () {
      linkFolder(Number(select.value), account, folderId, true, this, done);
    }));
    return row;
  }

  function spherePicker(item, row) {
    var old = row.querySelector(".pick");
    if (old) { row.removeChild(old); return; }
    var chosen = {};
    (item.spheres || []).forEach(function (id) { chosen[id] = true; });
    var box = el("div", "pick");
    box.appendChild(el("p", null, "В каких сферах этот чат — можно в нескольких:"));
    sph.list.forEach(function (s) {
      var label = el("label", "check");
      var input = el("input");
      input.type = "checkbox";
      input.checked = !!chosen[s.id];
      input.addEventListener("change", function () { if (input.checked) chosen[s.id] = true; else delete chosen[s.id]; });
      label.appendChild(input);
      label.appendChild(el("span", null, s.title));
      box.appendChild(label);
    });
    var actions = el("div", "actions");
    var save = button("primary", "Сохранить", function () {
      save.disabled = true;
      var ids = sph.list.filter(function (s) { return chosen[s.id]; }).map(function (s) { return s.id; });
      request("PUT", "spheres/chat", { account_id: chats.account, peer_class: item.peer_class, tg_id: item.tg_id, sphere_ids: ids }).then(function (got) {
        if (got.status === 401) { sessionLost(got.data); return; }
        save.disabled = false;
        if (got.status !== 200) { toast(got.data.error || "Не получилось."); return; }
        item.spheres = got.data.spheres;
        toast("Сферы чата сохранены.");
        var fresh = chatRow(item);
        fresh.sectionChanged = row.sectionChanged;
        row.parentNode.replaceChild(fresh, row);
        sph.loaded = false;
        if ($("chats-layout").value === "spheres") loadDialogs(true);
      }, function () { save.disabled = false; toast("Нет связи с сервером."); });
    });
    actions.appendChild(save);
    actions.appendChild(button("secondary", "Отмена", function () { row.removeChild(box); }));
    box.appendChild(actions);
    row.appendChild(box);
  }

  function offerNode(p, done) {
    var box = el("div", "offer");
    box.appendChild(el("p", null, "Новые чаты в папке «" + p.folder_title + "»: " + p.count + ". Добавить в сферу «" + p.sphere_title + "»?"));
    var names = (p.chats || []).map(function (c) { return c.title || "без названия"; }).slice(0, 6);
    if (names.length) box.appendChild(el("p", "muted small", names.join(", ") + (p.count > names.length ? " и ещё " + (p.count - names.length) : "")));
    var actions = el("div", "actions");
    function decide(accept, b) {
      b.disabled = true;
      request("POST", "spheres/" + p.sphere_id + "/proposals", { account_id: p.account_id, folder_id: p.folder_id, accept: accept }).then(function (got) {
        if (got.status === 401) { sessionLost(got.data); return; }
        b.disabled = false;
        if (got.status !== 200) { toast(got.data.error || "Не получилось."); return; }
        toast(accept ? "Добавлено в сферу: " + got.data.added + "." : "Больше не предложу: " + got.data.declined + ".");
        sph.loaded = false;
        refreshSphereList();
        if (done) done();
      }, function () { b.disabled = false; toast("Нет связи с сервером."); });
    }
    actions.appendChild(button("primary", "Добавить", function () { decide(true, this); }));
    actions.appendChild(button("secondary", "Не предлагать", function () { decide(false, this); }));
    box.appendChild(actions);
    return box;
  }

  function makeFromFolder(account, folderId, b, done) {
    b.disabled = true;
    request("POST", "spheres/folders/make", { account_id: account, folder_id: folderId }).then(function (got) {
      if (got.status === 401) { sessionLost(got.data); return; }
      b.disabled = false;
      if (got.status !== 200) { toast(got.data.error || "Не получилось."); return; }
      toast((got.data.created ? "Сфера «" + got.data.sphere.title + "» заведена" : "Сфера «" + got.data.sphere.title + "» дополнена") +
            ": добавлено чатов — " + got.data.added + ".");
      sph.loaded = false;
      refreshSphereList();
      if (done) done();
    }, function () { b.disabled = false; toast("Нет связи с сервером."); });
  }

  function linkFolder(sphereId, account, folderId, link, b, done) {
    b.disabled = true;
    request("POST", "spheres/" + sphereId + "/folders", { account_id: account, folder_id: folderId, link: link }).then(function (got) {
      if (got.status === 401) { sessionLost(got.data); return; }
      b.disabled = false;
      if (got.status !== 200) { toast(got.data.error || "Не получилось."); return; }
      toast(link ? "Новые чаты папки будут предлагаться" + (got.data.proposed ? " (сейчас — " + got.data.proposed + ")." : ".") : "Папка отвязана.");
      sph.loaded = false;
      refreshSphereList();
      if (done) done();
    }, function () { b.disabled = false; toast("Нет связи с сервером."); });
  }

  /* ---- экран «Сферы» */

  function refreshSphereList() {
    return request("GET", "spheres").then(function (got) {
      if (got.status !== 200) return;
      sph.data = got.data;
      sph.list = (got.data.spheres || []).map(function (s) { return { id: s.id, title: s.title }; });
      var waiting = (got.data.proposals || []).reduce(function (n, p) { return n + p.count; }, 0);
      $("tab-spheres-count").textContent = waiting ? String(waiting) : "";
      show("tab-spheres-count", waiting > 0);
      if (tab === "spheres" && sph.loaded) drawSpheres();
    }, function () { /* число на вкладке — не главное */ });
  }

  function sphPanel(name) {
    ["sph-loading", "sph-failed", "sph-body"].forEach(function (id) { show(id, id === name); });
  }

  function loadSpheres(quiet) {
    if (!quiet && !sph.loaded) sphPanel("sph-loading");
    return Promise.all([request("GET", "spheres"), request("GET", "spheres/folders")]).then(function (all) {
      var a = all[0], f = all[1];
      if (a.status === 401 || f.status === 401) { sessionLost((a.status === 401 ? a : f).data); return; }
      if (a.status !== 200 || f.status !== 200) {
        $("sph-failed-text").textContent = (a.data.error || f.data.error) || "Сервер не ответил. Попробуйте ещё раз через минуту.";
        sphPanel("sph-failed");
        return;
      }
      sph.data = a.data;
      sph.list = (a.data.spheres || []).map(function (s) { return { id: s.id, title: s.title }; });
      sph.folders = f.data.folders || [];
      sph.loaded = true;
      sphPanel("sph-body");
      drawSpheres();
    }, function () {
      $("sph-failed-text").textContent = "Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.";
      sphPanel("sph-failed");
    });
  }

  function sphMeta(s) {
    var parts = [s.chats + " " + plural(s.chats, "чат", "чата", "чатов")];
    if (s.projects) parts.push(s.projects + " " + plural(s.projects, "проект", "проекта", "проектов"));
    (s.folders || []).forEach(function (f) {
      parts.push("папка «" + (f.title || "№ " + f.folder_id) + "»" + (f.present ? "" : " — её нет в Telegram") + (f.propose ? ": новые предлагаются" : ""));
    });
    return parts.join(" · ");
  }

  function drawSpheres() {
    var data = sph.data || { spheres: [], proposals: [] };
    var offers = $("sph-offers");
    clear(offers);
    (data.proposals || []).forEach(function (p) { offers.appendChild(offerNode(p, function () { loadSpheres(true); })); });
    $("sph-count").textContent = data.spheres.length ? String(data.spheres.length) : "";
    var list = $("sph-list");
    clear(list);
    show("sph-empty", !data.spheres.length);
    data.spheres.forEach(function (s, i) { list.appendChild(sphereItem(s, i, data.spheres)); });
    var folders = $("sph-folders");
    clear(folders);
    show("sph-folders-empty", !sph.folders.length);
    sph.folders.forEach(function (f) { folders.appendChild(folderItem(f)); });
  }

  function changeSphere(method, path, body, b, message) {
    if (b) b.disabled = true;
    return request(method, path, body).then(function (got) {
      if (got.status === 401) { sessionLost(got.data); return false; }
      if (b) b.disabled = false;
      if (got.status !== 200) { toast(got.data.error || "Не получилось."); return false; }
      if (message) toast(message);
      sph.edit = null;
      loadSpheres(true);
      refreshSphereList();
      return got.data;
    }, function () { if (b) b.disabled = false; toast("Нет связи с сервером."); return false; });
  }

  function sphereItem(s, i, all) {
    var li = el("li", "sph");
    li.setAttribute("data-sphere", String(s.id));
    if (sph.edit === s.id) { li.appendChild(editForm(s)); return li; }
    li.appendChild(el("div", "sph-name", s.title));
    if (s.description) li.appendChild(el("div", "sph-desc", s.description));
    li.appendChild(el("div", "sph-meta", sphMeta(s)));
    var actions = el("div", "actions");
    var open = !!sph.open[s.id];
    actions.appendChild(button("secondary", open ? "Скрыть чаты" : "Чаты (" + s.chats + ")", function () { sph.open[s.id] = !open; drawSpheres(); }));
    actions.appendChild(button("secondary", "Изменить", function () { sph.edit = s.id; drawSpheres(); }));
    var up = button("secondary narrow", "↑", function () { move(all, i, -1, up); });
    up.disabled = i === 0;
    up.setAttribute("aria-label", "Выше: " + s.title);
    var down = button("secondary narrow", "↓", function () { move(all, i, 1, down); });
    down.disabled = i === all.length - 1;
    down.setAttribute("aria-label", "Ниже: " + s.title);
    var remove = button("secondary", "Удалить", function () { confirmDelete(s, li, remove); });
    actions.appendChild(remove);
    var order = el("span", "arrows");          // «выше» и «ниже» — всегда рядом
    order.appendChild(up);
    order.appendChild(down);
    actions.appendChild(order);
    li.appendChild(actions);
    if (open) li.appendChild(sphereChatsNode(s));
    return li;
  }

  function move(all, i, step, b) {
    var ids = all.map(function (s) { return s.id; });
    var j = i + step;
    if (j < 0 || j >= ids.length) return;
    var t = ids[i]; ids[i] = ids[j]; ids[j] = t;
    changeSphere("PUT", "spheres/order", { ids: ids }, b, "");
  }

  function confirmDelete(s, li, b) {
    var old = li.querySelector(".confirm");
    if (old) { li.removeChild(old); return; }
    var box = el("div", "confirm");
    box.appendChild(el("p", null, "Удалить сферу «" + s.title + "»? Она будет снята с чатов (" + s.chats + ") и проектов (" +
      s.projects + "). Сами чаты, сохранённые сообщения и проекты останутся."));
    var actions = el("div", "actions");
    var yes = button("danger", "Удалить", function () { changeSphere("DELETE", "spheres/" + s.id, undefined, yes, "Сфера удалена. Чаты и проекты остались."); });
    actions.appendChild(yes);
    actions.appendChild(button("secondary", "Отмена", function () { li.removeChild(box); b.focus(); }));
    box.appendChild(actions);
    li.appendChild(box);
  }

  function editForm(s) {
    var form = el("form", "sph-form");
    form.setAttribute("novalidate", "");
    var title = el("input");
    title.type = "text"; title.maxLength = 60; title.value = s.title; title.setAttribute("aria-label", "Название сферы");
    var about = el("input");
    about.type = "text"; about.maxLength = 300; about.value = s.description || ""; about.placeholder = "Коротко, что в ней";
    about.setAttribute("aria-label", "Описание сферы");
    form.appendChild(title);
    form.appendChild(about);
    var actions = el("div", "actions");
    var save = el("button", "primary", "Сохранить");
    save.type = "submit";
    actions.appendChild(save);
    actions.appendChild(button("secondary", "Отмена", function () { sph.edit = null; drawSpheres(); }));
    form.appendChild(actions);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      changeSphere("PUT", "spheres/" + s.id, { title: title.value, description: about.value }, save, "Сфера сохранена.");
    });
    return form;
  }

  function sphereChatsNode(s) {
    var list = el("ul", "sph-chats");
    list.appendChild(el("li", "muted", "Загружаю чаты…"));
    request("GET", "spheres/" + s.id + "/chats").then(function (got) {
      clear(list);
      if (got.status !== 200) { list.appendChild(el("li", "muted", got.data.error || "Не загрузились.")); return; }
      var items = got.data.chats || [];
      if (!items.length) list.appendChild(el("li", "muted", "В сфере пока нет чатов. Добавьте их кнопкой «Сферы» в строке чата на экране «Чаты»."));
      items.forEach(function (c) {
        var li = el("li");
        var name = el("span", null, c.title || "без названия");
        name.appendChild(el("span", "muted", " · " + (c.enabled ? "читается" : c.excluded ? "не сохраняется" : "не читается")));
        li.appendChild(name);
        var drop = button("link", "Убрать", function () {
          changeSphere("POST", "spheres/" + s.id + "/chats", { account_id: c.account_id, chats: [{ peer_class: c.peer_class, tg_id: c.tg_id }], add: false },
                       drop, "Чат убран из сферы.");
        });
        drop.setAttribute("aria-label", "Убрать из сферы чат " + (c.title || ""));
        li.appendChild(drop);
        list.appendChild(li);
      });
    }, function () { clear(list); list.appendChild(el("li", "muted", "Нет связи с сервером.")); });
    return list;
  }

  function folderItem(f) {
    var li = el("li", "sph");
    li.appendChild(el("div", "sph-name", (f.emoticon ? f.emoticon + " " : "") + f.title));
    var made = (f.spheres || [])[0];
    var linked = (f.spheres || []).filter(function (x) { return x.propose; })[0];
    var meta = f.count + " " + plural(f.count, "чат", "чата", "чатов") + (f.account_role === "assistant" ? " · аккаунт ассистента" : "");
    if (made) meta += " · сфера «" + made.title + "»" + (linked ? ", новые предлагаются" : "");
    li.appendChild(el("div", "sph-meta", meta));
    (f.notes || []).forEach(function (n) { li.appendChild(el("div", "sect-note", n)); });
    var actions = el("div", "actions");
    actions.appendChild(button("secondary", made ? "Дополнить «" + made.title + "»" : "Сделать сферу", function () {
      makeFromFolder(f.account_id, f.folder_id, this, function () { loadSpheres(true); });
    }));
    if (linked) actions.appendChild(button("secondary", "Не предлагать новые", function () {
      linkFolder(linked.id, f.account_id, f.folder_id, false, this, function () { loadSpheres(true); });
    }));
    li.appendChild(actions);
    if (!linked && sph.list.length) li.appendChild(linkRow(f.account_id, f.folder_id, f.title, made, function () { loadSpheres(true); }));
    return li;
  }

  function wireSpheres() {
    $("sph-retry").addEventListener("click", function () { loadSpheres(false); });
    $("sph-new").addEventListener("submit", function (event) {
      event.preventDefault();
      var save = $("sph-new-save");
      changeSphere("POST", "spheres", { title: $("sph-new-title").value, description: $("sph-new-description").value }, save, "").then(function (data) {
        if (!data) return;
        $("sph-new-title").value = "";
        $("sph-new-description").value = "";
        toast("Сфера «" + data.sphere.title + "» заведена.");
      });
    });
  }

  /* ------------------------------------------------------------ «Уведомления» (Р-84) */

  // Здесь — только посмотреть, выключить лишнее и выбрать время: включить больше можно только на
  // странице «Переписка» (сервер отвергает расширение из приложения, код page_only).
  var nt = { data: null, loaded: false };
  var NT_OFF = [
    ["personal", "off", "Не сообщать о личных сообщениях", function (s) { return s.personal !== "off"; }],
    ["groups", false, "Не сообщать об обращениях в группах", function (s) { return s.groups; }],
    ["preview", false, "Не показывать текст сообщений", function (s) { return s.preview; }]
  ];

  function ntPanel(name) {
    ["nt-loading", "nt-failed", "nt-body"].forEach(function (id) { show(id, id === name); });
  }

  function loadNotify(quiet) {
    if (!quiet && !nt.loaded) ntPanel("nt-loading");
    return request("GET", "notify").then(function (got) {
      if (got.status === 401) { sessionLost(got.data); return; }
      if (got.status !== 200) {
        $("nt-failed-text").textContent = got.data.error || "Сервер не ответил. Попробуйте ещё раз через минуту.";
        ntPanel("nt-failed");
        return;
      }
      nt.data = got.data;
      nt.loaded = true;
      ntPanel("nt-body");
      drawNotify();
    }, function () {
      $("nt-failed-text").textContent = "Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.";
      ntPanel("nt-failed");
    });
  }

  function drawNotify() {
    var d = nt.data, s = d.settings;
    show("nt-nobot", !d.own_bot);
    var now = $("nt-now");
    now.textContent = "";
    (d.describe || []).forEach(function (line) { now.appendChild(el("li", "", line.replace(/^• /, ""))); });
    var box = $("nt-off");
    box.textContent = "";
    var any = false;
    NT_OFF.forEach(function (item) {
      if (!item[3](s)) return;
      any = true;
      var button = el("button", "secondary", item[2]);
      button.type = "button";
      button.addEventListener("click", function () {
        var body = {};
        body[item[0]] = item[1];
        saveNotify(body, button, "Готово: " + item[2].toLowerCase() + ".");
      });
      box.appendChild(button);
    });
    show("nt-off-none", !any);
    $("nt-when").value = (s.mode === "digest" ? "d" + s.digest_minutes : "i" + s.gap_minutes);
    $("nt-quiet").checked = s.quiet;
    $("nt-from").value = s.quiet_from;
    $("nt-to").value = s.quiet_to;
    $("nt-from").disabled = $("nt-to").disabled = !s.quiet;
  }

  function saveNotify(body, button, done) {
    if (busy) return Promise.resolve();
    busy = true;
    if (button) button.disabled = true;
    return request("PUT", "notify", body).then(function (got) {
      busy = false;
      if (button) button.disabled = false;
      if (got.status === 401) { sessionLost(got.data); return; }
      if (got.status !== 200) { toast(got.data.error || "Не получилось. Попробуйте ещё раз."); return; }
      nt.data = got.data;
      drawNotify();
      toast(done);
    }, function () {
      busy = false;
      if (button) button.disabled = false;
      toast("Нет связи с сервером.");
    });
  }

  function wireNotify() {
    $("nt-retry").addEventListener("click", function () { loadNotify(false); });
    $("nt-quiet").addEventListener("change", function () {
      $("nt-from").disabled = $("nt-to").disabled = !$("nt-quiet").checked;
    });
    $("nt-when-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var when = $("nt-when").value, minutes = Number(when.slice(1));
      var body = when.charAt(0) === "d" ? { mode: "digest", digest_minutes: minutes } : { mode: "instant", gap_minutes: minutes };
      body.quiet = $("nt-quiet").checked;
      if (body.quiet) { body.quiet_from = $("nt-from").value; body.quiet_to = $("nt-to").value; }
      saveNotify(body, $("nt-save"), "Сохранено.");
    });
  }

  /* ------------------------------------------------------------ запуск */

  $("refresh").addEventListener("click", function () { load(); });
  $("tab-approvals").addEventListener("click", function () { openTab("approvals"); });
  $("tab-chats").addEventListener("click", function () { openTab("chats"); });
  $("tab-spheres").addEventListener("click", function () { openTab("spheres"); });
  $("tab-notify").addEventListener("click", function () { openTab("notify"); });
  wireChats();
  wireSpheres();
  wireNotify();
  try {
    var saved = sessionStorage.getItem(TAB_NAME);
    tab = saved === "chats" || saved === "spheres" || saved === "notify" ? saved : "approvals";
  } catch (e) { tab = "approvals"; }
  openTab(tab);
  $("retry").addEventListener("click", function () {
    screen("screen-wait");
    load();
  });
  $("home-screen").addEventListener("click", function () { postEvent("web_app_add_to_home_screen"); });

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState !== "visible" || !key) return;
    load();
    // Список чатов — из базы сервиса, без запроса к Telegram: можно перечитать при возвращении.
    if (tab === "chats" && chats.opened && !chats.loading) loadAccounts(true);
    if (tab === "spheres") loadSpheres(true);
    if (tab === "notify") loadNotify(true);
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
