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
 * какие чаты читает ассистент (то же, что шаг «Какие чаты читать» в «Настройках Штурмана», Р-80).
 * Список чатов сервис отдаёт из базы; обновить его из Telegram можно только кнопкой «Обновить список
 * из Telegram». Загрузка идёт в фоне (Р-81): экран показывает её ход, а если Telegram велит
 * подождать — отсчёт; потом загрузка продолжится сама, с того же места.
 *
 * Пакет 2 ревизии удобства: ответ на «Вопрос ассистента» полем «Ваш ответ» (П1), «Остановить всё» и
 * «Продолжить…» (Р-87: остановка сразу, продолжение — только нажатием в боте), кнопка «Открыть
 * настройки в браузере» (П4: web_app_open_link, адрес «Настроек Штурмана» отдаёт сервер) и вкладка
 * «Черновики» (Р-89, Р-105; внутреннее имя rehearsal: кому готовить черновики ответов, предел в сутки и пауза,
 *   последние карточки и оценки за 7 дней).
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
  var tab = "approvals";                         // открытый раздел: approvals | commitments | chats | spheres | rehearsal | notify | history
  var TABS = ["approvals", "commitments", "chats", "spheres", "memory", "rehearsal", "notify", "history"];
  var GROUPS_NAME = "shturman-app-groups";       // свёрнутые разделы (только эта вкладка Telegram, sessionStorage)
  var halt = { stopped: false };                 // «Остановить всё»: из списка согласований (и GET halt)
  var setupUrl = null;                           // адрес «Настроек Штурмана» — для кнопки «Открыть настройки в браузере»

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
    fixContrast();
  }

  /* Контраст не ниже 4,5:1 (П5). Цвета темы Telegram бывают бледнее: подсказка #8e8e93 на белом — 3,3:1,
   * синий #007aff — 4,0:1 и как текст, и как фон кнопки под белой надписью. Поправка — сдвиг того же цвета
   * к чёрному (на светлом фоне) или к белому (на тёмном), пока контраст не станет достаточным; оттенок
   * остаётся темы. Цвет, которому хватает контраста, не трогается. */
  function rgbOf(value) {
    var s = String(value || "").trim(), m;
    if ((m = /^#([0-9a-f]{6})$/i.exec(s))) return [0, 2, 4].map(function (i) { return parseInt(m[1].substr(i, 2), 16); });
    if ((m = /^rgba?\(([^)]+)\)$/i.exec(s))) {
      var parts = m[1].split(/[\s,\/]+/).filter(Boolean).map(Number);
      if (parts.length >= 3 && (parts.length < 4 || parts[3] > 0.5)) return parts.slice(0, 3);
    }
    return null;
  }
  function luminance(c) {
    var f = function (v) { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
  }
  function ratio(a, b) { var x = luminance(a), y = luminance(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); }
  function hex(c) { return "#" + c.map(function (v) { return ("0" + Math.round(Math.max(0, Math.min(255, v))).toString(16)).slice(-2); }).join(""); }
  // fg против всех фонов: смешиваем с чёрным или белым шагами по 4 %, пока хуже всего не станет ≥ need
  // (берём 4,6 — с запасом на округление цвета).
  function readable(fg, grounds, need) {
    var worst = function (c) { return Math.min.apply(null, grounds.map(function (g) { return ratio(c, g); })); };
    if (worst(fg) >= need) return null;
    var dark = grounds.every(function (g) { return luminance(g) < 0.18; }), target = dark ? [255, 255, 255] : [0, 0, 0];
    for (var k = 1; k <= 25; k++) {
      var c = fg.map(function (v, i) { return v + (target[i] - v) * k * 0.04; });
      if (worst(c) >= need) return hex(c);
    }
    return hex(target);
  }
  function fixContrast() {
    var root = document.documentElement, style = root.style;
    ["--hint-fix", "--accent-fix", "--button-fix", "--danger-fix"].forEach(function (n) { style.removeProperty(n); });
    var cs = getComputedStyle(root);
    var get = function (name) { return rgbOf(cs.getPropertyValue(name)); };
    var bg = get("--bg"), section = get("--section"), text = get("--button-text");
    if (!bg || !section) return;
    var grounds = [bg, section];
    var hint = get("--hint-src"), accent = get("--accent-src"), danger = get("--danger-src"), button = get("--button-src");
    var fixed = {
      "--hint-fix": hint && readable(hint, grounds, 4.6),
      "--accent-fix": accent && readable(accent, grounds, 4.6),
      "--danger-fix": danger && readable(danger, grounds, 4.6),
      // кнопка — фон под надписью цвета button_text: двигаем фон, а не надпись
      "--button-fix": button && text && readable(button, [text], 4.6)
    };
    Object.keys(fixed).forEach(function (n) { if (fixed[n]) style.setProperty(n, fixed[n]); });
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

  // Какой раздел открыть первым — из адреса кнопки бота (?open=…); других данных в адресе нет.
  var openParam = (/[?&]open=([a-z]+)/.exec(location.search) || [])[1] || "";

  // Данные входа больше не нужны в адресной строке: они уйдут на сервер один раз. Раздел из адреса —
  // тоже: после перезагрузки откроется тот, что был открыт.
  if (location.hash || openParam) {
    try { history.replaceState(null, "", location.pathname); } catch (e) { /* пусть */ }
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
    TABS.forEach(function (one) { show("screen-" + one, signedIn && tab === one); });
    show("refresh", signedIn && tab === "approvals");
    show("halt-stop", signedIn && !halt.stopped);
    show("halt-banner", signedIn && halt.stopped);
  }

  function openTab(name) {
    tab = TABS.indexOf(name) >= 0 ? name : "approvals";
    try { sessionStorage.setItem(TAB_NAME, tab); } catch (e) { /* не страшно */ }
    TABS.forEach(function (one) {
      $("tab-" + one).setAttribute("aria-selected", one === tab ? "true" : "false");
    });
    if (signedIn) screen("main");
    if (tab === "chats") { chatsOpened(); schedulePoll(); }
    if (tab === "spheres" && signedIn) loadSpheres(false);
    if (tab === "rehearsal" && signedIn) loadRehearsal(false);
    if (tab === "notify" && signedIn) loadNotify(false);
    if (tab === "commitments" && signedIn) loadCommitments(false);
    if (tab === "notify" && signedIn) { loadNotify(false); loadSummaries(); }
    if (tab === "history" && signedIn) loadHistory(false);
    if (tab === "memory" && signedIn) loadMemory(false);
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
      commitment: "договорённостей", owner_fact: "фактов о вас", project_fact: "значений проектов",
      page: "страниц о людях",
      project: "проектов"
    };
    var parts = [];
    Object.keys(words).forEach(function (name) {
      var n = later && later[name];
      if (n) parts.push(words[name] + ": " + n);
    });
    if (!parts.length) return "";
    return "Ещё ждут показа — " + parts.join(", ") + ". Они придут со следующей сводкой в боте; " +
      "решить их сразу можно в «Настройках Штурмана», раздел «Память ассистента».";
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
    node.id = cardId(item.key);
    node.tabIndex = -1;
    node.appendChild(el("div", "kind", item.kind_title));
    if (item.title && item.title !== item.kind_title) node.appendChild(el("h2", null, item.title));
    if (item.head && item.head.length) {
      var lines = el("ul", "lines");
      item.head.forEach(function (line) { lines.appendChild(el("li", null, line)); });
      node.appendChild(lines);
    }
    if (item.context && item.context.length) {
      // Разговор перед черновиком (Р-103): свёрнут; только последние строки, как в карточке бота.
      var talk = el("details", "context");
      talk.appendChild(el("summary", null, "Разговор — последние сообщения"));
      var said = el("ul", "lines");
      item.context.forEach(function (line) { said.appendChild(el("li", null, line)); });
      talk.appendChild(said);
      node.appendChild(talk);
    }
    if (item.kind === "draft" || item.kind === "suggest") {
      node.appendChild(el("div", "verbatim-label", "Текст сообщения дословно:"));
      node.appendChild(el("div", "verbatim", item.text));
    } else if (item.text) {
      node.appendChild(el("div", "body", item.text));
    }
    if (item.note) node.appendChild(el("div", "note", item.note));
    var until = deadline(item.expires_at);
    if (until) node.appendChild(el("div", "note", "Ждёт решения до " + until + "."));
    if (item.answer) node.appendChild(answerBox(item));
    var actions = el("div", "actions");
    // У договорённости «Не так» — с причиной (Р-96): кнопка открывает причины; «Без причины» — прежнее «Не так».
    var why = (item.actions || []).filter(function (action) { return action.why; });
    (item.actions || []).forEach(function (action) {
      if (action.why) return;
      var button = el("button", action.primary ? "primary" : "secondary", action.label);
      button.type = "button";
      if (why.length && action.ref.indexOf("sh:cm:r:") === 0) {
        button.addEventListener("click", function () { whyBox(node, action, why, actions, button); });
      } else {
        button.addEventListener("click", function () { decide(action.ref, actions); });
      }
      actions.appendChild(button);
    });
    node.appendChild(actions);
    return node;
  }

  function whyBox(node, plain, why, actions, opener) {
    var had = node.querySelector(".why");
    if (had) { had.parentNode.removeChild(had); opener.focus(); return; }
    var box = el("div", "confirm why");
    box.appendChild(el("p", null, "Что не так? Причина поможет точнее находить договорённости."));
    var chips = el("div", "chips");
    why.forEach(function (action) {
      var b = el("button", "chip-btn", action.label);
      b.type = "button";
      b.addEventListener("click", function () { decide(action.ref, actions); });
      chips.appendChild(b);
    });
    box.appendChild(chips);
    var rest = el("div", "actions");
    var skip = el("button", "secondary", "Без причины");
    skip.type = "button";
    skip.addEventListener("click", function () { decide(plain.ref, actions); });
    rest.appendChild(skip);
    box.appendChild(rest);
    node.appendChild(box);
  }

  // Сколько осталось до срока — словами: «осталось 12 мин», «осталось меньше минуты».
  function leftWords(value) {
    var at = new Date(value).getTime();
    if (!value || isNaN(at)) return "";
    var minutes = Math.floor((at - Date.now()) / 60000);
    if (minutes < 0) return "Срок вышел — ассистент, скорее всего, уже не ждёт ответа.";
    if (minutes < 1) return "Осталось меньше минуты.";
    if (minutes < 60) return "Осталось " + minutes + " мин.";
    var hours = Math.floor(minutes / 60);
    return "Осталось " + hours + " ч " + (minutes % 60) + " мин.";
  }

  // «Вопрос ассистента»: поле «Ваш ответ» и кнопка «Ответить» — тот же путь на сервере, что кнопка
  // «Ответить» в боте и команда /replytask N answer (replies.owner.answer).
  function answerBox(item) {
    var box = el("form", "answer");
    box.setAttribute("novalidate", "");
    var id = "answer-" + item.id;
    var label = el("label", "field-label", "Ваш ответ");
    label.setAttribute("for", id);
    var input = el("textarea");
    input.id = id;
    input.maxLength = item.answer.max || 2000;
    input.rows = 3;
    input.placeholder = "Одним сообщением — ассистент передаст его по смыслу";
    var left = el("div", "left", leftWords(item.expires_at));
    var send = el("button", "primary", "Ответить");
    send.type = "submit";
    box.appendChild(label);
    box.appendChild(input);
    box.appendChild(left);
    box.appendChild(send);
    box.addEventListener("submit", function (event) {
      event.preventDefault();
      var text = input.value.trim();
      if (!text) { toast("Напишите ответ — пустой ассистенту не передать."); input.focus(); return; }
      if (busy) return;
      busy = true;
      send.disabled = true;
      request("POST", "approvals/answer", { ref: item.answer.ref, text: text }).then(function (got) {
        busy = false;
        send.disabled = false;
        if (got.status === 401) { sessionLost(got.data); return; }
        if (got.status !== 200) { toast(got.data.error || "Ответ не передан. Попробуйте ещё раз."); load(); return; }
        input.value = "";
        toast(got.data.answer || "Ответ передан ассистенту.");
        load();
      }, function () {
        busy = false;
        send.disabled = false;
        toast("Нет связи с сервером. Ответ не передан — попробуйте ещё раз.");
      });
    });
    return box;
  }

  function render(data) {
    drawHalt(data.halt || { stopped: false });
    setupUrl = typeof data.setup_url === "string" && data.setup_url.indexOf("https://") === 0 ? data.setup_url : null;
    Array.prototype.forEach.call(document.querySelectorAll(".open-setup"), function (b) { b.hidden = !setupUrl; });
    // Список обновляется раз в минуту: пока владелец пишет ответ, карточки не перерисовываем.
    var active = document.activeElement;
    if (listShown && active && active.tagName === "TEXTAREA" && active.value.trim() && active.closest(".answer")) return;
    var items = data.items || [];
    approvalsData = data;
    drawGroups(items, data.groups || []);
    drawSoon(items);
    drawNow();
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
    if (first && tab === "rehearsal") loadRehearsal(false);
    if (first && tab === "notify") loadNotify(false);
    if (first && tab === "commitments") loadCommitments(false);
    if (first && tab === "notify") { loadNotify(false); loadSummaries(); }
    if (first && tab === "history") loadHistory(false);
    if (first && tab === "memory") loadMemory(false);
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
      if (got.status === 200) { render(got.data); loadNow(); return; }
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

  /* ------------------------------------------------- разделы «Согласований» и «Сейчас» (П3, Р-94) */

  // Карточки — по разделам («Ответы», «Разрешения», «Договорённости», «Память», «Защита»), разделы
  // сворачиваются; свёрнутое помнится до закрытия приложения. Внутри раздела сначала то, что скоро
  // истечёт. Сверху — «Сейчас»: сколько в каждом разделе (нажатие — к разделу), договорённости на
  // сегодня и просроченные (нажатие — экран «Договорённости») и что работает — только числа и время.
  var SOON_MINUTES = 20;                         // «скоро истечёт» — меньше 20 минут на решение
  var approvalsData = null;                      // последний ответ app/approvals
  var nowData = null;                            // последний ответ app/now

  function cardId(key) { return "card-" + String(key).replace(/[^a-z0-9_-]/gi, "-"); }

  function stored(name) {
    try { return JSON.parse(sessionStorage.getItem(name) || "{}") || {}; } catch (e) { return {}; }
  }
  function isOpen(key, fallback) {
    var map = stored(GROUPS_NAME);
    return Object.prototype.hasOwnProperty.call(map, key) ? Boolean(map[key]) : fallback;
  }
  function rememberOpen(key, open) {
    var map = stored(GROUPS_NAME);
    map[key] = Boolean(open);
    try { sessionStorage.setItem(GROUPS_NAME, JSON.stringify(map)); } catch (e) { /* не страшно */ }
  }

  function minutesLeft(value) {
    var at = new Date(value).getTime();
    if (!value || isNaN(at)) return null;
    return Math.floor((at - Date.now()) / 60000);
  }
  function leftShort(value) {
    var m = minutesLeft(value);
    if (m === null) return "";
    if (m < 1) return "осталось меньше минуты";
    if (m < 60) return "осталось " + m + " мин";
    return "осталось " + Math.floor(m / 60) + " ч " + (m % 60) + " мин";
  }
  function soonItems(items) {
    return items.filter(function (i) { var m = minutesLeft(i.expires_at); return m !== null && m >= 0 && m < SOON_MINUTES; })
      .sort(function (a, b) { return new Date(a.expires_at) - new Date(b.expires_at); });
  }

  function drawGroups(items, groups) {
    var list = $("list");
    clear(list);
    var byGroup = {};
    items.forEach(function (item) {
      var key = item.group || "permissions";
      (byGroup[key] = byGroup[key] || []).push(item);
    });
    groups.forEach(function (g) {
      var mine = byGroup[g.key] || [];
      if (!mine.length) return;
      var soon = soonItems(mine);
      var rest = mine.filter(function (i) { return soon.indexOf(i) < 0; });
      var box = el("details", "sect grp");
      box.id = "grp-" + g.key;
      box.open = isOpen("a:" + g.key, true);
      var head = el("summary");
      head.appendChild(el("span", "sect-title", g.title));
      head.appendChild(el("span", "sect-count on", String(mine.length)));
      // ближайший срок решения в разделе; меньше SOON_MINUTES — выделен
      var timed = mine.filter(function (i) { var m = minutesLeft(i.expires_at); return m !== null && m >= 0; })
        .sort(function (a, b) { return new Date(a.expires_at) - new Date(b.expires_at); });
      if (timed.length) head.appendChild(el("span", "soon-tag" + (soon.length ? " hot" : ""), leftShort(timed[0].expires_at)));
      box.appendChild(head);
      var body = el("div", "sect-body grp-body");
      soon.concat(rest).forEach(function (item) { body.appendChild(card(item)); });
      box.appendChild(body);
      head.addEventListener("click", function () { rememberOpen("a:" + g.key, !box.open); });
      list.appendChild(box);
    });
  }

  // Раскрыть раздел и показать карточку или сам раздел.
  function reveal(target) {
    if (!target) return;
    var box = target.tagName === "DETAILS" ? target : target.closest("details");
    if (box && !box.open) box.open = true;
    try { target.scrollIntoView({ block: "start", behavior: "smooth" }); } catch (e) { target.scrollIntoView(); }
    var focusable = target.tagName === "DETAILS" ? target.querySelector("summary") : target;
    if (focusable && focusable.focus) {
      try { focusable.focus({ preventScroll: true }); } catch (e) { focusable.focus(); }
    }
  }

  function drawSoon(items) {
    var soon = soonItems(items);
    var box = $("soon-list");
    clear(box);
    soon.forEach(function (item) {
      var b = el("button", "soon-item", item.kind_title + " — " + leftShort(item.expires_at));
      b.type = "button";
      b.addEventListener("click", function () { reveal($(cardId(item.key))); });
      box.appendChild(b);
    });
    show("soon", soon.length > 0);
  }

  var ACCOUNT_ROLE = { owner: "Ваш аккаунт", assistant: "Аккаунт помощника", business: "Бизнес-бот в личных чатах" };
  var STATE_MARK = { ok: "✓", paused: "⏸", wait: "…", off: "⚠" };

  function loadNow() {
    return request("GET", "now").then(function (got) {
      if (got.status !== 200) return;            // о входе скажет список; «Сейчас» — по возможности
      nowData = got.data;
      drawNow();
      drawCommitmentsBadge();
    }, function () { /* список остаётся */ });
  }

  function drawNow() {
    var groups = (approvalsData && approvalsData.groups) || [];
    var chips = $("now-groups");
    clear(chips);
    var total = 0;
    groups.forEach(function (g) {
      if (!g.count) return;
      total += g.count;
      var b = el("button", "chip-btn");
      b.type = "button";
      b.appendChild(el("span", null, g.title));
      b.appendChild(el("span", "chip-n", String(g.count)));
      b.addEventListener("click", function () { reveal($("grp-" + g.key)); });
      chips.appendChild(b);
    });
    show("now-groups", total > 0);
    show("now-groups-label", total > 0);
    show("now-none", Boolean(approvalsData) && total === 0);
    if (!nowData) { show("now-commitments", false); show("now-more", false); return; }
    var c = nowData.commitments || {};
    var row = $("now-commitments");
    clear(row);
    row.appendChild(el("span", "now-row-title", "Договорённости на сегодня"));
    var parts = [];
    if (c.overdue) parts.push("просрочено " + c.overdue);
    if (c.today) parts.push("сегодня " + c.today);
    row.appendChild(el("span", "now-row-value" + (c.overdue ? " late" : ""), parts.length ? parts.join(" · ") : "на сегодня нет"));
    row.appendChild(el("span", "now-row-go", "›"));
    show("now-commitments", true);

    var lines = $("now-lines");
    clear(lines);
    var problems = [];
    function line(mark, text) {
      var li = el("li", "now-line");
      li.appendChild(el("span", "now-mark", mark));
      li.appendChild(el("span", null, text));
      lines.appendChild(li);
    }
    var ORDER = { owner: 0, assistant: 1, business: 2 };
    var accounts = (nowData.accounts || []).slice().sort(function (a, b) { return (ORDER[a.role] || 9) - (ORDER[b.role] || 9); });
    if (!accounts.length) {
      line("⚠", "Аккаунт Telegram не подключён — подключается в «Настройках Штурмана»");
      problems.push("аккаунт не подключён");
    }
    accounts.forEach(function (a) {
      var label = a.label && a.role !== "business" ? " «" + a.label + "»" : "";
      line(STATE_MARK[a.state] || "⚠", (ACCOUNT_ROLE[a.role] || "Аккаунт") + label + " — " + a.words);
      if (a.state === "off") problems.push("нет связи с аккаунтом");
    });
    line("•", "Последнее сообщение в архиве: " + (nowData.last_message_at ? deadline(nowData.last_message_at) : "ещё не было"));
    var failed = nowData.model_failed_24h || {};
    var fails = (failed.media || 0) + (failed.requests || 0);
    if (fails) {
      line("⚠", "Модель: не получилось за сутки — " + fails + ". Повторить можно в «Настройках Штурмана»");
      problems.push("сбои модели");
    } else {
      line("✓", "Модель: сбоев за сутки нет");
    }
    line("•", nowData.sending === "on" ? "Отправка: включена"
      : "Отправка: выключена — ничего не уходит от вашего имени; черновики ответов приходят вам");
    line("•", nowData.triage ? "Разбор входящих: включён" : "Разбор входящих: выключен — включается в «Настройках Штурмана»");
    var stopped = Boolean((nowData.halt || {}).stopped || halt.stopped);
    line(stopped ? "⏸" : "✓", stopped ? "Ассистент: остановлен — продолжить можно в полосе вверху" : "Ассистент: работает");
    if (stopped) problems.push("ассистент остановлен");
    var haltBox = $("now-halt");
    clear(haltBox);
    if (!stopped) {
      // та же кнопка, что в шапке: одно и то же действие, своей логики нет
      var stop = el("button", "stop", "Остановить всё");
      stop.type = "button";
      stop.addEventListener("click", function () { $("halt-stop").click(); });
      haltBox.appendChild(stop);
    }
    drawUpdate(nowData.update, line, problems);
    var seen = nowData.last_message_at ? "последнее сообщение " + deadline(nowData.last_message_at) : "сообщений ещё не было";
    var summary = $("now-summary");
    summary.textContent = problems.length ? "⚠ Внимание: " + problems.join(", ") : "Всё работает · " + seen;
    summary.className = problems.length ? "warn" : "";
    $("now-more").open = isOpen("now", problems.length > 0);
    show("now-more", true);
  }

  // Плашка новой версии и строка «Версия» в «Что работает» (Р-101). Тексты — из проверенного списка выпусков
  // и только как текст (textContent): ничего из него не исполняется и не становится разметкой.
  var UPDATE_RESULT = { "ok": "✓", "rolled-back": "⚠", "not-installed": "⚠", "failed-needs-human": "⚠", "rollback-failed": "⚠" };
  var UPDATE_WORDS = {
    "ok": "обновлено — проверки прошли",
    "rolled-back": "обновление не встало — всё вернули как было",
    "not-installed": "обновление не установлено — работает прежняя версия, данные не тронуты",
    "failed-needs-human": "обновление прошло с ошибками — попросите агента, который сопровождает сервер, «откатить последнее обновление»",
    "rollback-failed": "обновление не встало, и откат не удался — нужен тот, кто сопровождает сервер"
  };
  function drawUpdate(u, line, problems) {
    var box = $("now-update");
    if (!u) { show("now-update", false); return; }
    var a = u.available;
    if (a) {
      $("now-update-title").textContent = (a.security ? "Исправление безопасности: " : "Новая версия «Штурмана»: ") + a.tag
        + (a.rc ? " (кандидат выпуска)" : "");
      var list = $("now-update-changes");
      clear(list);
      (a.changes || []).forEach(function (c) { list.appendChild(el("li", null, c)); });
      var meta = [];
      if (a.count > 1) meta.push("выпусков впереди: " + a.count);
      if (a.downtime_minutes === 0) meta.push("без перерыва в работе");
      else if (a.downtime_minutes) meta.push("перерыв около " + a.downtime_minutes + " мин");
      if (a.owner_steps && a.owner_steps.length) meta.push("понадобятся ваши действия: " + a.owner_steps.join("; "));
      $("now-update-meta").textContent = meta.join(" · ");
      show("now-update-meta", meta.length > 0);
      $("now-update-codex").textContent = (a.codex_text || "").replace(/^[^\n]*\n\n/, "");
      box.className = "upd" + (a.security ? " security" : "");
      if (a.security) problems.push("исправление безопасности");
    }
    show("now-update", Boolean(a));
    var last = u.last;
    var text = "Версия: " + (u.running || "?");
    if (last && UPDATE_WORDS[last.result]) text += " · " + deadline(last.at) + " " + UPDATE_WORDS[last.result];
    else if (u.problem === "expired" || u.problem === "invalid") text += " · проверить новые версии сейчас нельзя (список выпусков на GitHub устарел)";
    else if (u.enabled === false) text += " · проверка новых версий выключена";
    else if (!a) text += " · новых версий нет";
    line(last && UPDATE_RESULT[last.result] ? UPDATE_RESULT[last.result] : "•", text);
    if (last && last.result !== "ok") problems.push("обновление");
  }

  function copyCodex() {
    var text = $("now-update-codex").textContent || "";
    function done() { toast("Текст для агента скопирован."); }
    function fallback() {
      try {
        var range = document.createRange();
        range.selectNodeContents($("now-update-codex"));
        var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
        if (document.execCommand && document.execCommand("copy")) { done(); return; }
      } catch (e) { /* ниже — подсказка */ }
      toast("Выделите текст и скопируйте его вручную.");
    }
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, fallback);
    else fallback();
  }

  function wireNow() {
    // помнится только нажатие владельца (по умолчанию «что работает» раскрыто, когда что-то не так)
    $("now-summary").addEventListener("click", function () { rememberOpen("now", !$("now-more").open); });
    $("now-commitments").addEventListener("click", function () { openTab("commitments"); });
    $("now-update-copy").addEventListener("click", copyCodex);
  }

  /* ------------------------------------------------------------ «Чаты» */

  // Тот же выбор, что в шаге «Какие чаты читать» в «Настройках Штурмана» (setup.js: loadChats,
  // chatRow, drawLoad): поиск, вид, «только выбранные», глубина истории, исключение. Отличия:
  // список — из базы сервиса (обновить из Telegram — только кнопкой «Обновить список из Telegram»),
  // чаты включаются по одному, исключение не стирает уже сохранённое.

  var FIRST_CHATS = 20, MORE_CHATS = 20;           // та же порция, что на странице (С11)
  var KIND_NAMES = { personal: "личный", group: "группа", channel: "канал" };
  var chats = {
    accounts: [], account: null, mine: null, items: [], total: 0, enabledTotal: 0, dialogsTotal: null, loaded: true, age: null,
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
      button.appendChild(el("span", "seg-role", a.role === "owner" ? "Ваш аккаунт" : "Аккаунт помощника"));
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
      $("chats-offline-text").textContent = (a.role === "owner" ? "Ваш аккаунт" : "Аккаунт помощника") +
        (a.label ? " «" + a.label + "»" : "") + " сейчас не подключён, поэтому список его чатов недоступен. " +
        "Войдите в него или снимите его с паузы в «Настройках Штурмана».";
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
      if (chats.busy > 0 || chats.loading || document.visibilityState !== "visible" ||
          document.querySelector("#screen-chats .confirm, #screen-chats .pick, #screen-chats .folder-pick")) { schedulePoll(); return; }
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
      if (typeof got.data.dialogs_total === "number") chats.dialogsTotal = got.data.dialogs_total;
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
    var n = chats.enabledTotal, a = currentAccount();
    // «Читается 12 чатов из 1 500 в вашем Telegram» — те же слова, что на странице (С11).
    var words = "Читается " + number(n) + " " + plural(n, "чат", "чата", "чатов") +
      (chats.loaded && chats.dialogsTotal !== null ? " из " + number(chats.dialogsTotal) +
        (a && a.role === "assistant" ? " у помощника в Telegram" : " в вашем Telegram") : "") + ".";
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
      "из аккаунта, бизнес-режима и выгрузки. Уже сохранённое останется — стереть его можно только в «Настройках Штурмана»."));
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

  // Те же разделы, что в «Настройках Штурмана» (setup.js: loadSections, drawSectionBody): папки Telegram,
  // сферы, активность, вид — со счётчиком «читается X из Y». Отличие от страницы: «Включить все» здесь
  // нет (Р-80 — включать чтение из приложения можно только по одному чату), «Выключить все» есть, и
  // о нём бот согласований пришлёт сообщение.
  var SECTION_FIRST = 20, SECTION_MORE = 20;
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
    if (actions.firstChild) body.appendChild(actions);
    if (folderId !== null) {
      body.appendChild(folderLinkTools({ account_id: chats.account, folder_id: folderId, title: sec.title, spheres: sec.spheres || [] },
                                       function () { loadDialogs(true); }));
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

  /* Папка Telegram и сфера (С12) — как на странице (setup.js: folderLinkTools): одна кнопка «Связать со
   * сферой…»; внутри — «Новая сфера из папки» или одна из ваших (по умолчанию ничего: «Выберите сферу…») и
   * флажок «Предлагать новые чаты папки». */
  function folderLinkTools(f, done) {
    var made = (f.spheres || [])[0];
    var linked = (f.spheres || []).filter(function (x) { return x.propose; })[0];
    var box = el("div", "folder-link");
    if (made) box.appendChild(el("p", "sect-note", "Связана со сферой «" + made.title + "»" + (linked ? ", новые чаты папки предлагаются." : ".")));
    var actions = el("div", "actions");
    var open = button("secondary", "Связать со сферой…", function () {
      var old = box.querySelector(".folder-pick");
      if (old) { box.removeChild(old); open.setAttribute("aria-expanded", "false"); pickClosed(); return; }
      open.setAttribute("aria-expanded", "true");
      var pick = el("div", "pick folder-pick");
      var select = el("select");
      select.setAttribute("aria-label", "Сфера для чатов папки «" + f.title + "»");
      var first = el("option", null, "Выберите сферу…"); first.value = ""; select.appendChild(first);
      if (!made) { var fresh = el("option", null, "Новая сфера «" + f.title + "» из папки"); fresh.value = "new"; select.appendChild(fresh); }
      sph.list.forEach(function (s) { var o = el("option", null, s.title); o.value = String(s.id); select.appendChild(o); });
      select.value = "";
      pick.appendChild(select);
      var label = el("label", "check");
      var propose = el("input"); propose.type = "checkbox"; propose.checked = true;
      label.appendChild(propose);
      label.appendChild(el("span", null, "Предлагать новые чаты папки — сами они в сферу не попадут"));
      pick.appendChild(label);
      var row = el("div", "actions");
      var go = button("primary", "Связать", function () {
        linkWith(f, select.value, propose.checked, go, function () {
          if (pick.parentNode) box.removeChild(pick);
          open.setAttribute("aria-expanded", "false");
          pickClosed();
          if (done) done();
        });
      });
      go.disabled = true;
      select.addEventListener("change", function () { go.disabled = !select.value; });
      row.appendChild(go);
      row.appendChild(button("secondary", "Отмена", function () { box.removeChild(pick); open.setAttribute("aria-expanded", "false"); open.focus(); pickClosed(); }));
      pick.appendChild(row);
      box.appendChild(pick);
      select.focus();
    });
    open.setAttribute("aria-expanded", "false");
    actions.appendChild(open);
    if (linked) actions.appendChild(button("secondary", "Не предлагать новые", function () {
      linkFolder(linked.id, f.account_id, f.folder_id, false, this, done);
    }));
    box.appendChild(actions);
    return box;
  }

  function linkWith(f, value, propose, b, done) {
    var body = { account_id: f.account_id, folder_id: f.folder_id };
    if (value !== "new") body.sphere_id = Number(value);
    b.disabled = true;
    request("POST", "spheres/folders/make", body).then(function (got) {
      if (got.status === 401) { sessionLost(got.data); return; }
      if (got.status !== 200) { b.disabled = false; toast(got.data.error || "Не получилось."); return; }
      var sphere = got.data.sphere, already = (f.spheres || []).some(function (x) { return x.id === sphere.id && x.propose; });
      var said = (got.data.created ? "Сфера «" + sphere.title + "» заведена" : "Папка связана со сферой «" + sphere.title + "»") +
                 ": добавлено чатов — " + got.data.added + ".";
      var next = propose && !already
        ? request("POST", "spheres/" + sphere.id + "/folders", { account_id: f.account_id, folder_id: f.folder_id, link: true })
        : Promise.resolve(null);
      return next.then(function (x) {
        b.disabled = false;
        toast(x && x.status !== 200 ? said + " Предлагать новые не получилось." : said + (propose ? " Новые чаты папки будут предлагаться." : ""));
        sph.loaded = false;
        refreshSphereList();
        if (done) done();
      });
    }, function () { b.disabled = false; toast("Нет связи с сервером."); });
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
    drawFolders();
  }

  // Открыт выбор «Связать со сферой…» — папки не перерисовываем у владельца под рукой; перерисуем, когда закроет.
  var foldersLater = false;
  function drawFolders() {
    var folders = $("sph-folders");
    if (folders.querySelector(".folder-pick")) { foldersLater = true; return; }
    clear(folders);
    show("sph-folders-empty", !sph.folders.length);
    sph.folders.forEach(function (f) { folders.appendChild(folderItem(f)); });
  }
  function pickClosed() {
    if (foldersLater) { foldersLater = false; drawFolders(); }
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
    var meta = f.count + " " + plural(f.count, "чат", "чата", "чатов") + (f.account_role === "assistant" ? " · аккаунт помощника" : "");
    li.appendChild(el("div", "sph-meta", meta));
    (f.notes || []).forEach(function (n) { li.appendChild(el("div", "sect-note", n)); });
    li.appendChild(folderLinkTools(f, function () { loadSpheres(true); }));
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
  // «Настройках Штурмана» (сервер отвергает расширение из приложения, код page_only).
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

  /* ------------------------------------------------------------ «Остановить всё» (Р-87) */

  // Остановить — сразу, одним нажатием: это ужесточение. Продолжить — только нажатием в боте
  // согласований: отсюда уходит лишь просьба, и в боте появляется карточка подтверждения.
  function sinceWords(value) {
    var at = new Date(value);
    if (!value || isNaN(at.getTime())) return "";
    var words = deadline(value);                         // «сегодня в 14:30», «15 октября в 9:05»
    if (words.indexOf("сегодня в ") === 0) return "с " + words.slice(10);
    if (words.indexOf("завтра") === 0) return "";
    return (at.getDate() === 2 ? "со " : "с ") + words;
  }

  function drawHalt(h) {
    halt = h || { stopped: false };
    show("halt-stop", signedIn && !halt.stopped);
    show("halt-banner", signedIn && halt.stopped);
    if (!halt.stopped) return;
    $("halt-since").textContent = sinceWords(halt.since) +
      (halt.via === "bot" ? " (в боте)" : halt.via === "page" ? " (в «Настройках Штурмана»)" : "");
    $("halt-reading").textContent = halt.reading_paused
      ? "Чтение переписки тоже на паузе."
      : "Переписка по-прежнему собирается в архив.";
    show("halt-reading-box", Boolean(halt.reading_paused));
    show("halt-reading-stop", !halt.reading_paused);
    if (halt.resume_pending === undefined) {
      // кто ждёт подтверждения — только в GET halt; подробности не обязательны
      request("GET", "halt").then(function (got) {
        if (got.status === 200 && got.data.stopped) drawHalt(got.data);
      }, function () { /* не страшно */ });
      return;
    }
    if (halt.resume_pending) {
      haltNote("Подтверждение «Продолжить» ждёт в боте согласований — нажмите там «Да, сделать».");
      $("halt-resume").textContent = "Прислать подтверждение ещё раз";
    } else {
      haltNote("");
      $("halt-resume").textContent = "Продолжить…";
    }
  }

  function haltNote(text) {
    $("halt-note").textContent = text;
    show("halt-note", Boolean(text));
  }

  function haltCall(path, body, button, done) {
    if (busy) return;
    busy = true;
    button.disabled = true;
    request("POST", path, body).then(function (got) {
      busy = false;
      button.disabled = false;
      if (got.status === 401) { sessionLost(got.data); return; }
      if (got.status !== 200 && got.status !== 202) { toast(got.data.error || "Не получилось. Попробуйте ещё раз."); return; }
      drawHalt(got.data);
      if (got.data.note) haltNote(got.data.note);
      toast(done);
      load();
    }, function () {
      busy = false;
      button.disabled = false;
      toast("Нет связи с сервером. Ничего не изменилось — попробуйте ещё раз.");
    });
  }

  function wireHalt() {
    $("halt-stop").addEventListener("click", function () {
      haltCall("halt", { reading: false }, this, "Остановлено. Подробности — в боте согласований.");
    });
    $("halt-reading-stop").addEventListener("click", function () {
      haltCall("halt", { reading: true }, this, "Чтение переписки тоже остановлено.");
    });
    $("halt-resume").addEventListener("click", function () {
      haltCall("halt/resume", { reading: $("halt-reading-resume").checked }, this,
        "Карточка «Продолжить» — в боте согласований. Продолжится после вашего нажатия там.");
    });
  }

  /* ------------------------------------------------------------ «Открыть настройки в браузере» (П4) */

  // Ссылка открывается во внешнем браузере (Telegram: openLink). Адрес — только от сервера и только
  // https; ключа или входа в нём нет: в «Настройках Штурмана» вход по коду из бота или по ссылке.
  function openSetup() {
    if (!setupUrl) return;
    if (window.TelegramWebviewProxy || (window.external && "notify" in window.external) || inFrame) {
      postEvent("web_app_open_link", { url: setupUrl });
    } else {
      window.open(setupUrl, "_blank", "noopener");
    }
  }

  /* ------------------------------------------------------------ «Черновики» (Р-89, Р-105) */

  // Включить, добавить человека, поднять предел и сократить паузу отсюда можно сразу (владелец так решил),
  // но каждое изменение сервер пишет в журнал и сообщает в бот с командой, которой его вернуть.
  // limitsDirty — владелец правит числа: перерисовка вкладки их не затирает.
  var rh = { data: null, loaded: false, timer: null, limitsDirty: false };

  function rhPanel(name) {
    ["rh-loading", "rh-failed", "rh-unavailable", "rh-body"].forEach(function (id) { show(id, id === name); });
  }

  function loadRehearsal(quiet) {
    if (!quiet && !rh.loaded) rhPanel("rh-loading");
    return request("GET", "rehearsal").then(function (got) {
      if (got.status === 401) { sessionLost(got.data); return; }
      if (got.status !== 200) {
        $("rh-failed-text").textContent = got.data.error || "Сервер не ответил. Попробуйте ещё раз через минуту.";
        rhPanel("rh-failed");
        return;
      }
      rh.data = got.data;
      rh.loaded = true;
      drawRehearsal();
    }, function () {
      $("rh-failed-text").textContent = "Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.";
      rhPanel("rh-failed");
    });
  }

  function personName(p) { return p.name || (p.username ? "@" + p.username : "без имени"); }

  function drawRehearsal() {
    var d = rh.data;
    if (!d.available) {
      $("rh-unavailable-text").textContent = d.unavailable_text || "";
      rhPanel("rh-unavailable");
      $("rh-count").textContent = "";
      return;
    }
    rhPanel("rh-body");
    var s = d.settings;
    show("rh-halted", Boolean(halt.stopped));
    $("rh-enabled").checked = Boolean(s.enabled);
    if (!rh.limitsDirty) {
      $("rh-cap-input").value = String(s.daily_cap);
      $("rh-pause-input").value = String(s.pause_seconds);
    }
    $("rh-used").textContent = "Сегодня уже: " + ((d.stats || {}).asked_24h || 0) + " из " + s.daily_cap +
      " (считаются последние 24 часа).";
    var list = $("rh-people");
    clear(list);
    (d.people || []).forEach(function (p) {
      var li = el("li");
      li.appendChild(el("span", "who", personName(p)));
      li.appendChild(button("secondary", "Убрать", function () {
        savePeople(s.people.filter(function (id) { return id !== p.tg_id; }), this,
          "Убрано: " + personName(p) + ".");
      }));
      list.appendChild(li);
    });
    show("rh-people-empty", !(d.people || []).length);
    $("rh-count").textContent = s.enabled ? "сами — вкл" : "сами — выкл";
    var st = d.stats || {};
    $("rh-stats").textContent = "Карточек: " + (st.shown || 0) + ". Отправлено вами — " + (st.sent || 0) + ", «В чат» — " + (st.put || 0) +
      ", свой вариант — " + (st.voice || 0) + ", не годится — " + (st.bad || 0) + ", без оценки — " + (st.unrated || 0) +
      ". Всего «годится» — " + (st.good || 0) + ", «почти» — " + (st.almost || 0) + ".";
    var recent = $("rh-recent");
    clear(recent);
    (d.recent || []).forEach(function (r) {
      var li = el("li");
      li.appendChild(el("div", null, (r.who || "Собеседник") + (r.outcome_text ? " — ассистент " + r.outcome_text : "")));
      var meta = [deadline(r.at)];
      meta.push(r.rating_text || "без оценки");
      if (r.on_demand) meta.push("по вашей просьбе");
      li.appendChild(el("div", "meta", meta.join(" · ")));
      if (r.variant) {
        li.appendChild(el("div", "meta", "Как ответили бы вы:"));
        li.appendChild(el("div", "variant", r.variant));
      }
      recent.appendChild(li);
    });
    show("rh-recent-empty", !(d.recent || []).length);
    drawFound(rh.found || []);
  }

  function drawFound(items) {
    rh.found = items;
    var list = $("rh-found");
    clear(list);
    if (!rh.data) return;
    var have = rh.data.settings.people;
    items.filter(function (p) { return have.indexOf(p.tg_id) < 0; }).slice(0, 8).forEach(function (p) {
      var li = el("li");
      li.appendChild(el("span", "who", personName(p)));
      li.appendChild(button("primary", "Добавить", function () {
        if (have.length >= ((rh.data.limits || {}).people || 50)) { toast("Список уже полный."); return; }
        savePeople(have.concat([p.tg_id]), this, "Добавлено: " + personName(p) + ". Бот сообщит об изменении.");
      }));
      list.appendChild(li);
    });
  }

  function saveRehearsal(body, b, done) {
    if (busy) return;
    busy = true;
    if (b) b.disabled = true;
    request("PUT", "rehearsal", body).then(function (got) {
      busy = false;
      if (b) b.disabled = false;
      if (got.status === 401) { sessionLost(got.data); return; }
      if (got.status !== 200) { toast(got.data.error || "Не получилось. Попробуйте ещё раз."); loadRehearsal(true); return; }
      rh.data = got.data;
      drawRehearsal();
      toast(done);
    }, function () {
      busy = false;
      if (b) b.disabled = false;
      toast("Нет связи с сервером.");
      loadRehearsal(true);
    });
  }

  function savePeople(people, b, done) { saveRehearsal({ people: people }, b, done); }

  // Предел и пауза: целое число в пределах, которые назвал сервер; меняется только то, что изменили.
  function saveLimits(ev) {
    ev.preventDefault();
    if (!rh.data) return;
    var lim = rh.data.limits || {}, s = rh.data.settings, body = {}, bad = null;
    [["daily_cap", "rh-cap-input", "Карточек в сутки", lim.daily_cap || [1, 300], ""],
     ["pause_seconds", "rh-pause-input", "Пауза между карточками", lim.pause_seconds || [0, 3600], " секунд"]]
      .forEach(function (f) {
        var raw = $(f[1]).value.trim(), n = Number(raw);
        if (!/^\d+$/.test(raw) || n < f[3][0] || n > f[3][1]) {
          bad = bad || "«" + f[2] + "» — целое число от " + f[3][0] + " до " + f[3][1] + f[4] + ".";
        } else if (n !== s[f[0]]) {
          body[f[0]] = n;
        }
      });
    if (bad) { toast(bad); return; }
    if (!Object.keys(body).length) { rh.limitsDirty = false; drawRehearsal(); toast("Ничего не изменилось."); return; }
    rh.limitsDirty = false;
    saveRehearsal(body, $("rh-limits-save"), "Сохранено. Бот сообщит об изменении и как его вернуть.");
  }

  function wireRehearsal() {
    $("rh-retry").addEventListener("click", function () { loadRehearsal(false); });
    $("rh-enabled").addEventListener("change", function () {
      var on = this.checked;
      saveRehearsal({ enabled: on }, this, on ? "Черновики будут готовиться сами. Бот сообщит об изменении."
        : "Черновики сами больше не готовятся. «Подготовить ответ» под уведомлением работает.");
    });
    $("rh-limits").addEventListener("submit", saveLimits);
    ["rh-cap-input", "rh-pause-input"].forEach(function (id) {
      $(id).addEventListener("input", function () { rh.limitsDirty = true; });
    });
    $("rh-q").addEventListener("input", function () {
      var q = this.value.trim();
      clearTimeout(rh.timer);
      if (!q) { drawFound([]); return; }
      rh.timer = setTimeout(function () {
        request("GET", "rehearsal/people?q=" + encodeURIComponent(q)).then(function (got) {
          if (got.status === 200 && $("rh-q").value.trim() === q) drawFound(got.data.items || []);
        }, function () { /* попробует при следующей букве */ });
      }, 300);
    });
  }

  /* ------------------------------------------------------------ «Договорённости» (Р-94) */

  // Два списка — «Я обещал» и «Мне обещали», внутри — «Просрочено», «Сегодня», «На неделе», «Позже»,
  // «Без срока». Только принятые владельцем; неподтверждённые — в «Согласованиях», здесь их число.
  // «Выполнено», «Перенести», «Не так» — сразу (это записи самого владельца), с «Вернуть» после
  // «Выполнено» и «Не так». Каждое действие несёт начало отпечатка того, что владелец видел (v):
  // если договорённость с тех пор изменилась, сервер его не выполнит.
  var CM_DIRS = ["owner_owes", "owed_to_owner", "team"];
  var CM_DIR_TITLES = { owner_owes: "Я обещал", owed_to_owner: "Мне обещали", team: "В команде" };
  var CM_GROUPS = [["overdue", "Просрочено"], ["today", "Сегодня"], ["week", "На неделе"], ["later", "Позже"],
                   ["no_date", "Без срока"]];
  var CM_DIR_NAME = "shturman-app-cm-dir";
  var cm = { data: null, loaded: false, dir: "owner_owes" };
  try {
    var savedDir = sessionStorage.getItem(CM_DIR_NAME);
    if (CM_DIRS.indexOf(savedDir) >= 0) cm.dir = savedDir;
  } catch (e) { /* пусть */ }

  function cmPanel(name) {
    ["cm-loading", "cm-failed", "cm-body"].forEach(function (id) { show(id, id === name); });
  }

  function loadCommitments(quiet) {
    if (!quiet && !cm.loaded) cmPanel("cm-loading");
    return request("GET", "commitments").then(function (got) {
      if (got.status === 401) { sessionLost(got.data); return; }
      if (got.status !== 200) {
        $("cm-failed-text").textContent = got.data.error || "Сервер не ответил. Попробуйте ещё раз через минуту.";
        cmPanel("cm-failed");
        return;
      }
      cm.data = got.data;
      cm.loaded = true;
      cmPanel("cm-body");
      drawCommitments();
    }, function () {
      $("cm-failed-text").textContent = "Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.";
      cmPanel("cm-failed");
    });
  }

  function cmCount(dir) {
    if (dir === "team") return ((cm.data && cm.data.team) || []).length;
    var lists = (cm.data && cm.data.lists && cm.data.lists[dir]) || {};
    return CM_GROUPS.reduce(function (n, g) { return n + (lists[g[0]] || []).length; }, 0);
  }

  // На вкладке — сколько на сегодня и просрочено (оба списка).
  function drawCommitmentsBadge() {
    var c = (nowData && nowData.commitments) || null;
    var n = c ? (c.overdue || 0) + (c.today || 0) : 0;
    $("tab-commitments-count").textContent = n ? String(n) : "";
    show("tab-commitments-count", n > 0);
  }

  // «обещано 23.09» (Р-96): день сообщения, в котором дано обещание.
  function promisedText(item) {
    if (!item.promised) return "";
    var p = item.promised.split("-");
    return "обещано " + p[2] + "." + p[1];
  }

  function dueText(item) {
    if (!item.due_date && item.due_event) return "по событию: «" + item.due_event + "»";
    if (!item.due_date) return item.due_vague ? "назван неточно" : "не назван";
    var when = item.due_words || item.due_date;
    if (item.days < 0) {
      var late = -item.days;
      return when + " — просрочено на " + late + " " + plural(late, "день", "дня", "дней");
    }
    if (item.days === 0) return "сегодня" + (item.due_time ? " до " + item.due_time : "");
    if (item.days === 1) return "завтра, " + when;
    return when;
  }

  function cmItem(item) {
    var li = el("li", "cm");
    li.id = "cm-item-" + item.id;
    li.appendChild(el("div", "cm-what", item.what));
    var meta = el("div", "cm-meta");
    if (item.who) meta.appendChild(el("span", null, (item.direction === "owner_owes" ? "Кому: " : "Кто: ") + item.who));
    meta.appendChild(el("span", "cm-due" + (item.days !== null && item.days < 0 ? " late" : ""), "Срок: " + dueText(item)));
    if (item.promised) meta.appendChild(el("span", "cm-promised", promisedText(item)));
    li.appendChild(meta);
    var from = el("div", "cm-meta");
    var chat = item.chat || {};
    // личный чат с тем же человеком — без повтора имени
    var where = chat.title && chat.title === item.who ? (chat.kind || "чат")
      : (chat.title ? "«" + chat.title + "»" : "чат без названия") + (chat.kind ? ", " + chat.kind : "");
    from.appendChild(el("span", null, "Откуда: " + where));
    (item.spheres || []).forEach(function (name) { from.appendChild(el("span", "chip", name)); });
    li.appendChild(from);
    var actions = el("div", "actions");
    actions.appendChild(button("primary", "Выполнено", function () {
      cmAct(item, "done", {}, this, "Отмечено: выполнено.");
    }));
    actions.appendChild(button("secondary", "Перенести", function () { cmMove(item, li, this); }));
    actions.appendChild(button("secondary", "Не так", function () { cmWrong(item, li, this); }));
    li.appendChild(actions);
    return li;
  }

  function closeInline(li) {
    Array.prototype.forEach.call(li.querySelectorAll(".pick, .confirm"), function (n) { n.parentNode.removeChild(n); });
  }

  function cmMove(item, li, opener) {
    var had = li.querySelector(".pick");
    closeInline(li);
    if (had) return;
    var box = el("div", "pick cm-move");
    box.appendChild(el("p", null, "Перенести срок:"));
    var quick = el("div", "actions");
    quick.appendChild(button("secondary", "На завтра", function () { cmAct(item, "move", { to: "tomorrow" }, this); }));
    quick.appendChild(button("secondary", "Через неделю", function () { cmAct(item, "move", { to: "week" }, this); }));
    box.appendChild(quick);
    var row = el("div", "tools-row cm-date");
    var label = el("label", "field");
    var id = "cm-date-" + item.id;
    label.setAttribute("for", id);
    label.appendChild(el("span", null, "Или выберите дату"));
    var input = el("input");
    input.type = "date";
    input.id = id;
    if (cm.data && cm.data.today) input.min = cm.data.today;
    label.appendChild(input);
    row.appendChild(label);
    row.appendChild(button("primary", "Перенести", function () {
      if (!input.value) { toast("Выберите дату."); input.focus(); return; }
      cmAct(item, "move", { to: "date", date: input.value }, this);
    }));
    box.appendChild(row);
    var cancel = el("div", "actions");
    cancel.appendChild(button("secondary", "Отмена", function () { closeInline(li); opener.focus(); }));
    box.appendChild(cancel);
    li.appendChild(box);
  }

  function cmWrong(item, li, opener) {
    var had = li.querySelector(".confirm");
    closeInline(li);
    if (had) return;
    var box = el("div", "confirm");
    box.appendChild(el("p", null, "Записано неверно? Договорённость уйдёт из списка и из сводок, а это же обещание " +
      "из того же сообщения больше не будет предложено. Что не так — по желанию:"));
    var picked = { reason: null };
    var chips = el("div", "chips cm-reasons");
    ((cm.data && cm.data.reasons) || []).forEach(function (r) {
      var b = el("button", "chip-btn", r.label);
      b.type = "button";
      b.setAttribute("aria-pressed", "false");
      b.addEventListener("click", function () {
        var on = picked.reason !== r.code;
        picked.reason = on ? r.code : null;
        Array.prototype.forEach.call(chips.querySelectorAll("button"), function (x) { x.setAttribute("aria-pressed", "false"); });
        b.setAttribute("aria-pressed", on ? "true" : "false");
      });
      chips.appendChild(b);
    });
    box.appendChild(chips);
    var actions = el("div", "actions");
    actions.appendChild(button("danger", "Убрать", function () {
      cmAct(item, "wrong", picked.reason ? { reason: picked.reason } : {}, this, "Убрано: записано неверно.");
    }));
    actions.appendChild(button("secondary", "Отмена", function () { closeInline(li); opener.focus(); }));
    box.appendChild(actions);
    li.appendChild(box);
  }

  function cmAct(item, action, body, b, done) {
    if (busy) return;
    busy = true;
    if (b) b.disabled = true;
    body.v = item.v;
    request("POST", "commitments/" + item.id + "/" + action, body).then(function (got) {
      busy = false;
      if (b) b.disabled = false;
      if (got.status === 401) { sessionLost(got.data); return; }
      if (got.status !== 200) { toast(got.data.error || "Не получилось. Попробуйте ещё раз."); loadCommitments(true); return; }
      var text = done || got.data.answer || "Готово.";
      if (got.data.undo) toastAction(text, "Вернуть", function () { cmReopen(item); });
      else toast(text);
      loadCommitments(true);
      loadNow();
    }, function () {
      busy = false;
      if (b) b.disabled = false;
      toast("Нет связи с сервером. Ничего не изменилось — попробуйте ещё раз.");
    });
  }

  function cmReopen(item) {
    request("POST", "commitments/" + item.id + "/reopen", {}).then(function (got) {
      if (got.status === 401) { sessionLost(got.data); return; }
      toast(got.status === 200 ? "Вернул в список." : (got.data.error || "Не получилось вернуть."));
      loadCommitments(true);
      loadNow();
    }, function () { toast("Нет связи с сервером."); });
  }

  // Сообщение внизу с кнопкой действия («Вернуть»): держится дольше обычного.
  function toastAction(message, label, fn) {
    var node = $("toast");
    clear(node);
    node.appendChild(el("span", null, message + " "));
    var b = el("button", "toast-btn", label);
    b.type = "button";
    b.addEventListener("click", function () { node.hidden = true; fn(); });
    node.appendChild(b);
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { node.hidden = true; }, 8000);
  }

  // «В команде» (Р-96): обещания между другими людьми — только для сведения, одна кнопка «Скрыть».
  function cmTeamItem(item) {
    var li = el("li", "cm cm-team");
    li.id = "cm-item-" + item.id;
    li.appendChild(el("div", "cm-what", item.what));
    var meta = el("div", "cm-meta");
    var who = (item.who || "кто-то") + (item.to ? " → " + item.to : "");
    meta.appendChild(el("span", null, who));
    meta.appendChild(el("span", "cm-due", "Срок: " + dueText(item)));
    if (item.promised) meta.appendChild(el("span", "cm-promised", promisedText(item)));
    li.appendChild(meta);
    var from = el("div", "cm-meta");
    var chat = item.chat || {};
    from.appendChild(el("span", null, "Откуда: " + (chat.title ? "«" + chat.title + "»" : "чат без названия") +
      (chat.kind ? ", " + chat.kind : "")));
    (item.spheres || []).forEach(function (name) { from.appendChild(el("span", "chip", name)); });
    li.appendChild(from);
    var actions = el("div", "actions");
    actions.appendChild(button("secondary", "Скрыть", function () { cmAct(item, "hide", {}, this, "Скрыто из «В команде»."); }));
    li.appendChild(actions);
    return li;
  }

  function drawTeam(box) {
    var items = (cm.data && cm.data.team) || [];
    if (!items.length) return false;
    box.appendChild(el("p", "muted small cm-team-note", "Обещания между другими людьми из ваших чатов — только для " +
      "сведения: подтверждать их не нужно, в сводку они не идут. Через две недели уходят сами."));
    var ul = el("ul", "cm-list");
    items.forEach(function (item) { ul.appendChild(cmTeamItem(item)); });
    box.appendChild(ul);
    return true;
  }

  function drawCommitments() {
    var d = cm.data;
    CM_DIRS.forEach(function (dir) {
      var n = cmCount(dir);
      $("cm-dir-" + dir + "-label").textContent = CM_DIR_TITLES[dir] + (n ? " · " + n : "");
      $("cm-dir-" + dir).setAttribute("aria-selected", dir === cm.dir ? "true" : "false");
    });
    var total = cmCount("owner_owes") + cmCount("owed_to_owner");
    $("cm-promised-note").hidden = cm.dir === "team";
    $("cm-count").textContent = total ? String(total) : "";
    if (d.waiting) {
      $("cm-waiting-text").textContent = "Ещё " + d.waiting + " " + plural(d.waiting, "ждёт", "ждут", "ждут") +
        " подтверждения «Верно / Не так» — в «Согласованиях».";
    }
    show("cm-waiting", Boolean(d.waiting) && cm.dir !== "team");
    var box = $("cm-groups");
    clear(box);
    var lists = d.lists[cm.dir] || {};
    var any = cm.dir === "team" ? drawTeam(box) : false;
    CM_GROUPS.forEach(function (g) {
      var items = lists[g[0]] || [];
      if (!items.length) return;
      any = true;
      var sect = el("details", "sect cm-group" + (g[0] === "overdue" ? " late" : ""));
      sect.id = "cm-" + g[0];
      var key = "c:" + cm.dir + ":" + g[0];
      sect.open = isOpen(key, true);
      var head = el("summary");
      head.appendChild(el("span", "sect-title", g[1]));
      head.appendChild(el("span", "sect-count on", String(items.length)));
      sect.appendChild(head);
      var body = el("div", "sect-body");
      var ul = el("ul", "cm-list");
      items.forEach(function (item) { ul.appendChild(cmItem(item)); });
      body.appendChild(ul);
      sect.appendChild(body);
      head.addEventListener("click", function () { rememberOpen(key, !sect.open); });
      box.appendChild(sect);
    });
    $("cm-empty").textContent = cm.dir === "team"
      ? "Обещаний между другими людьми пока нет. Они появляются после разбора переписки — без вашего подтверждения."
      : cm.dir === "owner_owes"
      ? "Ваших обещаний пока нет. Они появляются после разбора переписки и вашего «Верно» в «Согласованиях»."
      : "Обещаний вам пока нет. Они появляются после разбора переписки и вашего «Верно» в «Согласованиях».";
    show("cm-empty", !any);
    show("cm-truncated", Boolean(d.truncated));
  }

  function wireCommitments() {
    $("cm-retry").addEventListener("click", function () { loadCommitments(false); });
    CM_DIRS.forEach(function (dir) {
      $("cm-dir-" + dir).addEventListener("click", function () {
        cm.dir = dir;
        try { sessionStorage.setItem(CM_DIR_NAME, dir); } catch (e) { /* пусть */ }
        if (cm.data) drawCommitments();
      });
    });
    $("cm-waiting-open").addEventListener("click", function () {
      openTab("approvals");
      reveal($("grp-commitments"));
    });
  }

  /* ------------------------------------------------------------ «История» (Р-95, С15, Х6) */

  // «Решения», «Ассистент», «Входы» — по дням, словами, без текста переписки. «Отозвать» и сужение — сразу;
  // «Вернуть как было», если возврат расширяет, и любое расширение доверия — только карточкой в бот
  // (сервер отвечает 202, «Да» под карточкой отсюда не нажать).
  var hs = { seg: "decisions", data: null, trust: null, timer: null };

  function hsPanel(name) {
    ["hs-loading", "hs-failed", "hs-body"].forEach(function (id) { show(id, id === name); });
  }

  function hsDay(iso) {
    var d = new Date(iso + "T12:00:00"), today = new Date(), y = new Date();
    y.setDate(today.getDate() - 1);
    if (d.toDateString() === today.toDateString()) return "Сегодня";
    if (d.toDateString() === y.toDateString()) return "Вчера";
    var months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
                  "октября", "ноября", "декабря"];
    return d.getDate() + " " + months[d.getMonth()];
  }

  function hsTime(iso) {
    var at = new Date(iso);
    return at.getHours() + ":" + ((at.getMinutes() < 10 ? "0" : "") + at.getMinutes());
  }

  function hsFailed(got) {
    if (got && got.status === 401) { sessionLost(got.data); return; }
    $("hs-failed-text").textContent = (got && got.data && got.data.error) || "Нет связи с сервером. Попробуйте ещё раз.";
    hsPanel("hs-failed");
  }

  function loadHistory(quiet) {
    Array.prototype.forEach.call(document.querySelectorAll("#hs-seg button"), function (b) {
      b.setAttribute("aria-selected", b.getAttribute("data-hs") === hs.seg ? "true" : "false");
    });
    if (!quiet) hsPanel("hs-loading");
    var path = hs.seg === "trust" ? "replies" : "history?tab=" + hs.seg + "&days=14";
    return request("GET", path).then(function (got) {
      if (got.status !== 200) { hsFailed(got); return; }
      if (hs.seg === "trust") hs.trust = got.data; else hs.data = got.data;
      hsPanel("hs-body");
      drawHistory();
    }, function () { hsFailed(null); });
  }

  function hsSend(method, path, body, b, done) {
    if (busy) return Promise.resolve();
    busy = true;
    if (b) b.disabled = true;
    return request(method, path, body).then(function (got) {
      busy = false;
      if (b) b.disabled = false;
      if (got.status === 401) { sessionLost(got.data); return; }
      if (got.status !== 200 && got.status !== 202) { toast(got.data.error || "Не получилось. Попробуйте ещё раз."); loadHistory(true); return; }
      toast(got.status === 202 ? "Подтвердите в боте согласований — карточка уже там." : (got.data.text && got.data.text !== "Готово." ? got.data.text : done || "Готово."));
      loadHistory(true);
    }, function () {
      busy = false;
      if (b) b.disabled = false;
      toast("Нет связи с сервером.");
    });
  }

  function hsButton(label, cls, onClick) {
    var b = el("button", cls || "secondary small-btn", label);
    b.type = "button";
    b.addEventListener("click", function () { onClick(b); });
    return b;
  }

  function hsItem(item) {
    var li = el("li", "hs-item");
    li.appendChild(el("time", "hs-time", hsTime(item.at)));
    var body = el("div", "hs-body");
    body.appendChild(el("span", "hs-title", item.title));
    var meta = [item.group, item.who].filter(Boolean).join(" · ");
    if (meta) body.appendChild(el("span", "hs-meta", meta));
    if (item.status) body.appendChild(el("span", "hs-status", item.status));
    if (item.note) body.appendChild(el("span", "hs-meta", item.note));
    (item.actions || []).forEach(function (a) {
      var id = a.ref.split(":")[1];
      body.appendChild(hsButton(a.label, "secondary small-btn", function (b) {
        hsSend("POST", "history/" + id + "/revert", {}, b, "Вернул как было.");
      }));
    });
    li.appendChild(body);
    return li;
  }

  function drawHistory() {
    var box = $("hs-body");
    clear(box);
    $("hs-count").textContent = "";
    if (hs.seg === "trust") { drawTrust(box); return; }
    var d = hs.data;
    if (!d) return;
    if (d.tab === "decisions") {
      var grants = el("div", "panel");
      grants.appendChild(el("h2", "", "Действует сейчас"));
      if (!d.grants.length) grants.appendChild(el("p", "muted small", "Разрешений на источники и доступов внешних программ нет."));
      d.grants.forEach(function (g) {
        var row = el("div", "hs-grant");
        row.appendChild(el("span", "", g.title));
        row.appendChild(el("span", "muted small", g.detail + " · до " + g.until_words));
        var parts = g.ref.split(":");
        row.appendChild(hsButton("Отозвать", "secondary small-btn", function (b) {
          hsSend("POST", "history/grants/" + parts[0] + "/" + parts[1] + "/revoke", {}, b);
        }));
        grants.appendChild(row);
      });
      box.appendChild(grants);
      $("hs-count").textContent = d.grants.length ? String(d.grants.length) : "";
    }
    if (!d.days_list.length) {
      box.appendChild(el("p", "muted", d.tab === "assistant" ? "За две недели ассистент ничего не делал сам." :
                                       d.tab === "logins" ? "Входов за две недели не было." : "За две недели ничего не решалось и не менялось."));
    }
    d.days_list.forEach(function (day) {
      box.appendChild(el("h2", "hs-day", hsDay(day.date)));
      var list = el("ol", "hs-list");
      day.items.forEach(function (item) { list.appendChild(hsItem(item)); });
      box.appendChild(list);
    });
  }

  function drawTrust(box) {
    var d = hs.trust;
    if (!d) return;
    var people = el("div", "panel");
    people.appendChild(el("h2", "", "Доверенные"));
    people.appendChild(el("p", "muted small", "Им ассистент отвечает сам, если включён автоответ. Убрать — сразу; добавить — подтверждением в боте."));
    if (!d.trusted.length) people.appendChild(el("p", "muted small", "Пока никого."));
    d.trusted.forEach(function (p) {
      var who = p.name || (p.username ? "@" + p.username : "без имени в архиве");
      var row = el("div", "hs-grant");
      row.appendChild(el("span", "", who));
      row.appendChild(hsButton("Убрать", "secondary small-btn", function (b) { hsSend("DELETE", "replies/trusted/" + p.tg_user_id, {}, b, who + " — больше не в доверенных."); }));
      people.appendChild(row);
    });
    var q = el("input", "");
    q.type = "search";
    q.placeholder = "Найти человека, чтобы сделать доверенным";
    q.setAttribute("aria-label", "Найти человека по имени");
    q.autocomplete = "off";
    var found = el("div", "hs-found");
    q.addEventListener("input", function () {
      var text = q.value.trim();
      clearTimeout(hs.timer);
      clear(found);
      if (!text) return;
      hs.timer = setTimeout(function () {
        request("GET", "replies/people?q=" + encodeURIComponent(text)).then(function (got) {
          clear(found);
          if (got.status !== 200 || q.value.trim() !== text) return;
          (got.data.items || []).slice(0, 8).forEach(function (p) {
            var row = el("div", "hs-grant");
            row.appendChild(el("span", "", personName(p)));
            if (p.trusted) row.appendChild(el("span", "muted small", "доверенный"));
            else row.appendChild(hsButton("Сделать доверенным…", "secondary small-btn", function (b) { hsSend("POST", "replies/trusted", { tg_user_id: p.tg_id }, b); }));
            found.appendChild(row);
          });
        }, function () { /* при следующей букве */ });
      }, 300);
    });
    people.appendChild(q);
    people.appendChild(found);
    box.appendChild(people);

    var auto = el("div", "panel");
    auto.appendChild(el("h2", "", "Автоответ доверенным"));
    d.autoreply.accounts.forEach(function (a) {
      var name = a.label || (a.role === "assistant" ? "Помощник" : "Ваш аккаунт");
      var row = el("div", "hs-grant");
      row.appendChild(el("span", "", "«" + name + "»: " + (a.enabled ? "включён" : "выключен")));
      if (a.enabled) row.appendChild(hsButton("Выключить", "secondary small-btn", function (b) { hsSend("PUT", "replies/autoreply/" + a.account_id, { enabled: false }, b, "Автоответ выключен."); }));
      else if (d.autoreply.available) row.appendChild(hsButton("Включить…", "secondary small-btn", function (b) { hsSend("PUT", "replies/autoreply/" + a.account_id, { enabled: true }, b); }));
      auto.appendChild(row);
    });
    if (!d.autoreply.accounts.length) auto.appendChild(el("p", "muted small", "Аккаунтов пока нет."));
    box.appendChild(auto);

    var groups = el("div", "panel");
    groups.appendChild(el("h2", "", "Группы и обращения"));
    if (!d.groups.length) groups.appendChild(el("p", "muted small", "Ассистент не отвечает ни в одной группе."));
    d.groups.forEach(function (g) {
      var title = (g.title || "группа без названия") + (g.topic_tg_id ? ", тема № " + g.topic_tg_id : "");
      var row = el("div", "hs-grant");
      row.appendChild(el("span", "", title));
      row.appendChild(hsButton("Запретить", "secondary small-btn", function (b) {
        hsSend("DELETE", "replies/groups/" + g.chat_id + (g.topic_tg_id ? "?topic=" + g.topic_tg_id : ""), {}, b, "В «" + title + "» ассистент больше не отвечает.");
      }));
      groups.appendChild(row);
    });
    groups.appendChild(el("p", "muted small", "Обращения: " + (d.addressing.length ? d.addressing.map(function (a) { return "«" + a + "»"; }).join(", ") : "нет") + "."));
    d.addressing.forEach(function (a, i) {
      groups.appendChild(hsButton("Убрать «" + a + "»", "secondary small-btn", function (b) {
        var rest = d.addressing.slice();
        rest.splice(i, 1);
        hsSend("PUT", "replies/addressing", { aliases: rest }, b, "Обращение убрано.");
      }));
    });
    groups.appendChild(el("p", "muted small", "Разрешить группу, задать черновики по чатам и новые обращения — в «Настройках Штурмана» или кнопками в боте: /groups, /drafting, /addressing."));
    box.appendChild(groups);
  }

  function wireHistory() {
    Array.prototype.forEach.call(document.querySelectorAll("#hs-seg button"), function (b) {
      b.addEventListener("click", function () { hs.seg = b.getAttribute("data-hs"); loadHistory(false); });
    });
    $("hs-retry").addEventListener("click", function () { loadHistory(false); });
  }

  /* ------------------------------------------------------------ сводки во вкладке 🔔 (Р-95, Х3) */

  // Видно, только когда ассистент в Hermes хоть раз ответил (в режиме без Hermes строки нет). Сразу:
  // сводка приходит вам же; бот сообщит об изменении.
  var sm = { data: null };

  function loadSummaries() {
    return request("GET", "summaries").then(function (got) {
      if (got.status !== 200) { show("sm-form", false); return; }
      sm.data = got.data;
      drawSummaries();
    }, function () { show("sm-form", false); });
  }

  function smNext(key) {
    var h = sm.data.hermes[key] || {};
    if (!h.exists) return "Задачи пока нет — появится после «Сохранить».";
    if (!h.enabled) return "Сейчас выключена.";
    return h.next_local ? "Следующая — " + h.next_local + "." : "";
  }

  function drawSummaries() {
    var d = sm.data;
    show("sm-form", !!(d && d.available));
    if (!d || !d.available) return;
    var m = d.choice["morning-brief"], w = d.choice["weekly-review"];
    $("sm-morning-on").checked = !!m.enabled;
    $("sm-morning-time").value = m.time;
    $("sm-weekly-on").checked = !!w.enabled;
    $("sm-weekly-day").value = String(w.day);
    $("sm-weekly-time").value = w.time;
    $("sm-morning-next").textContent = smNext("morning-brief");
    $("sm-weekly-next").textContent = smNext("weekly-review");
    var s = d.sync.state;
    var msg = s === "pending" ? "Ассистент применяет новое время." : s === "drift" ? "У ассистента сейчас другое время — «Сохранить» вернёт выбранное."
      : s === "error" ? (d.sync.error_text || "Ассистент не смог применить.") : s === "no_report" ? "Ассистент давно не отвечал." : "";
    $("sm-sync").textContent = msg;
    show("sm-sync", !!msg);
  }

  function wireSummaries() {
    $("sm-form").addEventListener("submit", function (event) {
      event.preventDefault();
      if (busy) return;
      var drift = sm.data && sm.data.sync.state === "drift";
      var body = {
        "morning-brief": { enabled: $("sm-morning-on").checked, time: $("sm-morning-time").value },
        "weekly-review": { enabled: $("sm-weekly-on").checked, time: $("sm-weekly-time").value, day: Number($("sm-weekly-day").value) }
      };
      if (drift) body.again = true;
      busy = true;
      $("sm-save").disabled = true;
      request("PUT", "summaries", body).then(function (got) {
        busy = false;
        $("sm-save").disabled = false;
        if (got.status === 401) { sessionLost(got.data); return; }
        if (got.status !== 200) { toast(got.data.error || "Не получилось. Попробуйте ещё раз."); return; }
        sm.data = got.data;
        drawSummaries();
        toast("Сохранено — ассистент применит в течение минуты.");
        setTimeout(loadSummaries, 8000);
      }, function () {
        busy = false;
        $("sm-save").disabled = false;
        toast("Нет связи с сервером.");
      });
    });
  }

  /* ------------------------------------------------------------ «Память» (шаг 6 записки) */

  var mm = { loaded: false, list: null, subject: null, kind: null, id: null, timer: null, editing: null };

  function ddmm(iso) {
    if (!iso) return "";
    var d = String(iso).slice(0, 10).split("-");
    return d.length === 3 ? d[2] + "." + d[1] + "." + d[0] : String(iso);
  }

  function mmFailed(text) {
    $("mm-failed-text").textContent = text;
    show("mm-loading", false);
    show("mm-failed", true);
  }

  function loadMemory(quiet) {
    if (mm.kind) return openSubject(mm.kind, mm.id, true);
    if (!quiet && !mm.loaded) { show("mm-loading", true); show("mm-failed", false); }
    var q = $("mm-q").value.trim();
    return request("GET", "memory" + (q ? "?q=" + encodeURIComponent(q) : "")).then(function (got) {
      if (got.status === 401) { sessionLost(got.data); return; }
      if (got.status !== 200) { mmFailed(got.data.error || "Сервер не ответил. Попробуйте ещё раз через минуту."); return; }
      mm.loaded = true;
      mm.list = got.data;
      show("mm-loading", false);
      show("mm-failed", false);
      drawMemoryList();
    }, function () { mmFailed("Нет связи с сервером. Проверьте интернет и попробуйте ещё раз."); });
  }

  function mmEntry(item, label) {
    var li = el("li", "cm");
    var b = el("button", "link-btn", item.title);
    b.type = "button";
    b.addEventListener("click", function () { openSubject(item.kind, item.id, false); });
    li.appendChild(b);
    var meta = el("div", "cm-meta");
    meta.appendChild(el("span", null, label));
    if (item.waiting) meta.appendChild(el("span", "cm-due late", "ждут решения: " + item.waiting));
    li.appendChild(meta);
    return li;
  }

  function drawMemoryList() {
    var list = $("mm-list"), d = mm.list || { people: [], projects: [] };
    list.textContent = "";
    if (!$("mm-q").value.trim()) list.appendChild(mmEntry(d.owner, "о вас: профиль"));
    var KIND_NOTE = { organization: "организация", unit: "подразделение" };
    d.projects.forEach(function (p) { list.appendChild(mmEntry(p, KIND_NOTE[p.project_kind] || "проект")); });
    d.people.forEach(function (p) { list.appendChild(mmEntry(p, "человек")); });
    show("mm-empty", !list.childNodes.length);
  }

  function mmMeta(item, history) {
    var meta = el("div", "cm-meta");
    if (item.slot) meta.appendChild(el("span", null, item.slot));
    // положение дел — «на дату», событие — просто дата, остальное — «с даты» (Р-111)
    var when = item.nature === "observation" ? "на " : item.nature === "event" ? "" : "с ";
    meta.appendChild(el("span", null, when + ddmm(item.since) + (item.until ? " по " + ddmm(item.until) : "")));
    if (item.basis) meta.appendChild(el("span", null, item.basis));
    if (item.correction) meta.appendChild(el("span", null, "исправлено вами"));
    if (item.chat) meta.appendChild(el("span", null, "чат: " + item.chat));
    if (history) meta.appendChild(el("span", "mm-state", "узнал " + ddmm(item.learned_at) +
      (item.decided_at ? ", решено " + ddmm(item.decided_at) : "")));
    return meta;
  }

  function mmAct(item, action, body) {
    return request("POST", "memory/facts/" + item.id + "/" + action, body ? { v: item.v, text: body.text } : { v: item.v }).then(function (got) {
      if (got.status === 401) { sessionLost(got.data); return; }
      toast(got.data.answer || got.data.error || "Не получилось.");
      mm.editing = null;
      openSubject(mm.kind, mm.id, true);
    }, function () { toast("Нет связи с сервером."); });
  }

  function mmCurrent(item, editable) {
    var li = el("li", "cm");
    li.appendChild(el("div", "cm-what", item.text));
    li.appendChild(mmMeta(item, false));
    if (item.quote) li.appendChild(el("div", "mm-quote", "«" + item.quote + "» — " + ddmm(item.said_at)));
    if (!editable) return li;
    if (mm.editing === item.id) {
      var box = el("div", "mm-edit");
      var area = document.createElement("textarea");
      area.maxLength = 240;
      area.value = item.text;
      area.setAttribute("aria-label", "Как правильно");
      box.appendChild(area);
      var row = el("div", "actions");
      var save = el("button", "primary", "Сохранить");
      save.type = "button";
      save.addEventListener("click", function () { save.disabled = true; mmAct(item, "correct", { text: area.value }); });
      var cancel = el("button", "secondary", "Отмена");
      cancel.type = "button";
      cancel.addEventListener("click", function () { mm.editing = null; drawSubject(); });
      row.appendChild(save);
      row.appendChild(cancel);
      box.appendChild(row);
      li.appendChild(box);
      return li;
    }
    var actions = el("div", "actions");
    var fix = el("button", "secondary", "Поправить");
    fix.type = "button";
    fix.addEventListener("click", function () { mm.editing = item.id; drawSubject(); });
    var wrong = el("button", "secondary", "Не так");
    wrong.type = "button";
    wrong.addEventListener("click", function () { wrong.disabled = true; mmAct(item, "wrong"); });
    actions.appendChild(fix);
    actions.appendChild(wrong);
    li.appendChild(actions);
    return li;
  }

  function drawSubject() {
    var d = mm.subject;
    if (!d) return;
    $("mm-subject-title").textContent = d.title;
    var cur = $("mm-current"), hist = $("mm-history");
    cur.textContent = "";
    hist.textContent = "";
    d.current.forEach(function (item) { cur.appendChild(mmCurrent(item, d.editable)); });
    var empty = d.as_of ? "В тот день ничего не действовало — или ассистент этого не видел." :
      d.known_at ? "К тому дню ассистент ничего об этом не знал." : "Пока ничего не записано.";
    $("mm-current-empty").textContent = empty;
    show("mm-current-empty", !d.current.length);
    var note = d.as_of ? "Действовало " + ddmm(d.as_of) + " — по дате сообщения, в котором это сказано." :
      d.known_at ? "Что ассистент знал к концу " + ddmm(d.known_at) + ": записанное позже сюда не попадает." : "";
    $("mm-when-note").textContent = note;
    show("mm-when-note", !!note);
    var pending = d.pending.length;
    $("mm-pending-text").textContent = pending ? (d.known_at ? "Ждало вашего решения: " : "Ждут вашего решения: ") + pending : "";
    show("mm-pending", pending > 0 && !d.known_at);
    d.history.forEach(function (item) {
      var li = el("li", "cm");
      li.appendChild(el("div", "cm-what", item.text));
      var st = el("div", "mm-state", item.state_text);
      li.appendChild(st);
      li.appendChild(mmMeta(item, true));
      hist.appendChild(li);
    });
  }

  function openSubject(kind, id, quiet) {
    mm.kind = kind;
    mm.id = id;
    show("mm-list-box", false);
    show("mm-subject", true);
    var mode = $("mm-mode").value, day = $("mm-date").value;
    var query = mode !== "now" && day ? "?" + mode + "=" + encodeURIComponent(day) : "";
    return request("GET", "memory/" + kind + "/" + id + query).then(function (got) {
      if (got.status === 401) { sessionLost(got.data); return; }
      if (got.status !== 200) { toast(got.data.error || "Не загрузилось."); if (!quiet) backToList(); return; }
      mm.subject = got.data;
      drawSubject();
    }, function () { toast("Нет связи с сервером."); });
  }

  function backToList() {
    mm.kind = mm.id = mm.subject = mm.editing = null;
    $("mm-mode").value = "now";
    $("mm-date").value = "";
    show("mm-date", false);
    show("mm-subject", false);
    show("mm-list-box", true);
    loadMemory(true);
  }

  function wireMemory() {
    $("mm-retry").addEventListener("click", function () { loadMemory(false); });
    $("mm-back").addEventListener("click", backToList);
    $("mm-q").addEventListener("input", function () { clearTimeout(mm.timer); mm.timer = setTimeout(function () { loadMemory(true); }, 300); });
    $("mm-mode").addEventListener("change", function () {
      var dated = $("mm-mode").value !== "now";
      show("mm-date", dated);
      if (dated && !$("mm-date").value) $("mm-date").value = new Date().toISOString().slice(0, 10);
      if (mm.kind) openSubject(mm.kind, mm.id, true);
    });
    $("mm-date").addEventListener("change", function () { if (mm.kind) openSubject(mm.kind, mm.id, true); });
    $("mm-pending-open").addEventListener("click", function () { openTab("approvals"); });
  }

  /* ------------------------------------------------------------ запуск */

  $("refresh").addEventListener("click", function () { load(); });
  $("tab-approvals").addEventListener("click", function () { openTab("approvals"); });
  $("tab-commitments").addEventListener("click", function () { openTab("commitments"); });
  $("tab-chats").addEventListener("click", function () { openTab("chats"); });
  $("tab-spheres").addEventListener("click", function () { openTab("spheres"); });
  $("tab-rehearsal").addEventListener("click", function () { openTab("rehearsal"); });
  $("tab-notify").addEventListener("click", function () { openTab("notify"); });
  $("tab-history").addEventListener("click", function () { openTab("history"); });
  $("tab-memory").addEventListener("click", function () { openTab("memory"); });
  wireChats();
  wireMemory();
  wireSpheres();
  wireNotify();
  wireHalt();
  wireRehearsal();
  wireNow();
  wireCommitments();
  wireHistory();
  wireSummaries();
  Array.prototype.forEach.call(document.querySelectorAll(".open-setup"), function (b) {
    b.addEventListener("click", openSetup);
  });
  try {
    var saved = sessionStorage.getItem(TAB_NAME);
    tab = TABS.indexOf(saved) >= 0 ? saved : "approvals";
  } catch (e) { tab = "approvals"; }
  // Кнопка «Открыть «Штурман»» под /today в боте открывает сразу «Договорённости» (?open=commitments).
  if (TABS.indexOf(openParam) >= 0) tab = openParam;
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
    if (tab === "rehearsal") loadRehearsal(true);
    if (tab === "notify") loadNotify(true);
    if (tab === "commitments") loadCommitments(true);
    if (tab === "notify") { loadNotify(true); loadSummaries(); }
    if (tab === "history") loadHistory(true);
    if (tab === "memory") loadMemory(true);
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
