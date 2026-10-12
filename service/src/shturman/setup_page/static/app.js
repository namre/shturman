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
 * Всё, что приходит с сервера, выводится только через textContent: в карточках есть чужой текст.
 */
(function () {
  "use strict";

  var API = "../api/app/";
  var KEY_NAME = "shturman-app-session";
  var THEME_NAME = "shturman-app-theme";
  var TELEGRAM_WEB = "https://web.telegram.org";
  var key = "";
  var params = parseHash(location.hash);
  var initData = params.tgWebAppData || "";       // только в памяти: годится один раз
  var busy = false;
  var toastTimer = null;
  var listTimer = null;

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

  function screen(name) {
    ["screen-wait", "screen-outside", "screen-failed", "screen-approvals"].forEach(function (id) {
      show(id, id === name);
    });
    show("refresh", name === "screen-approvals");
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

  function card(item) {
    var node = el("article", "card");
    node.appendChild(el("div", "kind", item.kind_title));
    node.appendChild(el("h2", null, item.title));
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
    screen("screen-approvals");
  }

  function load() {
    return request("GET", "approvals").then(function (got) {
      if (got.status === 200) { render(got.data); return; }
      if (got.status === 401) {
        saveKey("");
        if (initData) return signIn().then(function (ok) { if (ok) return load(); });
        $("failed-text").textContent = got.data.error || "Вход устарел. Закройте приложение и откройте его снова.";
        screen("screen-failed");
        return;
      }
      toast(got.data.error || "Не получилось загрузить список. Попробуйте ещё раз.");
    }, function () { toast("Нет связи с сервером. Попробуйте ещё раз."); });
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

  /* ------------------------------------------------------------ запуск */

  $("refresh").addEventListener("click", function () { load(); });
  $("home-screen").addEventListener("click", function () { postEvent("web_app_add_to_home_screen"); });

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible" && key) load();
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
