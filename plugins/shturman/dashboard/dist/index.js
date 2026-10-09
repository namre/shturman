/*
 * Штурман — мастер первой настройки. Вкладка дашборда Hermes.
 *
 * Обычный скрипт без сборки: React и клиент API берутся из SDK дашборда.
 * Ключи, токен бота, список разрешённых пользователей и перезапуск шлюза уходят в штатные
 * вызовы Hermes (SDK.api). В /api/plugins/shturman идёт только то, чего в дашборде нет: выбор
 * помощника, привязка владельца, проверка модели, отметки шагов, состояние настройки переписки.
 *
 * Переписка настраивается не здесь, а на отдельной странице, которую отдаёт сервис переписки,
 * мимо Hermes и на своём адресе — не на адресе дашборда. Мастер показывает её состояние
 * и обычную ссылку на неё (адрес приходит с сервера уже проверенным). Ссылку входа он
 * не запрашивает и не показывает и ничего на эту страницу не передаёт. Бота-ассистента
 * в бизнес-режиме Telegram мастер не подключает; о боте согласований он только упоминает:
 * тот понадобится позже, если владелец решит разрешить отправку.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const h = React.createElement;
  const { useState, useEffect } = SDK.hooks;
  const api = SDK.api;

  const PLUGIN = "shturman";
  const API = "/api/plugins/" + PLUGIN;
  const BASE = (window.__HERMES_BASE_PATH__ || "").replace(/\/$/, "");
  const JSON_HEADERS = { "Content-Type": "application/json" };

  const get = (path) => SDK.fetchJSON(API + path);
  const post = (path, body) =>
    SDK.fetchJSON(API + path, { method: "POST", headers: JSON_HEADERS, body: JSON.stringify(body || {}) });
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function errorText(error) {
    const raw = String((error && error.message) || error || "");
    const match = raw.match(/"detail"\s*:\s*"((?:[^"\\]|\\.)*)"/);
    if (match) {
      try { return JSON.parse('"' + match[1] + '"'); } catch (e) { return match[1]; }
    }
    return raw.replace(/^\d{3}:\s*/, "") || "Что-то пошло не так. Попробуйте ещё раз.";
  }

  /* ------------------------------------------------------------ мелкие детали */

  const Check = () =>
    h("svg", { width: 16, height: 16, viewBox: "0 0 16 16", fill: "none", stroke: "currentColor",
               strokeWidth: 2, strokeLinecap: "round", strokeLinejoin: "round", "aria-hidden": true },
      h("path", { d: "M3 8.5l3.2 3.2L13 4.8" }));

  function Note({ kind, children }) {
    return h("div", { className: "shturman-note shturman-note-" + (kind || "info"),
                      role: kind === "error" ? "alert" : "status" },
      kind === "ok" ? h(Check) : null, h("span", null, children));
  }

  function Btn({ kind, busy, children, ...rest }) {
    return h("button", { ...rest, type: "button", className: "shturman-btn shturman-btn-" + (kind || "primary"),
                         disabled: !!rest.disabled || !!busy },
      busy ? "Подождите…" : children);
  }

  function Field({ id, label, hint, children }) {
    return h("div", { className: "shturman-field" },
      h("label", { htmlFor: id }, label), children,
      hint ? h("div", { className: "shturman-hint" }, hint) : null);
  }

  function Section({ n, title, locked, lockedText, children }) {
    return h("section", { className: "shturman-section" + (locked ? " is-locked" : "") },
      h("h3", null, n ? n + ". " : "", title),
      locked ? h("p", { className: "shturman-muted" }, lockedText) : children);
  }

  /* ------------------------------------------- схемы «куда нажать и что ввести»
   * Это условные рисунки, а не снимки чужих экранов: показывают порядок действий
   * и место, куда смотреть. Подписи на реальном экране могут немного отличаться. */

  const Mark = ({ n }) => h("span", { className: "shturman-mark", "aria-hidden": true }, n);

  function Shot({ title, legend, children }) {
    return h("figure", { className: "shturman-shot" },
      h("div", { className: "shturman-shot-bar" }, title),
      h("div", { className: "shturman-shot-body" }, children),
      h("figcaption", null,
        h("ol", null, legend.map((text, i) => h("li", { key: i }, text))),
        h("div", { className: "shturman-shot-foot" }, "Схема. Подписи на вашем экране могут немного отличаться.")));
  }

  const Out = ({ n, children }) =>
    h("div", { className: "shturman-msg shturman-msg-out" + (n ? " is-marked" : "") },
      n ? h(Mark, { n }) : null, children);
  const In = ({ children }) => h("div", { className: "shturman-msg shturman-msg-in" }, children);
  const Hl = ({ n, children }) =>
    h("span", { className: "shturman-hl" }, n ? h(Mark, { n }) : null, children);
  const Key = ({ n, children }) =>
    h("div", { className: "shturman-key" + (n ? " is-marked" : "") }, n ? h(Mark, { n }) : null, children);
  const Addr = ({ children }) => h("div", { className: "shturman-addr" }, children);

  const ShotKey = ({ site, button }) =>
    h(Shot, { title: "Браузер · кабинет провайдера", legend: [
      "Откройте страницу ключей и нажмите «" + button + "».",
      "Скопируйте ключ сразу: провайдер показывает его один раз.",
      "Вставьте ключ в поле «Ключ» на этой странице.",
    ] },
      h(Addr, null, site),
      h(Key, { n: 1 }, button),
      h("div", { className: "shturman-secret" }, h(Hl, { n: 2 }, "sk-••••••••••••"), h("span", { className: "shturman-copy" }, "Copy")));

  const ShotDevice = ({ code }) =>
    h(Shot, { title: "Браузер · страница OpenAI", legend: [
      "Войдите в свой аккаунт ChatGPT, если страница попросит.",
      "Введите код, который показан слева.",
      "Нажмите Continue и вернитесь сюда — мастер продолжит сам.",
    ] },
      h(Addr, null, "auth.openai.com/codex/device"),
      h("div", { className: "shturman-input-fake" }, h(Hl, { n: 2 }, code || "XXXX-XXXX")),
      h(Key, { n: 3 }, "Continue"));

  const ShotNewBot = () =>
    h(Shot, { title: "Telegram · @BotFather", legend: [
      "Найдите в Telegram @BotFather (с синей галочкой) и отправьте /newbot.",
      "Придумайте название — так бот будет подписан в списке чатов.",
      "Придумайте адрес латиницей, он должен заканчиваться на bot.",
      "Скопируйте токен из ответа: длинную строку с двоеточием.",
    ] },
      h(Out, { n: 1 }, "/newbot"),
      h(In, null, "Alright, a new bot. How are we going to call it?"),
      h(Out, { n: 2 }, "Мой Штурман"),
      h(In, null, "Good. Now let's choose a username… It must end in `bot`."),
      h(Out, { n: 3 }, "ivan_shturman_bot"),
      h(In, null, "Done! … Use this token to access the HTTP API:", h("br"),
        h(Hl, { n: 4 }, "1234567890:AAH…xyz")));

  const ShotStart = ({ bot }) =>
    h(Shot, { title: "Telegram · @" + (bot || "ваш_бот"), legend: [
      "Нажмите «Открыть бота» слева — откроется чат с вашим ботом.",
      "Внизу чата нажмите «Запустить» (в английской версии — Start).",
      "Бот ответит «Принято». Вернитесь сюда и подтвердите, что это вы.",
    ] },
      h(In, null, "Здесь будет ваш разговор с ассистентом."),
      h("div", { className: "shturman-startbar" }, h(Key, { n: 2 }, "ЗАПУСТИТЬ")));

  /* ----------------------------------------------- память страницы и операции
   *
   * Дашборд может заново смонтировать вкладку в любой момент (например, когда уточнил
   * активный профиль или когда перезапускается шлюз). Поэтому всё, что должно пережить
   * перемонтирование, лежит не в состоянии компонентов, а здесь: ответ сервера, текущий шаг,
   * ссылка привязки и ход длинных операций. Операции не зависят от того, смонтирован ли
   * компонент, который их запустил: начатая настройка всегда доводится до конца. */

  const mem = { st: null, index: null, draft: null, model: {}, bot: {}, corr: null };
  const jobs = {};
  const listeners = new Set();
  const emit = () => listeners.forEach((fn) => fn());

  function useStore() {
    const [, force] = useState(0);
    useEffect(() => {
      const fn = () => force((x) => x + 1);
      listeners.add(fn);
      return () => { listeners.delete(fn); };
    }, []);
  }

  const patch = (key, values) => { mem[key] = Object.assign({}, mem[key], values); emit(); };
  const job = (name) => jobs[name] || { status: "idle", error: "" };
  const running = (name) => job(name).status === "running";

  /* fn возвращает строку с ошибкой либо ничего. Повторный запуск идущей операции игнорируется. */
  async function run(name, fn) {
    if (running(name)) return;
    jobs[name] = { status: "running", error: "" }; emit();
    try {
      const failure = await fn();
      jobs[name] = failure ? { status: "error", error: failure } : { status: "ok", error: "" };
    } catch (e) {
      jobs[name] = { status: "error", error: errorText(e) };
    }
    emit();
  }

  async function loadState() {
    mem.st = await get("/state");
    emit();
    return mem.st;
  }

  const JobError = ({ names }) => {
    const failed = names.map(job).filter((j) => j.status === "error")[0];
    return failed ? h(Note, { kind: "error" }, failed.error) : null;
  };

  /* ---------------------------------------------------------- ожидание шлюза */

  const HEALTHY = ["connected", "running", "ok"];

  const telegramState = (status) => ((status && status.gateway_platforms) || {}).telegram || {};

  /* Перезапускает шлюз и ждёт, пока бот заново выйдет на связь.
   * Старая запись «на связи» держится ещё несколько секунд после команды, поэтому
   * успехом считается только запись новее той, что была до перезапуска. */
  async function restartGatewayAndWait(timeoutMs) {
    let before = {};
    try { before = telegramState(await api.getStatus()); } catch (e) { /* статуса ещё нет */ }
    await api.restartGateway();
    const deadline = Date.now() + (timeoutMs || 120000);
    while (Date.now() < deadline) {
      await sleep(3000);
      try {
        const tg = telegramState(await api.getStatus());
        const fresh = tg.updated_at !== before.updated_at || HEALTHY.indexOf(before.state) === -1;
        if (fresh && HEALTHY.indexOf(tg.state) !== -1) return "";
        if (fresh && tg.state === "fatal") return tg.error_message || "Telegram отклонил подключение.";
      } catch (e) { /* дашборд мог моргнуть — пробуем дальше */ }
    }
    return "Бот не вышел на связь за две минуты.";
  }

  async function loadTelegramPlatform() {
    const data = await api.getMessagingPlatforms();
    const platform = (data.platforms || []).filter((p) => p.id === "telegram")[0] || {};
    patch("bot", { platform });
    return platform;
  }

  const envIsSet = (platform, key) =>
    ((platform && platform.env_vars) || []).some((v) => v.key === key && v.is_set);

  /* ============================================================ шаг 1: помощник */

  function savePersona(form, next) {
    return run("persona", async () => {
      let profile = "default";
      try { profile = (await SDK.fetchJSON("/api/profiles/active")).active || "default"; } catch (e) { /* по умолчанию */ }
      const soul = await api.getProfileSoul(profile);
      const res = await post("/persona", Object.assign({}, form, { soul: soul.content || "" }));
      await api.updateProfileSoul(profile, res.soul);
      mem.draft = null;
      await loadState();
      next();
    });
  }

  function PersonaStep({ st, next }) {
    const cat = st.catalog;
    const form = mem.draft || st.persona;
    const set = (values) => { mem.draft = Object.assign({}, form, values); emit(); };

    const introText = form.intro === "custom"
      ? (form.custom_intro || "[ваш вариант]")
      : (cat.intros.filter((i) => i.id === form.intro)[0] || cat.intros[0]).text;

    const cards = cat.personas.concat([{
      id: "custom", name: "Своё имя",
      idea: "Назовите ассистента сами и опишите характер своими словами.",
      sample: "Имя и описание можно поменять в любой момент.",
    }]);
    const tones = [["off", "Только имя"], ["light", "Интонация"], ["full", "Характер целиком"]];

    return h("div", { className: "shturman-step" },
      h("h2", null, "Кто будет вашим помощником"),
      h("p", { className: "shturman-lead" },
        "Имя и характер действуют только в вашем чате с ассистентом. Ответы другим людям он пишет вашим голосом."),

      h("div", { className: "shturman-cards", role: "group", "aria-label": "Имя и характер" },
        cards.map((p) => h("button", {
          key: p.id, type: "button", "aria-pressed": form.persona === p.id,
          className: "shturman-card" + (form.persona === p.id ? " is-selected" : ""),
          onClick: () => set({ persona: p.id }),
        },
          h("span", { className: "shturman-card-name" }, p.name),
          h("span", null, p.idea),
          h("span", { className: "shturman-card-sample" }, p.id === "custom" ? p.sample : "«" + p.sample + "»")))),

      form.persona === "custom" ? h("div", { className: "shturman-grid2" },
        h(Field, { id: "sh-own-name", label: "Имя ассистента" },
          h("input", { id: "sh-own-name", type: "text", maxLength: 40, value: form.custom_name,
                       placeholder: "Как вы будете его называть",
                       onChange: (e) => set({ custom_name: e.target.value }) })),
        h(Field, { id: "sh-own-voice", label: "Как он говорит" },
          h("input", { id: "sh-own-voice", type: "text", maxLength: 600, value: form.custom_voice,
                       placeholder: "Например: сухо и точно, без шуток",
                       onChange: (e) => set({ custom_voice: e.target.value }) }))) : null,

      h("div", { className: "shturman-grid2" },
        h(Field, { id: "sh-owner", label: "Как к вам обращаться" },
          h("input", { id: "sh-owner", type: "text", maxLength: 80, value: form.owner_address,
                       placeholder: "Имя или имя и отчество",
                       onChange: (e) => set({ owner_address: e.target.value }) })),
        h("div", { className: "shturman-field" },
          h("span", { className: "shturman-label", id: "sh-tone-label" }, "Насколько выражен характер"),
          h("div", { className: "shturman-pills", role: "group", "aria-labelledby": "sh-tone-label" },
            tones.map((t) => h("button", {
              key: t[0], type: "button", "aria-pressed": form.tone === t[0],
              className: "shturman-pill" + (form.tone === t[0] ? " is-selected" : ""),
              onClick: () => set({ tone: t[0] }),
            }, t[1]))))),

      h("section", { className: "shturman-section" },
        h("h3", null, "Как он представляется другим людям"),
        h("p", { className: "shturman-muted" },
          "Так ассистент называет себя, когда пишет вашим собеседникам сам, а не готовит черновик от вашего имени."),
        h("div", { className: "shturman-pills", role: "group", "aria-label": "Как представляется ассистент" },
          cat.intros.concat([{ id: "custom", label: "Свой вариант" }]).map((i) => h("button", {
            key: i.id, type: "button", "aria-pressed": form.intro === i.id,
            className: "shturman-pill" + (form.intro === i.id ? " is-selected" : ""),
            onClick: () => set({ intro: i.id }),
          }, i.label))),
        h("div", { className: "shturman-grid2" },
          form.intro === "custom" ? h(Field, { id: "sh-intro-own", label: "Свой вариант" },
            h("input", { id: "sh-intro-own", type: "text", maxLength: 60, value: form.custom_intro,
                         placeholder: "Например: референт",
                         onChange: (e) => set({ custom_intro: e.target.value }) })) : null,
          h(Field, { id: "sh-gen", label: "Чей он помощник",
                     hint: "Впишите имя так, как оно должно звучать в подписи." },
            h("input", { id: "sh-gen", type: "text", maxLength: 80, value: form.owner_genitive,
                         placeholder: "Ивана Ивановича",
                         onChange: (e) => set({ owner_genitive: e.target.value }) }))),
        h("div", { className: "shturman-preview" },
          h("span", { className: "shturman-eyebrow" }, "Как это будет выглядеть"),
          h("div", null, "Здравствуйте. Я " + introText + " " + (form.owner_genitive || "[Ивана Ивановича]") + "."))),

      h(JobError, { names: ["persona"] }),
      h("div", { className: "shturman-actions" },
        h(Btn, { busy: running("persona"), onClick: () => savePersona(form, next) }, "Сохранить и продолжить")));
  }

  /* ============================================================== шаг 2: модель */

  const PROVIDERS = [
    { id: "openrouter", slug: "openrouter", env: "OPENROUTER_API_KEY", name: "Ключ OpenRouter",
      note: "Один ключ, много моделей. Оплата по расходу.",
      site: "openrouter.ai/settings/keys", url: "https://openrouter.ai/settings/keys", button: "Create Key" },
    { id: "openai", slug: "openai", env: "OPENAI_API_KEY", name: "Ключ OpenAI",
      note: "Ключ API OpenAI. Оплата по расходу.",
      site: "platform.openai.com/api-keys", url: "https://platform.openai.com/api-keys", button: "Create new secret key" },
    { id: "chatgpt", slug: "openai-codex", env: "", name: "Подписка ChatGPT",
      note: "Вход по подписке, без ключа. Голосовые и поиск по смыслу работают на вашем сервере, ключ им не нужен." },
  ];
  const providerById = (id) => PROVIDERS.filter((p) => p.id === id)[0] || PROVIDERS[0];

  /* Читает строку провайдера из списка моделей Hermes и подбирает модель по умолчанию. */
  async function loadModelRow(pid, refresh) {
    const slug = providerById(pid).slug;
    const data = await api.getModelOptions({ refresh: !!refresh });
    const row = (data.providers || []).filter((r) => r.slug === slug)[0] || null;
    let pick = "";
    if (row && row.authenticated) {
      pick = data.provider === slug ? (data.model || "") : "";
      if (!pick) {
        try { pick = (await SDK.fetchJSON("/api/model/recommended-default?provider=" + encodeURIComponent(slug))).model || ""; }
        catch (e) { /* подсказки нет — возьмём из списка */ }
      }
      if (!pick) pick = (row.featured_models || [])[0] || (row.models || [])[0] || "";
    }
    if ((mem.model.pid || "openrouter") !== pid) return;   // пока грузили, выбрали другого провайдера
    patch("model", { row, model: mem.model.model || pick });
  }

  function chooseProvider(pid) {
    mem.model = { pid };
    ["model.key", "model.device", "model.probe"].forEach((n) => { if (!running(n)) delete jobs[n]; });
    emit();
    loadModelRow(pid, false).catch(() => {});
  }

  function saveModelKey(pid, value) {
    const provider = providerById(pid);
    return run("model.key", async () => {
      if (!value) return "Вставьте ключ.";
      const check = await SDK.fetchJSON("/api/providers/validate", {
        method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ key: provider.env, value }),
      });
      if (!check.ok) {
        return check.reachable
          ? "Провайдер не принял ключ. Проверьте, что скопировали его целиком."
          : "Нет связи с провайдером. Проверьте интернет на сервере и попробуйте ещё раз.";
      }
      await api.setEnvVar(provider.env, value);
      patch("model", { replaceKey: false });
      await loadModelRow(pid, true);
      return "";
    });
  }

  function startDeviceLogin() {
    return run("model.device", async () => {
      const start = await api.startOAuthLogin("openai-codex");
      patch("model", { device: start });
      const interval = Math.max(3, Number(start.poll_interval || 5)) * 1000;
      const deadline = Date.now() + Math.max(60, Number(start.expires_in || 900)) * 1000;
      while (Date.now() < deadline) {
        await sleep(interval);
        const poll = await api.pollOAuthSession("openai-codex", start.session_id);
        if (poll.status === "pending") continue;
        patch("model", { device: null });
        if (poll.status !== "approved") return poll.error_message || "Вход не завершён. Попробуйте ещё раз.";
        await loadModelRow("chatgpt", true);
        return "";
      }
      patch("model", { device: null });
      return "Время на ввод кода вышло. Попробуйте ещё раз.";
    });
  }

  function chooseAndProbe(pid, confirmed) {
    const provider = providerById(pid);
    return run("model.probe", async () => {
      const model = (mem.model.model || "").trim();
      if (!model) return "Выберите модель.";
      patch("model", { probe: null, confirm: "" });
      const res = await api.setModelAssignment({
        scope: "main", provider: provider.slug, model, confirm_expensive_model: !!confirmed,
      });
      if (res && res.confirm_required) {
        patch("model", { confirm: res.confirm_message || "Эта модель дорогая. Выбрать её?" });
        return "";
      }
      const probe = await post("/model/probe");
      patch("model", { probe });
      if (probe.ok) await loadState();
      return "";
    });
  }

  function ModelStep({ st, next }) {
    const m = mem.model;
    const pid = m.pid || "openrouter";
    const provider = providerById(pid);
    const [key, setKey] = useState("");

    useEffect(() => { if (m.row === undefined) loadModelRow(pid, false).catch(() => {}); }, [pid]);

    const row = m.row || null;
    const authed = !!(row && row.authenticated);
    const models = row ? (row.featured_models && row.featured_models.length ? row.featured_models : row.models || []) : [];
    const probing = running("model.probe");

    return h("div", { className: "shturman-step" },
      h("h2", null, "Какая модель будет думать"),
      h("p", { className: "shturman-lead" }, "Ассистенту нужна языковая модель. Выберите, как вы за неё платите."),

      h("div", { className: "shturman-cards shturman-cards-3", role: "group", "aria-label": "Способ оплаты модели" },
        PROVIDERS.map((p) => h("button", {
          key: p.id, type: "button", "aria-pressed": pid === p.id,
          className: "shturman-card" + (pid === p.id ? " is-selected" : ""),
          onClick: () => { setKey(""); chooseProvider(p.id); },
        }, h("span", { className: "shturman-card-name" }, p.name), h("span", null, p.note)))),

      h("div", { className: "shturman-split" },
        h("div", { className: "shturman-col" },

          provider.env ? h(Section, { n: 1, title: "Ключ" },
            authed && !m.replaceKey
              ? h("div", { className: "shturman-stack" },
                  h(Note, { kind: "ok" }, "Ключ сохранён на сервере."),
                  h("div", null, h(Btn, { kind: "ghost", onClick: () => patch("model", { replaceKey: true }) }, "Заменить ключ")))
              : h("div", { className: "shturman-stack" },
                  h("p", null, "Ключ выдаёт провайдер в личном кабинете: ",
                    h("a", { href: provider.url, target: "_blank", rel: "noopener noreferrer" }, provider.site), "."),
                  h(Field, { id: "sh-key", label: "Ключ",
                             hint: "Ключ сохраняется на вашем сервере. ИИ-агент, который разворачивал сервер, его не видит." },
                    h("input", { id: "sh-key", type: "password", autoComplete: "off", value: key,
                                 placeholder: "Вставьте ключ из кабинета провайдера",
                                 onChange: (e) => setKey(e.target.value) })),
                  h("div", null, h(Btn, { busy: running("model.key"),
                                          onClick: () => saveModelKey(pid, key.trim()).then(() => { if (job("model.key").status === "ok") setKey(""); }) },
                    "Проверить и сохранить ключ"))))
          : h(Section, { n: 1, title: "Вход по подписке" },
            authed
              ? h(Note, { kind: "ok" }, "Подписка ChatGPT подключена.")
              : m.device
                ? h("div", { className: "shturman-stack" },
                    h("p", null, "Откройте страницу OpenAI и введите там этот код:"),
                    h("div", { className: "shturman-bigcode" }, m.device.user_code),
                    h("div", null, h("a", { className: "shturman-btn shturman-btn-primary", href: m.device.verification_url,
                                            target: "_blank", rel: "noopener noreferrer" }, "Открыть страницу OpenAI")),
                    h("p", { className: "shturman-muted" }, "Жду подтверждения. Эта страница обновится сама."))
                : h("div", { className: "shturman-stack" },
                    h("p", null, "Мастер покажет короткий код и ссылку на страницу OpenAI. Пароль от ChatGPT здесь вводить не нужно."),
                    h("div", null, h(Btn, { busy: running("model.device"), onClick: startDeviceLogin }, "Войти через ChatGPT")))),

          h(Section, { n: 2, title: "Модель и проверка", locked: !authed,
                       lockedText: provider.env ? "Откроется после сохранения ключа." : "Откроется после входа." },
            h("div", { className: "shturman-stack" },
              h(Field, { id: "sh-model", label: "Модель",
                         hint: "Можно оставить предложенную. Поменять модель потом можно в любой момент. " +
                               "Если захотите, чтобы ассистент понимал фото и сканы (шаг «Фото и документы» на странице «Переписка»), " +
                               "модель должна понимать изображения: например, GPT-5 и GPT-6 от OpenAI (в том числе по подписке ChatGPT), " +
                               "Gemini от Google, Claude от Anthropic. Чисто текстовая модель документы с текстом разберёт, а фото — нет. " +
                               "На OpenRouter это видно в описании модели: среди входных данных есть изображения (image)." },
                h("input", { id: "sh-model", type: "text", list: "sh-model-list", value: m.model || "", autoComplete: "off",
                             placeholder: "Начните вводить название",
                             onChange: (e) => patch("model", { model: e.target.value, probe: null }) }),
                h("datalist", { id: "sh-model-list" }, models.slice(0, 200).map((x) => h("option", { key: x, value: x })))),
              m.confirm ? h("div", { className: "shturman-stack" },
                h(Note, { kind: "warn" }, m.confirm),
                h("div", null, h(Btn, { busy: probing, onClick: () => chooseAndProbe(pid, true) }, "Да, выбрать эту модель"))) : null,
              h("div", null, h(Btn, { busy: probing, onClick: () => chooseAndProbe(pid, false) }, "Выбрать и проверить")),
              probing ? h("p", { className: "shturman-muted" }, "Задаю модели пробный вопрос. Это занимает до минуты.") : null,
              m.probe && m.probe.ok ? h(Note, { kind: "ok" }, "Модель ответила: «" + m.probe.reply + "». Можно идти дальше.") : null,
              m.probe && !m.probe.ok ? h(Note, { kind: "error" }, "Модель не ответила. " + m.probe.error) : null))),

        h("div", { className: "shturman-col shturman-col-aside" },
          provider.env
            ? h(ShotKey, { site: provider.site, button: provider.button })
            : h(ShotDevice, { code: m.device && m.device.user_code }))),

      h(JobError, { names: ["model.key", "model.device", "model.probe"] }),
      h("div", { className: "shturman-actions" },
        h(Btn, { kind: st.marks.model_ok ? "primary" : "ghost", onClick: next },
          st.marks.model_ok ? "Далее" : "Пропустить пока")));
  }

  /* ================================================================= шаг 3: бот */

  function saveBotToken(value) {
    return run("bot.token", async () => {
      if (!value) return "Вставьте токен.";
      const check = await post("/bot/check", { token: value });
      if (!check.ok) return check.error;
      await api.updateMessagingPlatform("telegram", { enabled: true, env: { TELEGRAM_BOT_TOKEN: value } });
      patch("bot", { replaceToken: false });
      await loadState();
      await loadTelegramPlatform();
      const failure = await restartGatewayAndWait();
      await loadTelegramPlatform();
      return failure ? failure + " Проверьте токен: нажмите «Заменить токен» и вставьте его заново." : "";
    });
  }

  function reconnectBot() {
    return run("bot.token", async () => {
      const failure = await restartGatewayAndWait();
      await loadTelegramPlatform();
      return failure;
    });
  }

  function startPairing() {
    return run("bot.pair", async () => {
      patch("bot", { pair: await post("/pairing/start") });
      return "";
    });
  }

  /* Владелец подтвердил, что боту написал он: закрепляем привязку, записываем его в Hermes как
   * единственного разрешённого пользователя и как чат для сообщений ассистента, перезапускаем бота. */
  function confirmOwner() {
    return run("bot.apply", async () => {
      const owner = (await post("/pairing/confirm")).owner;
      patch("bot", { pair: null, rebind: false });
      await api.updateMessagingPlatform("telegram", { env: { TELEGRAM_ALLOWED_USERS: String(owner.user_id) } });
      await api.setEnvVar("TELEGRAM_HOME_CHANNEL", String(owner.chat_id));
      const failure = await restartGatewayAndWait();
      await post("/mark", { key: "bot_applied" });
      await loadState();
      await loadTelegramPlatform();
      return failure;
    });
  }

  function rejectCandidate() {
    return run("bot.pair", async () => {
      await post("/pairing/reject");
      patch("bot", { pair: null });
      await loadState();
      return "";
    });
  }

  function BotStep({ st, next }) {
    const b = mem.bot;
    const [token, setToken] = useState("");
    const owner = st.pairing.owner;
    const candidate = st.pairing.candidate || null;
    const pair = candidate ? null : (b.pair || null);
    const botName = (st.bot && st.bot.username) || (pair && pair.bot_username) || "";

    useEffect(() => { loadTelegramPlatform().catch(() => {}); }, []);

    // Пока окно привязки открыто, ждём, когда владелец нажмёт «Запустить» в Telegram.
    useEffect(() => {
      if (!pair) return undefined;
      let stop = false;
      (async () => {
        while (!stop) {
          await sleep(2000);
          if (stop) return;
          let status;
          try { status = await get("/pairing"); } catch (e) { continue; }
          if (stop) return;
          if (status.candidate) { patch("bot", { pair: null }); loadState().catch(() => {}); return; }
          if (!status.pending) {
            patch("bot", { pair: null });
            jobs["bot.pair"] = { status: "error", error: "Время привязки вышло. Получите новую ссылку." };
            emit();
            return;
          }
        }
      })();
      return () => { stop = true; };
    }, [pair && pair.expires_at]);

    const platform = b.platform || null;
    const tokenSet = envIsSet(platform, "TELEGRAM_BOT_TOKEN");
    const connected = !!(platform && HEALTHY.indexOf(platform.state) !== -1);
    const connecting = running("bot.token");
    const applying = running("bot.apply");
    const applied = !!(owner && st.marks.bot_applied && !b.rebind);
    const showTokenForm = !tokenSet || b.replaceToken;

    return h("div", { className: "shturman-step" },
      h("h2", null, "Бот в Telegram"),
      h("p", { className: "shturman-lead" },
        "Это бот-ассистент: через него вы разговариваете с ассистентом. Он же присылает код для входа на эту страницу."),

      h("div", { className: "shturman-split" },
        h("div", { className: "shturman-col" },

          h(Section, { n: 1, title: "Создайте бота" },
            h("p", null, "Бота создаёте вы сами у официального @BotFather — так его токен не проходит через чужие сервисы. ",
              h("a", { href: "https://t.me/BotFather", target: "_blank", rel: "noopener noreferrer" }, "Открыть @BotFather"), ".")),

          h(Section, { n: 2, title: "Вставьте токен" },
            showTokenForm
              ? h("div", { className: "shturman-stack" },
                  h(Field, { id: "sh-token", label: "Токен бота",
                             hint: "Длинная строка с двоеточием из сообщения @BotFather." },
                    h("input", { id: "sh-token", type: "password", autoComplete: "off", value: token,
                                 placeholder: "1234567890:AAH…",
                                 onChange: (e) => setToken(e.target.value) })),
                  h("div", null, h(Btn, { busy: connecting, onClick: () => saveBotToken(token.trim()).then(() => { if (envIsSet(mem.bot.platform, "TELEGRAM_BOT_TOKEN")) setToken(""); }) },
                    "Проверить и сохранить")))
              : h("div", { className: "shturman-stack" },
                  h(Note, { kind: "ok" }, "Токен сохранён" + (botName ? ": бот @" + botName : "") + "."),
                  connecting || applying
                    ? h(Note, { kind: "info" }, "Бот выходит на связь с Telegram. Обычно это меньше минуты.")
                    : connected
                      ? h(Note, { kind: "ok" }, "Бот на связи.")
                      : h("div", { className: "shturman-stack" },
                          h(Note, { kind: "warn" }, "Бот пока не на связи."),
                          h("div", null, h(Btn, { kind: "ghost", onClick: reconnectBot }, "Подключить ещё раз"))),
                  connecting || applying ? null
                    : h("div", null, h(Btn, { kind: "ghost", onClick: () => patch("bot", { replaceToken: true }) }, "Заменить токен")))),

          h(Section, { n: 3, title: "Привяжите бота к себе",
                       locked: !applying && !applied && (showTokenForm || !connected || connecting),
                       lockedText: "Откроется, когда бот выйдет на связь." },
            applying
              ? h(Note, { kind: "info" }, "Вы привязаны. Сохраняю настройки и перезапускаю бота. Это занимает до минуты.")
              : applied
                ? h("div", { className: "shturman-stack" },
                    h(Note, { kind: "ok" }, "Владелец привязан: " + (owner.name || "вы") +
                      ". Ассистент слушается только вас, а вход сюда теперь идёт по коду от этого бота."),
                    h("div", null, h(Btn, { kind: "ghost", onClick: () => patch("bot", { rebind: true, pair: null }) },
                      "Привязать другой аккаунт")))
                : candidate
                  ? h("div", { className: "shturman-stack" },
                      h("p", null, "Боту написал этот аккаунт Telegram:"),
                      h("div", { className: "shturman-preview" },
                        h("strong", null, candidate.name || "Без имени"),
                        h("span", { className: "shturman-muted" },
                          (candidate.username ? "@" + candidate.username + " · " : "") + "номер аккаунта " + candidate.user_id)),
                      h("p", null, "Это вы? После подтверждения ассистент будет слушаться только этот аккаунт, и коды для входа будут приходить ему."),
                      h("div", { className: "shturman-actions" },
                        h(Btn, { onClick: confirmOwner }, "Да, это я"),
                        h(Btn, { kind: "ghost", busy: running("bot.pair"), onClick: rejectCandidate }, "Нет, это не я")))
                : pair
                  ? h("div", { className: "shturman-stack" },
                      h("p", null, "Откройте бота и нажмите в Telegram «Запустить». Так ассистент узнает ваш аккаунт и будет слушаться только вас."),
                      h("div", null, h("a", { className: "shturman-btn shturman-btn-primary", href: pair.deep_link,
                                              target: "_blank", rel: "noopener noreferrer" }, "Открыть бота")),
                      h("p", { className: "shturman-muted" }, "Жду нажатия. Эта страница обновится сама."),
                      h("details", null,
                        h("summary", null, "Кнопка не открывает Telegram"),
                        h("p", null, "Найдите в Telegram бота @" + pair.bot_username + " и отправьте ему этот код:"),
                        h("div", { className: "shturman-bigcode" }, pair.code.slice(0, 3) + " " + pair.code.slice(3))))
                  : h("div", { className: "shturman-stack" },
                      h("p", null, "Мастер даст ссылку на вашего бота. По ней ничего вводить не придётся: одна кнопка в Telegram."),
                      h("div", null, h(Btn, { busy: running("bot.pair"), onClick: startPairing }, "Получить ссылку"))))),

        h("div", { className: "shturman-col shturman-col-aside" },
          !showTokenForm && connected ? h(ShotStart, { bot: botName }) : h(ShotNewBot))),

      h(JobError, { names: ["bot.token", "bot.pair", "bot.apply"] }),
      h("div", { className: "shturman-actions" },
        h(Btn, { kind: applied ? "primary" : "ghost", onClick: next }, applied ? "Далее" : "Пропустить пока")));
  }

  /* ============================================================ шаг 4: переписка
   *
   * Сама настройка идёт на отдельной странице сервиса переписки: ключи приложения Telegram,
   * вход в аккаунт по QR-коду, выбор чатов. Здесь — короткое объяснение, состояние (признаки
   * и числа из /correspondence) и обычная ссылка. Ссылки входа здесь нет. */

  const SETUP_LINK_CMD = "./ops/setup-link.sh";
  const SETUP_URL_CMD = "./ops/set-setup-url.sh";
  const INSTALLER = "того, кто ставил ассистента";
  const count = (n) => Number(n || 0).toLocaleString("ru-RU");

  async function loadCorrespondence() {
    try { mem.corr = await get("/correspondence"); }
    catch (e) { mem.corr = { state: "unreachable", url: null, setup: null, archive: {} }; }
    emit();
    return mem.corr;
  }

  const refreshCorrespondence = () => run("corr.load", async () => { await loadCorrespondence(); return ""; });

  /* Собирается ли переписка: подключён аккаунт Telegram или в архиве уже есть сообщения. На
   * экземпляре, где настроен бот согласований, переписку собирает и бизнес-режим. */
  const collecting = (c) => !!(c && c.setup && (c.setup.accounts > 0 || c.setup.business_connected === true ||
    (c.archive && c.archive.messages > 0)));

  /* Почему кнопки нет. Вид заметки и её текст — по состоянию из /correspondence. */
  function CorrProblem({ state }) {
    if (state === "no_origin") {
      return h(Note, { kind: "info" },
        "У страницы настройки пока нет адреса в интернете, поэтому кнопки здесь нет. Попросите " + INSTALLER +
        " выполнить ", h("code", null, SETUP_LINK_CMD), ": команда даст ссылку и подскажет, как открыть её со своего компьютера.");
    }
    if (state === "same_origin") {
      return h(Note, { kind: "warn" },
        "Страница настройки переписки отключена: ей задан тот же адрес, что у ассистента, а на общем адресе ваш вход " +
        "в Telegram проходил бы через ассистента. Попросите " + INSTALLER + " задать странице отдельный адрес командой ",
        h("code", null, SETUP_URL_CMD), " и перезапустить ассистента.");
    }
    if (state === "outdated") {
      return h(Note, { kind: "warn" },
        "Страница настройки переписки недоступна — обновите экземпляр. У сервиса переписки на вашем сервере " +
        "прежняя версия. Попросите " + INSTALLER + " обновить его.");
    }
    if (state === "disabled") {
      return h(Note, { kind: "warn" },
        "Страница настройки переписки выключена в сервисе переписки. Попросите " + INSTALLER + " запустить проверку.");
    }
    if (state === "no_service") {
      return h(Note, { kind: "warn" },
        "Сервис переписки не подключён к ассистенту. Попросите " + INSTALLER + " запустить проверку.");
    }
    return h(Note, { kind: "warn" },
      "Сервис переписки сейчас не отвечает, поэтому состояние показать не могу. Попросите " + INSTALLER + " запустить проверку.");
  }

  /* Три строки первой настройки: ключи, аккаунт, чаты (сколько их в архиве и сколько сообщений). */
  function CorrFacts({ c }) {
    const s = c.setup;
    const fact = (label, ok, text) => h("div", { className: "shturman-sumrow" },
      h("span", { className: "shturman-sumlabel" }, label),
      h("span", { className: ok ? "is-ok" : "shturman-muted" }, text));
    const a = c.archive || {};
    const messages = a.messages > 0 ? a.messages : 0;
    const chats = a.chats > 0 ? a.chats : 0;
    return h("div", { className: "shturman-summary shturman-facts" },
      fact("Ключи Telegram", s.tg_keys === true, s.tg_keys === true ? "Заданы" : s.tg_keys === false ? "Не заданы" : "Неизвестно"),
      fact("Аккаунт Telegram", s.accounts > 0, s.accounts > 1 ? "Подключено: " + count(s.accounts) : s.accounts === 1 ? "Подключён" : "Не подключён"),
      fact("Чаты", messages > 0 || chats > 0,
        messages > 0 || chats > 0 ? "В архиве: " + count(chats) + ", сообщений: " + count(messages) : "Не выбраны"));
  }

  function CorrespondenceStep({ st, next }) {
    const c = mem.corr;

    // Владелец уходит на страницу настройки в другую вкладку; когда возвращается — состояние свежее.
    useEffect(() => {
      loadCorrespondence();
      const onFocus = () => { loadCorrespondence(); };
      window.addEventListener("focus", onFocus);
      return () => window.removeEventListener("focus", onFocus);
    }, []);

    async function proceed() {
      try { await post("/mark", { key: "correspondence_seen" }); await loadState(); } catch (e) { /* отметка не критична */ }
      next();
    }

    const open = !!(c && c.state === "ok" && c.url);
    const ready = collecting(c);

    return h("div", { className: "shturman-step" },
      h("h2", null, "Переписка"),
      h("p", { className: "shturman-lead" },
        "Чтобы ассистент помнил, о чём вы договаривались, ему нужен архив вашей переписки в Telegram."),
      h("p", null,
        "Настройка — на отдельной защищённой странице, чтобы ваш вход в Telegram не проходил через ассистента. " +
        "Там вы создадите бота согласований — пульт, через который подтверждаете решения ассистента, — и выберете " +
        "один из двух способов: ассистент видит всё как вы (вход в ваш аккаунт) или работает как отдельный " +
        "сотрудник (бизнес-режим Telegram для личных чатов и свой аккаунт для групп)."),

      st.business.connected ? h(Note, { kind: "info" },
        "Сейчас в бизнес-режиме Telegram подключён бот-ассистент — так делали в прежних версиях. Ничего не сломано, " +
        "но теперь его так не подключают. Когда будет удобно, отключите его в Telegram: Настройки → " +
        "«Telegram для бизнеса» → «Чат-боты».") : null,

      h("div", { className: "shturman-split" },
        h("div", { className: "shturman-col" },

          h("section", { className: "shturman-section" },
            h("div", { className: "shturman-stack" },
              !c ? h("p", { className: "shturman-muted" }, "Узнаю состояние…") : null,
              c && !open ? h(CorrProblem, { state: c.state }) : null,
              open ? h("div", null,
                h("a", { className: "shturman-btn shturman-btn-primary", href: c.url,
                         target: "_blank", rel: "noopener noreferrer" }, "Открыть настройку переписки")) : null,
              open ? h("p", { className: "shturman-muted" },
                "Страница откроется в новой вкладке. Вернитесь сюда, когда закончите.") : null)),

          h(Section, { title: "Как войти" },
            h("div", { className: "shturman-stack" },
              h("p", null, "По одноразовой ссылке — её выдаёт тот, кто ставил ассистента, или вы сами командой ",
                h("code", null, SETUP_LINK_CMD), ". Ссылка действует 30 минут."),
              h("p", { className: "shturman-muted" },
                "Мастер эту ссылку не выдаёт и не показывает: так она не проходит через ассистента.")))),

        h("div", { className: "shturman-col shturman-col-aside" },
          h("section", { className: "shturman-section" },
            h("h3", null, "Что уже настроено"),
            c && c.setup ? h(CorrFacts, { c })
               : h("p", { className: "shturman-muted" }, c ? "Состояние недоступно." : "Узнаю состояние…"),
            h("div", null, h(Btn, { kind: "ghost", busy: running("corr.load"), onClick: refreshCorrespondence }, "Обновить"))))),

      h("p", { className: "shturman-muted shturman-fine" },
        "Разрешить ассистенту отправлять сообщения можно позже — это отдельное решение. До тех пор он читает, " +
        "запоминает и готовит черновики."),

      h(JobError, { names: ["corr.load"] }),
      h("div", { className: "shturman-actions" },
        h(Btn, { kind: ready ? "primary" : "ghost", onClick: proceed }, ready ? "Далее" : "Настрою позже")));
  }

  /* Одна строка для итога: что с перепиской. Возвращает [признак, текст]. */
  function correspondenceSummary(c) {
    if (!c) return [null, "Узнаю состояние…"];
    if (c.state === "outdated") return [false, "Страница настройки недоступна — обновите экземпляр"];
    if (c.state === "same_origin") return [false, "Страница настройки отключена: ей нужен отдельный адрес"];
    if (!c.setup) return [null, "Состояние недоступно: сервис переписки не отвечает"];
    if (!collecting(c)) return [false, "Не настроена — шаг «Переписка»"];
    const s = c.setup;
    const messages = c.archive && c.archive.messages > 0 ? c.archive.messages : 0;
    const parts = [];
    if (s.accounts > 1) parts.push("аккаунтов Telegram: " + count(s.accounts));
    else if (s.accounts === 1) parts.push("аккаунт Telegram подключён");
    parts.push("сообщений в архиве: " + count(messages));
    const text = parts.join(", ");
    return [true, text.charAt(0).toUpperCase() + text.slice(1)];
  }

  /* ================================================================ шаг 5: итог */

  function DoneStep({ st }) {
    const row = (label, ok, text) => h("div", { className: "shturman-sumrow" },
      h("span", { className: "shturman-sumlabel" }, label),
      h("span", { className: ok === true ? "is-ok" : ok === false ? "is-warn" : "shturman-muted" }, text));
    const botOk = !!(st.pairing.owner && st.marks.bot_applied);
    const corr = correspondenceSummary(mem.corr);
    useEffect(() => { loadCorrespondence(); }, []);
    const finish = () => run("finish", async () => { await post("/mark", { key: "completed" }); await loadState(); return ""; });

    return h("div", { className: "shturman-step" },
      h("h2", null, "Курс проложен"),
      h("p", { className: "shturman-lead" }, "Вот что настроено. К любому шагу можно вернуться из списка сверху."),
      h("div", { className: "shturman-summary" },
        row("Помощник", !!st.marks.persona_saved,
          st.marks.persona_saved ? st.resolved.name : "Не выбран: останется стоковый характер Hermes"),
        row("Представляется", null, st.resolved.signature),
        row("Модель", !!st.marks.model_ok, st.marks.model_ok ? "Проверена" : "Не проверена: ассистент не сможет отвечать"),
        row("Бот и вход", botOk, botOk ? "Владелец привязан" : "Не привязан: войти можно только по ссылке активации"),
        row("Переписка", corr[0], corr[1])),
      botOk ? h("p", null, "Напишите боту «привет». Если он ответил, всё работает.") : null,
      h(JobError, { names: ["finish"] }),
      st.completed
        ? h(Note, { kind: "ok" }, "Настройка завершена. Вкладка «Штурман» остаётся в меню — сюда можно вернуться.")
        : h("div", { className: "shturman-actions" }, h(Btn, { busy: running("finish"), onClick: finish }, "Завершить настройку")),
      st.completed ? h("div", { className: "shturman-actions" },
        h("a", { className: "shturman-btn shturman-btn-primary", href: BASE + "/sessions" }, "Перейти к разговорам")) : null);
  }

  /* ============================================================== сам мастер */

  const STEPS = [
    { id: "persona", title: "Помощник", view: PersonaStep },
    { id: "model", title: "Модель", view: ModelStep },
    { id: "bot", title: "Бот в Telegram", view: BotStep },
    { id: "correspondence", title: "Переписка", view: CorrespondenceStep },
    { id: "done", title: "Готово", view: DoneStep },
  ];

  function stepDone(st, id) {
    const m = st.marks;
    if (id === "persona") return !!m.persona_saved;
    if (id === "model") return !!m.model_ok;
    if (id === "bot") return !!(st.pairing.owner && m.bot_applied);
    // Прежние шаги «Бизнес-режим» и «Переписка и память» заменены одним. Кто прошёл их до
    // обновления (отметка business_skipped, подключённый бизнес-режим, завершённый мастер),
    // для того шаг остаётся пройденным.
    if (id === "correspondence") {
      return !!(m.correspondence_seen || m.business_skipped || st.business.connected || st.completed);
    }
    return !!st.completed;
  }

  function firstOpenStep(st) {
    for (let i = 0; i < STEPS.length; i++) if (!stepDone(st, STEPS[i].id)) return i;
    return STEPS.length - 1;
  }

  const goTo = (i) => { mem.index = Math.max(0, Math.min(STEPS.length - 1, i)); emit(); };

  function Wizard() {
    useStore();
    const [error, setError] = useState("");

    useEffect(() => {
      loadState().then((st) => { if (mem.index === null) goTo(firstOpenStep(st)); })
        .catch((e) => setError(errorText(e)));
    }, []);

    const st = mem.st;
    if (!st && error) return h("div", { className: "shturman" }, h(Note, { kind: "error" }, "Мастер не загрузился. " + error));
    if (!st || mem.index === null) return h("div", { className: "shturman" }, h("p", { className: "shturman-muted" }, "Загружаю…"));

    const index = mem.index;
    const View = STEPS[index].view;
    const next = () => { goTo(index + 1); window.scrollTo(0, 0); };

    return h("div", { className: "shturman" },
      h("header", { className: "shturman-head" },
        h("div", { className: "shturman-eyebrow" }, "Штурман · настройка · шаг " + (index + 1) + " из " + STEPS.length),
        h("ol", { className: "shturman-stepper" },
          STEPS.map((s, i) => {
            const done = stepDone(st, s.id);
            return h("li", { key: s.id },
              h("button", {
                type: "button", "aria-current": i === index ? "step" : undefined,
                className: "shturman-stepbtn" + (i === index ? " is-active" : "") + (done ? " is-done" : ""),
                onClick: () => goTo(i),
              },
                h("span", { className: "shturman-stepnum" }, done && i !== index ? h(Check) : i + 1),
                h("span", null, s.title)));
          }))),
      h(View, { key: STEPS[index].id, st, next }),
      index > 0 ? h("div", { className: "shturman-back" },
        h(Btn, { kind: "ghost", onClick: () => goTo(index - 1) }, "Назад")) : null);
  }

  /* Напоминание на остальных страницах, пока настройка не закончена. */
  function Banner() {
    useStore();
    useEffect(() => { if (!mem.st) loadState().catch(() => {}); }, []);
    if (!mem.st || mem.st.completed || /\/shturman\/?$/.test(window.location.pathname)) return null;
    return h("div", { className: "shturman-banner" },
      h("span", null, "Настройка Штурмана не завершена."),
      h("a", { href: BASE + "/shturman" }, "Продолжить настройку"));
  }

  window.__HERMES_PLUGINS__.register(PLUGIN, Wizard);
  if (typeof window.__HERMES_PLUGINS__.registerSlot === "function") {
    window.__HERMES_PLUGINS__.registerSlot(PLUGIN, "header-banner", Banner);
  }
})();
