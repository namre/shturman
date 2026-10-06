/* Страницы входа «Штурмана». Работают поверх штатного входа Hermes:
 *   ../auth/login?provider=shturman      — начать вход (Hermes вернёт сюда со state);
 *   ../auth/callback?code=…&state=…      — завершить вход.
 * Страница ничего не знает о секретах: код проверяет сервер.
 */
(function () {
  "use strict";

  var ACTIVATION_KEY = "shturman.activation";
  var WIZARD_PATH = "/shturman";

  function $(id) { return document.getElementById(id); }
  function show(id) { var el = $(id); if (el) el.hidden = false; }
  function hide(id) { var el = $(id); if (el) el.hidden = true; }

  function takeActivation() {
    try {
      var value = sessionStorage.getItem(ACTIVATION_KEY);
      sessionStorage.removeItem(ACTIVATION_KEY);
      return value || "";
    } catch (e) { return ""; }
  }

  function loginUrl(next) {
    var url = "../auth/login?provider=shturman";
    return next ? url + "&next=" + encodeURIComponent(next) : url;
  }

  /* Завершает вход. Возвращает {ok, url} либо {ok:false, reason}. */
  function complete(code, state) {
    var url = "../auth/callback?code=" + encodeURIComponent(code) + "&state=" + encodeURIComponent(state);
    return fetch(url, { credentials: "same-origin", redirect: "follow", cache: "no-store" })
      .then(function (res) {
        if (res.ok) return { ok: true, url: res.url };
        return res.json().catch(function () { return {}; }).then(function (body) {
          var detail = String((body && body.detail) || "");
          var reason = detail.indexOf("Invalid code:") === 0
            ? detail.slice("Invalid code:".length).trim()
            : (detail.indexOf("PKCE") !== -1 || detail.indexOf("state") !== -1 ? "stale" : "error");
          return { ok: false, reason: reason };
        });
      })
      .catch(function () { return { ok: false, reason: "network" }; });
  }

  var REASONS = {
    wrong: "Код не подошёл. Проверьте цифры и попробуйте ещё раз.",
    expired: "Срок действия кода вышел. Запросите новый.",
    none: "Этот код уже использован или заменён новым. Запросите код ещё раз.",
    locked: "Слишком много неверных попыток. Вход временно закрыт, попробуйте позже.",
    stale: "Страница входа устарела. Запросите код ещё раз.",
    network: "Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.",
    error: "Не получилось войти. Попробуйте ещё раз.",
    activation_invalid: "Ссылка активации уже использована или устарела. Попросите ИИ-агента на сервере выдать новую."
  };

  function message(text, withRetry) {
    hide("view-activating"); hide("view-code");
    $("message-text").textContent = text;
    $("message-retry").hidden = !withRetry;
    show("view-message");
  }

  function minutes(seconds) {
    var m = Math.max(1, Math.ceil(Number(seconds || 0) / 60));
    return m + " мин.";
  }

  function loginPage() {
    var params = new URLSearchParams(location.search);
    var state = params.get("state") || "";
    var mode = params.get("m") || "";
    var activation = takeActivation();

    if (!state) { location.replace(loginUrl("")); return; }

    if (activation) {
      show("view-activating");
      complete("a." + activation, state).then(function (r) {
        if (r.ok) { location.replace(r.url || WIZARD_PATH); return; }
        message(REASONS[r.reason] || REASONS.error, false);
      });
      return;
    }

    if (mode === "no_owner") {
      message("Бот ещё не привязан к владельцу, поэтому код прислать некому. Войти можно только по ссылке активации.", false);
      return;
    }
    if (mode === "locked") {
      message("Слишком много неверных попыток. Вход закрыт ещё на " + minutes(params.get("wait")) +
              " Если это были не вы, ничего делать не нужно.", false);
      return;
    }
    if (mode === "send_failed") {
      message("Не удалось отправить код: бот сейчас не может написать вам в Telegram. Попробуйте через минуту.", true);
      return;
    }

    if (mode === "reused") {
      $("code-lead").textContent = "Код уже отправлен в Telegram меньше минуты назад — используйте его. Он действует 5 минут.";
    }
    show("view-code");
    var input = $("code");
    var error = $("code-error");
    var button = $("code-submit");
    input.focus();
    input.addEventListener("input", function () {
      var digits = input.value.replace(/\D/g, "").slice(0, 8);
      input.value = digits.length > 4 ? digits.slice(0, 4) + " " + digits.slice(4) : digits;
      error.hidden = true;
    });
    $("code-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var digits = input.value.replace(/\D/g, "");
      if (digits.length !== 8) {
        error.textContent = "В коде восемь цифр.";
        error.hidden = false;
        return;
      }
      button.disabled = true;
      complete(digits, state).then(function (r) {
        if (r.ok) { location.replace(r.url || "../"); return; }
        button.disabled = false;
        if (r.reason === "locked" || r.reason === "stale" || r.reason === "none" || r.reason === "expired") {
          message(REASONS[r.reason], r.reason !== "locked");
          return;
        }
        error.textContent = REASONS[r.reason] || REASONS.error;
        error.hidden = false;
        input.select();
      });
    });
  }

  function activatePage() {
    var value = (location.hash || "").replace(/^#/, "");
    if (!/^[A-Za-z0-9_-]{20,}$/.test(value)) { show("view-bad"); return; }
    show("view-ok");
    $("go").addEventListener("click", function () {
      try { sessionStorage.setItem(ACTIVATION_KEY, value); } catch (e) { /* без хранилища вход не получится */ }
      // Убираем значение из адресной строки и истории до ухода со страницы.
      try { history.replaceState(null, "", location.pathname); } catch (e) { /* не критично */ }
      location.assign(loginUrl(WIZARD_PATH));
    });
  }

  var page = document.body.getAttribute("data-page");
  if (page === "login") loginPage();
  if (page === "activate") activatePage();
})();
