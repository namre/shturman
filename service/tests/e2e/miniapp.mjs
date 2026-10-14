// Браузерная проверка Mini App бота согласований: вход по данным Telegram → «Согласования» →
// решение → пункт ушёл; чужой пользователь, устаревшие и повторённые данные входа не проходят;
// сессия приложения не открывает страницу настройки; рамка — только у web.telegram.org;
// экран «Чаты» (Р-80): список из базы, поиск, отбор, включение с глубиной, «Не сохранять»,
// список большого аккаунта (Р-81: загрузка в фоне, «подождите» от Telegram с отсчётом), аккаунт не подключён;
// раскладки списка и вкладка «Сферы» (Р-82).
// Запуск и стенд — README.md в этом каталоге. Печатает строки PASS/FAIL, код возврата 1 при FAIL.
//
// Настоящего Telegram здесь нет: данные входа подписывает пульт стенда так же, как Telegram, и
// приложение открывается как обычная страница с этими данными в адресе после «#» — так их
// передаёт и Telegram. Сообщения приложения Telegram'у (готово, развернуть) здесь уходить некуда.

import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.E2E_PLAYWRIGHT || "playwright");

const PORT = process.env.E2E_PORT || "8775";
const APP = `http://localhost:${PORT}/shturman-setup/app/`;
const CONTROL = `http://127.0.0.1:${Number(PORT) + 1}`;
const OUT = process.env.E2E_OUT || path.join(tmpdir(), "shturman-miniapp-shots");
const LOAD_OUT = process.env.E2E_LOAD_OUT || OUT;               // снимки загрузки списка чатов (Р-81)
const TOKEN = "7000000001:SENTINEL-bot-token_DoNotLeak-0123456789";
const THEME = { bg_color: "#1c242f", text_color: "#f5f5f5", hint_color: "#8d9aa8", button_color: "#3e88f7",
                button_text_color: "#ffffff", section_bg_color: "#232e3c", accent_text_color: "#6ab2f2" };
// Светлая тема Telegram для iOS: фон страницы и фон разделов — оба белые, отличается secondary_bg_color.
const THEME_LIGHT = { bg_color: "#ffffff", secondary_bg_color: "#efeff4", section_bg_color: "#ffffff",
                      text_color: "#000000", hint_color: "#8e8e93", link_color: "#007aff", button_color: "#007aff",
                      button_text_color: "#ffffff", accent_text_color: "#007aff", destructive_text_color: "#ff3b30",
                      section_separator_color: "#c8c7cc", subtitle_text_color: "#8e8e93",
                      section_header_text_color: "#6d6d72", header_bg_color: "#f8f8f8" };

mkdirSync(OUT, { recursive: true });
mkdirSync(LOAD_OUT, { recursive: true });
let failed = 0;
function check(name, ok, detail = "") {
  if (!ok) failed += 1;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}
async function control(pathname) {
  const res = await fetch(CONTROL + pathname);
  return pathname.startsWith("/init-data") ? res.text() : res.json();
}
function launchUrl(init, extra = {}) {
  const params = new URLSearchParams({ tgWebAppData: init, tgWebAppVersion: "8.0", tgWebAppPlatform: "ios",
                                       tgWebAppThemeParams: JSON.stringify(THEME), ...extra });
  return APP + "#" + params.toString();
}

const browser = await chromium.launch();
const phone = { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true, locale: "ru-RU" };

// --- 1. вне Telegram -----------------------------------------------------------------------------
{
  const context = await browser.newContext(phone);
  const page = await context.newPage();
  const response = await page.goto(APP);
  const csp = response.headers()["content-security-policy"] || "";
  check("страница приложения разрешает рамку только web.telegram.org",
        csp.includes("frame-ancestors https://web.telegram.org") && !response.headers()["x-frame-options"], csp);
  await page.waitForSelector("#screen-outside:not([hidden])");
  check("без данных Telegram приложение просит открыть его в Telegram", await page.isVisible("#screen-outside"));
  await context.close();
}

// --- 2. владелец входит и решает -----------------------------------------------------------------
const ownerInit = await control("/init-data?who=owner");
const context = await browser.newContext({ ...phone, colorScheme: "dark" });
const page = await context.newPage();
const errors = [];
const foreign = [];
page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
page.on("request", (r) => { if (!r.url().startsWith(`http://localhost:${PORT}/`)) foreign.push(r.url()); });
const bodies = [];
page.on("response", async (r) => { try { bodies.push(await r.text()); } catch (e) { /* не текст */ } });

await page.goto(launchUrl(ownerInit));
await page.waitForSelector("#screen-approvals:not([hidden])");
check("владелец вошёл: экран «Согласования»", await page.isVisible("#screen-approvals"));
check("данные входа убраны из адресной строки", !page.url().includes("tgWebAppData"), page.url());
const stored = await page.evaluate(() => ({ ss: sessionStorage.getItem("shturman-app-session") || "",
                                            ls: Object.keys(localStorage), cookie: document.cookie }));
check("ключ сессии — в sessionStorage, cookie нет, localStorage пуст",
      stored.ss.length > 30 && stored.cookie === "" && stored.ls.length === 0);
const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
check("цвета темы Telegram применены", bg === "rgb(28, 36, 47)", bg);

const cards = await page.$$eval(".card", (nodes) => nodes.map((n) => ({
  kind: n.querySelector(".kind").textContent, title: n.querySelector("h2").textContent, text: n.textContent,
  buttons: [...n.querySelectorAll("button")].map((b) => b.textContent) })));
const kinds = cards.map((c) => c.kind).sort();
check("в списке — черновик, подтверждение действия и страница памяти",
      JSON.stringify(kinds) === JSON.stringify(["Подтверждение действия", "Страница памяти", "Черновик ответа"]),
      JSON.stringify(kinds));
const draft = cards.find((c) => c.kind === "Черновик ответа");
check("черновик: адресат, текст дословно, кнопки как в боте (без «Изменить»)",
      draft && draft.text.includes("Кому: Иван Петров") && draft.text.includes("Смету пришлю в пятницу.\nС уважением.")
        && JSON.stringify(draft.buttons) === JSON.stringify(["Отправить", "Отклонить"]));
check("предупреждение: отправка на сервере выключена", await page.isVisible("#sending-off"));
await page.screenshot({ path: path.join(OUT, (process.env.E2E_SHOT_PREFIX || "") + "approvals-phone-dark.png"), fullPage: true });
const wide = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1);
check("страница не шире экрана телефона", wide);

// «Отправить» при выключенной отправке — ответ как в боте, черновик остаётся
await page.click(".card:has(.kind:text('Черновик ответа')) button:text('Отправить')");
await page.waitForSelector("#toast:not([hidden])");
const sendToast = await page.textContent("#toast");
check("«Отправить» при выключенной отправке: «Пока нельзя», черновик ждёт",
      sendToast.startsWith("Пока нельзя") && (await control("/state")).draft === "pending", sendToast);

// «Нет» под подтверждением действия — действие отклонено, пункт ушёл
await page.click(".card:has(.kind:text('Подтверждение действия')) button:text('Нет')");
await page.waitForFunction(() => ![...document.querySelectorAll(".card .kind")].some((n) => n.textContent === "Подтверждение действия"));
let state = await control("/state");
check("решение в приложении: действие отклонено, пункт исчез", state.action === "rejected", state.action);

// перезагрузка (так Telegram Web перезагружает рамку) — вход сохраняется
await page.reload();
await page.waitForSelector("#screen-approvals:not([hidden])");
check("после перезагрузки вход на месте", (await page.$$(".card")).length === 2);

// страница памяти — «Завести страницу»
await page.click(".card:has(.kind:text('Страница памяти')) button:text('Завести страницу')");
await page.waitForFunction(() => document.querySelectorAll(".card").length === 1);
state = await control("/state");
check("страница о человеке заведена тем же обработчиком, что под сводкой", state.page === "accepted", state.page);

check("в консоли нет ошибок", errors.length === 0, errors.join(" | "));

// сессия приложения не открывает страницу настройки (отказы 401 браузер пишет в консоль — это ожидаемо)
const key = await page.evaluate(() => sessionStorage.getItem("shturman-app-session"));
const setupTries = await page.evaluate(async (key) => {
  const out = [];
  for (const headers of [{ "X-Shturman-Session": key }, { "X-Shturman-App": key }]) {
    out.push((await fetch("/shturman-setup/api/state", { headers })).status);
    out.push((await fetch("/shturman-setup/api/bot/bind", {
      method: "POST", headers: { ...headers, "Content-Type": "application/json", "X-Shturman-Setup": "1" }, body: "{}" })).status);
  }
  return out;
}, key);
check("ключ приложения не открывает страницу настройки", setupTries.every((s) => s === 401), JSON.stringify(setupTries));

check("приложение не обращается к чужим адресам", foreign.length === 0, foreign.join(", "));
check("ключ сессии и токен бота не появляются в ответах, кроме ответа на вход",
      bodies.filter((b) => b.includes(key)).length === 1 && !bodies.some((b) => b.includes(TOKEN)));
await page.screenshot({ path: path.join(OUT, (process.env.E2E_SHOT_PREFIX || "") + "approvals-after.png"), fullPage: true });
await context.close();

// --- 3. чужой, устаревшие и повторённые данные входа ------------------------------------------------
for (const [name, init] of [
  ["чужой пользователь", await control("/init-data?who=stranger")],
  ["данные входа старше 10 минут", await control("/init-data?who=owner&age=900")],
  ["повтор уже использованных данных входа", ownerInit],
]) {
  const ctx = await browser.newContext(phone);
  const p = await ctx.newPage();
  await p.goto(launchUrl(init));
  await p.waitForSelector("#screen-failed:not([hidden])");
  check(`${name}: вход не состоялся`, await p.isVisible("#screen-failed") && !(await p.isVisible("#screen-approvals")));
  await ctx.close();
}
state = await control("/state");
check("сессий выдано ровно одна", state.sessions === 1, String(state.sessions));
const audit = JSON.stringify(state.audit);
check("журнал: вход, решения, отказ — без имён и текстов",
      audit.includes("app.login") && audit.includes("app.decide") && audit.includes("app.login_failed")
        && !audit.includes("Мария") && !audit.includes("Иван") && !audit.includes("Смету"), audit);

// --- 4. чужой сайт не открывает приложение в рамке -------------------------------------------------
{
  const ctx = await browser.newContext();
  const p = await ctx.newPage();
  await p.goto(CONTROL + "/framer");
  await p.waitForTimeout(1500);
  const inner = p.frames().find((f) => f !== p.mainFrame());
  let loaded = false;
  try { loaded = Boolean(inner && (await inner.$("#screen-outside, #screen-approvals"))); } catch (e) { loaded = false; }
  check("чужой сайт не может открыть приложение в рамке", !loaded);
  await ctx.close();
}

// --- 5. что видит человек: все виды карточек, обе темы, два размера телефона -------------------------
// Пульт заводит карточки всех видов из таблицы в approvals.py. Каждый вход — новыми данными входа
// (одни данные — одна сессия). Снимки нужно посмотреть глазами: сценарий проверяет только то,
// что можно проверить машиной, — нет ли в карточках сырых данных и не шире ли страница экрана.
const SHOT = process.env.E2E_SHOT_PREFIX || "";
const seededAll = await (await fetch(CONTROL + "/seed-all", { method: "POST" })).json();
check("пульт завёл карточки всех видов", seededAll.api.every(([s]) => s === 202) && seededAll.digests >= 1
      && seededAll.facts === 2 && seededAll.projects === 1 && seededAll.guard.sent === 1, JSON.stringify(seededAll));

const RAW = [
  [/\d{4}-\d{2}-\d{2}/, "дата ISO"], [/T\d{2}:\d{2}/, "время ISO"], [/\bsh:[a-z]{2}:/, "данные кнопки"],
  [/\b(None|null|undefined|NaN|true|false)\b/, "служебное значение"], [/[{}]|\[\s*["\d]/, "скобки словаря или списка"],
  [/"[a-z_]+"\s*:/, "ключ JSON"],
];
const ALL_KINDS = ["Задача ответа", "Подтверждение действия", "Скрытое сообщение", "Обязательство", "Факт о вас",
                   "Страница памяти", "Проект", "Черновик ответа"];

async function openApp(width, scheme, theme) {
  const ctx = await browser.newContext({ ...phone, viewport: { width, height: 800 }, colorScheme: scheme });
  const p = await ctx.newPage();
  const errs = [];
  p.on("console", (m) => { if (m.type() === "error") errs.push(m.text()); });
  await p.goto(launchUrl(await control("/init-data?who=owner"), { tgWebAppThemeParams: JSON.stringify(theme) }));
  await p.waitForSelector("#screen-approvals:not([hidden])");
  return { ctx, p, errs };
}

for (const [width, scheme, theme] of [[390, "dark", THEME], [390, "light", THEME_LIGHT], [360, "light", THEME_LIGHT],
                                      [360, "dark", THEME]]) {
  const { ctx, p, errs } = await openApp(width, scheme, theme);
  const tag = `${width}-${scheme}`;
  const seen = await p.$$eval(".card", (nodes) => nodes.map((n) => ({
    kind: n.querySelector(".kind").textContent,
    heading: n.querySelector("h2") ? n.querySelector("h2").textContent : "",
    framing: [...n.querySelectorAll(".kind, h2, .lines li, .note, .verbatim-label")].map((x) => x.textContent).join("\n"),
    text: n.innerText,
    clipped: [...n.querySelectorAll("button")].some((b) => b.scrollWidth > b.clientWidth + 1
      || b.getBoundingClientRect().right > n.getBoundingClientRect().right + 1),
    overlap: (() => {
      const rects = [...n.querySelectorAll("button")].map((b) => b.getBoundingClientRect());
      return rects.some((a, i) => rects.some((b, j) => i < j && a.left < b.right && b.left < a.right
        && a.top < b.bottom && b.top < a.bottom));
    })() })));
  const kinds = [...new Set(seen.map((c) => c.kind))];
  if (width === 390 && scheme === "dark") {
    check("в списке — карточки всех видов", ALL_KINDS.every((k) => kinds.includes(k)), JSON.stringify(kinds));
    const raw = seen.flatMap((c) => RAW.filter(([re]) => re.test(c.text)).map(([, what]) => `${c.kind}: ${what}`));
    check("в карточках нет сырых данных: дат ISO, кодов кнопок, None/null, словарей", raw.length === 0, raw.join("; "));
    const latin = seen.flatMap((c) => (c.framing.match(/[A-Za-z]{2,}/g) || [])
      .filter((w) => !["Telegram", "UTC"].includes(w)).map((w) => `${c.kind}: ${w}`));
    check("в заголовках и пояснениях карточек нет английских слов", latin.length === 0, latin.join("; "));
    check("заголовок карточки не повторяет её вид", seen.every((c) => c.heading !== c.kind
      && !c.heading.toLowerCase().startsWith(c.kind.toLowerCase())), JSON.stringify(seen.map((c) => [c.kind, c.heading])));
    const action = seen.find((c) => c.kind === "Подтверждение действия");
    check("подтверждение действия: описание без повтора «Подтвердите действие», предупреждение отдельно",
          action && !action.text.includes("Подтвердите действие") && action.text.includes("Если вы этого не просили"));
    check("у карточек со сроком — срок словами", seen.filter((c) => c.text.includes("Ждёт решения до")).length >= 3
      && seen.every((c) => !/Ждёт решения до \d/.test(c.text) || /(сегодня|завтра|\d+ [а-я]+) в \d/.test(c.text)));
  }
  check(`${tag}: кнопки не обрезаны и не налезают друг на друга`, seen.every((c) => !c.clipped && !c.overlap),
        JSON.stringify(seen.filter((c) => c.clipped || c.overlap).map((c) => c.kind)));
  check(`${tag}: страница не шире экрана`,
        await p.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1));
  // карточки отличимы от фона страницы — в светлой теме Telegram фон и разделы бывают одного цвета
  const contrast = await p.evaluate(() => {
    const card = document.querySelector(".card");
    const cs = getComputedStyle(card);
    return { page: getComputedStyle(document.body).backgroundColor, card: cs.backgroundColor, border: cs.borderTopWidth };
  });
  check(`${tag}: карточка отличается от фона страницы`, contrast.page !== contrast.card || contrast.border !== "0px",
        JSON.stringify(contrast));
  check(`${tag}: в консоли нет ошибок`, errs.length === 0, errs.join(" | "));
  await p.screenshot({ path: path.join(OUT, `${SHOT}list-${tag}.png`), fullPage: true });
  if (width === 390) {
    const handles = await p.$$(".card");
    for (let i = 0; i < handles.length; i++) {
      const kind = await handles[i].$eval(".kind", (n) => n.textContent);
      const slug = ALL_KINDS.indexOf(kind);
      await handles[i].screenshot({ path: path.join(OUT, `${SHOT}card-${scheme}-${String(i + 1).padStart(2, "0")}-k${slug}.png`) });
    }
  }
  await ctx.close();
}

// --- 6. сбои и пустой список -----------------------------------------------------------------------
{
  // сервер отвечает ошибкой на список (как при перезапуске за прокси) — экран с «Повторить», не вечное «Открываю…»
  const ctx = await browser.newContext({ ...phone, colorScheme: "light" });
  const p = await ctx.newPage();
  let fail = true;
  await p.route("**/api/app/approvals", (route) => fail
    ? route.fulfill({ status: 502, contentType: "text/html", body: "<html><body>502 Bad Gateway</body></html>" })
    : route.continue());
  await p.goto(launchUrl(await control("/init-data?who=owner"), { tgWebAppThemeParams: JSON.stringify(THEME_LIGHT) }));
  await p.waitForSelector("#screen-error:not([hidden])", { timeout: 5000 }).catch(() => {});
  check("ошибка сервера при первой загрузке: экран с понятным текстом и кнопкой «Повторить»",
        await p.isVisible("#screen-error") && await p.isVisible("#retry") && !(await p.isVisible("#screen-wait")));
  await p.screenshot({ path: path.join(OUT, `${SHOT}server-error-390-light.png`), fullPage: true });
  fail = false;
  await p.click("#retry").catch(() => {});
  await p.waitForSelector("#screen-approvals:not([hidden])", { timeout: 5000 }).catch(() => {});
  check("«Повторить» после сбоя показывает список", await p.isVisible("#screen-approvals"));
  await ctx.close();
}
{
  const ctx = await browser.newContext({ ...phone, colorScheme: "dark" });
  const p = await ctx.newPage();
  await p.route("**/api/app/login", (route) => route.fulfill({ status: 502, contentType: "text/html", body: "Bad Gateway" }));
  await p.goto(launchUrl(await control("/init-data?who=owner")));
  await p.waitForSelector("#screen-failed:not([hidden])");
  const text = await p.textContent("#failed-text");
  check("вход не прошёл из-за сбоя сервера: так и сказано", text.includes("Сервер сейчас не отвечает"), text);
  await ctx.close();
}
for (const [name, init, scheme] of [["failed-replay", ownerInit, "dark"], ["failed-old", null, "light"]]) {
  const ctx = await browser.newContext({ ...phone, viewport: { width: 360, height: 640 }, colorScheme: scheme });
  const p = await ctx.newPage();
  await p.goto(launchUrl(init || await control("/init-data?who=owner&age=900"),
                         { tgWebAppThemeParams: JSON.stringify(scheme === "dark" ? THEME : THEME_LIGHT) }));
  await p.waitForSelector("#screen-failed:not([hidden])");
  await p.screenshot({ path: path.join(OUT, `${SHOT}${name}-360-${scheme}.png`), fullPage: true });
  await ctx.close();
}
for (const scheme of ["light", "dark"]) {
  const ctx = await browser.newContext({ ...phone, viewport: { width: 360, height: 640 }, colorScheme: scheme });
  const p = await ctx.newPage();
  await p.goto(APP);
  await p.waitForSelector("#screen-outside:not([hidden])");
  await p.screenshot({ path: path.join(OUT, `${SHOT}outside-360-${scheme}.png`), fullPage: true });
  await ctx.close();
}
{
  await fetch(CONTROL + "/clear-all", { method: "POST" });
  const { ctx, p } = await openApp(390, "light", THEME_LIGHT);
  check("пустой список: «Сейчас ничего не ждёт вашего решения»", await p.isVisible("#empty")
        && (await p.$$(".card")).length === 0);
  await p.screenshot({ path: path.join(OUT, `${SHOT}empty-390-light.png`), fullPage: true });
  await ctx.close();
}

// --- 7. адреса приложения в обычном браузере: человек не видит голый JSON -----------------------------
{
  const ctx = await browser.newContext({ ...phone, colorScheme: "light" });
  const p = await ctx.newPage();
  const base = `http://localhost:${PORT}`;
  for (const [address, name] of [
    ["/shturman-setup/api/app/approvals", "api-approvals"], ["/shturman-setup/api/app/login", "api-login"],
    ["/shturman-setup/app/nothing", "app-404"], ["/shturman-setup/api/state", "page-api"],
    ["/shturman-setup/nothing", "page-404"],
  ]) {
    const res = await p.goto(base + address);
    const body = await p.evaluate(() => document.body.innerText);
    check(`открыт в браузере ${address}: страница по-русски, а не JSON (код ${res.status()})`,
          res.status() >= 400 && res.headers()["content-type"].startsWith("text/html")
            && !body.trim().startsWith("{") && /[а-я]/.test(body), body.slice(0, 80));
    await p.screenshot({ path: path.join(OUT, `${SHOT}browser-${name}.png`), fullPage: true });
  }
  // чужое имя узла — то, что видит человек, если адрес кнопки и адрес сервера разошлись
  // («localhost.» с точкой — то же место, но другое имя узла: его нет в перечне сервиса)
  const res = await p.goto(`http://localhost.:${PORT}/shturman-setup/app/`);
  const body = await p.evaluate(() => document.body.innerText);
  check("чужое имя узла: страница без JSON и без имени сервиса",
        res.status() === 421 && !body.includes("{") && !body.includes("Штурман"), body.slice(0, 80));
  await p.screenshot({ path: path.join(OUT, `${SHOT}browser-misdirected.png`), fullPage: true });
  // запросы скриптов по-прежнему получают JSON
  await p.goto(base + "/shturman-setup/");
  const apiFromPage = await p.evaluate(async () => {
    const r = await fetch("/shturman-setup/api/app/approvals");
    return { status: r.status, type: r.headers.get("content-type") };
  });
  check("запрос скрипта к API по-прежнему получает JSON", apiFromPage.status === 401
        && apiFromPage.type.startsWith("application/json"), JSON.stringify(apiFromPage));
  // адрес приложения без косой черты — перенаправление относительным адресом (за прокси схема https сохранится)
  const redirect = await fetch(base + "/shturman-setup/app", { redirect: "manual" });
  check("адрес приложения без «/» на конце: относительное перенаправление на /shturman-setup/app/",
        [301, 307, 308].includes(redirect.status) && redirect.headers.get("location") === "/shturman-setup/app/",
        `${redirect.status} ${redirect.headers.get("location")}`);
  await ctx.close();
}

// --- 8. экран «Чаты» (Р-80) ------------------------------------------------------------------------
// То же, что шаг «Какие чаты читать» на странице: список из базы сервиса, поиск, вид, «только
// выбранные», включение с глубиной, «Не сохранять» без стирания, «Обновить список из Telegram» — в фоне,
// с ходом загрузки и отсчётом, когда Telegram просит подождать. Каждое изменение — сообщением владельцу в бот.
// Ждёт, пока условие не станет истинным (сообщения владельцу уходят через очередь заданий).
async function waitFor(fn, ms = 5000) {
  const until = Date.now() + ms;
  while (Date.now() < until) { if (await fn()) return true; await new Promise((r) => setTimeout(r, 100)); }
  return false;
}
async function chatsSeen() { return (await fetch(CONTROL + "/chats/seen")).json(); }
async function chatsControl(pathname) { return (await fetch(CONTROL + pathname, { method: "POST" })).json(); }
// Отказы, которые браузер сам пишет в консоль («Failed to load resource … 429»), — ожидаемые ответы сервиса.
const realErrors = (errs) => errs.filter((e) => !e.startsWith("Failed to load resource"));

async function openChats(width, scheme, theme) {
  const opened = await openApp(width, scheme, theme);
  await opened.p.click("#tab-chats");
  await opened.p.waitForSelector("#chats-body:not([hidden]), #chats-none:not([hidden]), #chats-offline:not([hidden])");
  await opened.p.waitForFunction(() => document.querySelectorAll("#chats-list li").length > 0);
  return opened;
}

async function rowsOf(p) {
  return p.$$eval("#chats-list li.chat", (nodes) => nodes.map((n) => ({
    title: n.querySelector(".chat-title").textContent,
    meta: n.querySelector(".chat-meta").textContent,
    on: n.querySelector("input[type=checkbox]").checked,
    disabled: n.querySelector("input[type=checkbox]").disabled,
  })));
}

async function layoutOk(p) {
  return p.evaluate(() => {
    const wide = document.documentElement.scrollWidth <= window.innerWidth + 1;
    const rows = [...document.querySelectorAll("#chats-list li.chat")];
    const clipped = rows.filter((row) => {
      const box = row.getBoundingClientRect();
      return [...row.querySelectorAll(".switch, .link, .chat-title")].some((x) => {
        const r = x.getBoundingClientRect();
        return r.right > box.right + 1 || r.left < box.left - 1;
      });
    }).length;
    const controls = [...document.querySelectorAll("#chats-body button, #chats-body select, #chats-body input[type=search]")]
      .filter((x) => x.offsetParent !== null && x.getBoundingClientRect().right > window.innerWidth + 1).length;
    return { wide, clipped, controls };
  });
}

{
  const before = await chatsSeen();
  const { ctx, p, errs } = await openChats(390, "light", THEME_LIGHT);
  let seen = await chatsSeen();
  check("«Чаты»: список открылся из базы сервиса, без запроса к Telegram",
        JSON.stringify(seen.dialog_requests) === JSON.stringify(before.dialog_requests), JSON.stringify(seen.dialog_requests));
  const tabs = await p.$$eval("#chats-accounts button", (b) => b.map((x) => [x.textContent, x.getAttribute("aria-selected")]));
  check("«Чаты»: два аккаунта, выбран ваш (способ «как вы»)",
        tabs.length === 2 && tabs[0][0].startsWith("Ваш") && tabs[0][1] === "true", JSON.stringify(tabs));
  let rows = await rowsOf(p);
  const counter = await p.textContent("#chats-counter");
  check("«Чаты»: первые 20 чатов, счётчик выбранных и «Показать ещё»",
        rows.length === 20 && counter.includes("Выбрано для чтения: 2 чата") && await p.isVisible("#chats-more"), counter);
  check("«Чаты»: служебный чат Telegram помечен и не включается",
        rows.some((r) => r.title === "Telegram" && r.disabled && r.meta.includes("служебный")));
  const depth = await p.$eval("#chats-depth", (s) => s.value);
  check("«Чаты»: глубина по умолчанию — как в настройке аккаунта (12 месяцев)", depth === "12", depth);
  await p.screenshot({ path: path.join(OUT, `${SHOT}chats-list-390-light.png`), fullPage: false });

  // «Показать ещё»
  await p.click("#chats-more");
  await p.waitForFunction(() => document.querySelectorAll("#chats-list li.chat").length === 50);
  check("«Чаты»: «Показать ещё» догружает следующие", (await rowsOf(p)).length === 50);

  // поиск
  await p.fill("#chats-q", "иван");
  await p.waitForFunction(() => [...document.querySelectorAll("#chats-list .chat-title")].every((n) => /иван/i.test(n.textContent))
    && document.querySelectorAll("#chats-list li.chat").length > 0);
  rows = await rowsOf(p);
  check("«Чаты»: поиск по названию", rows.length >= 1 && rows[0].title === "Иван Петров", JSON.stringify(rows.map((r) => r.title)));

  // включение с глубиной
  await p.selectOption("#chats-depth", "3");
  await p.click("#chats-list li.chat:has(.chat-title:text-is('Иван Петров')) .switch");
  await p.waitForSelector("#toast:not([hidden])");
  const onToast = await p.textContent("#toast");
  await waitFor(async () => (await chatsSeen()).told.length > 0);
  seen = await chatsSeen();
  check("«Чаты»: чат включён с выбранной глубиной", seen.enabled.includes("Иван Петров")
        && onToast.includes("3 месяца"), onToast);
  check("«Чаты»: владельцу ушло сообщение в бот о включении",
        seen.told.some((t) => t.startsWith("Из приложения включено чтение чата «Иван Петров» (глубина: 3 месяца).")),
        JSON.stringify(seen.told));
  check("«Чаты»: запись в журнале «из приложения», без названия",
        seen.audit.some((a) => a.action === "app.sync_on" && a.outcome === "ok" && a.detail.includes("глубина: 3 месяца"))
        && !JSON.stringify(seen.audit).includes("Иван"), JSON.stringify(seen.audit));
  check("«Чаты»: счётчик выбранных обновился", (await p.textContent("#chats-counter")).includes("Выбрано для чтения: 3 чата"));
  await p.screenshot({ path: path.join(OUT, `${SHOT}chats-enabled-390-light.png`), fullPage: false });

  // вид чатов и «только выбранные»
  await p.fill("#chats-q", "");
  await p.selectOption("#chats-kind", "group");
  await p.waitForFunction(() => [...document.querySelectorAll("#chats-list .chat-meta")].every((n) => n.textContent.startsWith("группа")));
  rows = await rowsOf(p);
  check("«Чаты»: отбор по виду — только группы", rows.length > 0 && rows.every((r) => r.meta.startsWith("группа")));
  await p.selectOption("#chats-kind", "");
  await p.check("#chats-only");
  await p.waitForFunction(() => document.querySelectorAll("#chats-list li.chat").length === 3);
  rows = await rowsOf(p);
  check("«Чаты»: «только выбранные» — три включённых", rows.length === 3 && rows.every((r) => r.on), JSON.stringify(rows));
  await p.screenshot({ path: path.join(OUT, `${SHOT}chats-only-390-light.png`), fullPage: true });
  await p.uncheck("#chats-only");

  // «Не сохранять» — с подтверждением, без стирания
  await p.fill("#chats-q", "ivan_p");
  await p.waitForFunction(() => document.querySelectorAll("#chats-list li.chat").length === 1);
  const messagesBefore = (await chatsSeen()).messages;
  await p.click("#chats-list li.chat .link");
  await p.waitForSelector("#chats-list .confirm");
  await p.screenshot({ path: path.join(OUT, `${SHOT}chats-exclude-confirm-390-light.png`), fullPage: false });
  await p.click("#chats-list .confirm button.danger");
  await p.waitForSelector("#chats-list li.chat.is-excluded");
  await waitFor(async () => (await chatsSeen()).told.some((t) => t.includes("исключён из архива")));
  seen = await chatsSeen();
  check("«Чаты»: «Не сохранять» исключает чат, сохранённое остаётся",
        seen.excluded.includes("Иван Петров") && seen.messages === messagesBefore && !seen.enabled.includes("Иван Петров"),
        `${seen.messages} / ${messagesBefore}`);
  check("«Чаты»: владельцу ушло сообщение об исключении",
        seen.told.some((t) => t.startsWith("Из приложения чат «Иван Петров» исключён из архива")));
  rows = await rowsOf(p);
  check("«Чаты»: исключённый чат не включить — переключатель недоступен", rows[0].disabled && rows[0].meta.includes("не сохраняется"));
  await p.click("#chats-list li.chat .link");             // «Вернуть в архив»
  await p.waitForFunction(() => !document.querySelector("#chats-list li.chat.is-excluded"));
  await waitFor(async () => (await chatsSeen()).told.some((t) => t.includes("возвращён в архив")));
  seen = await chatsSeen();
  check("«Чаты»: «Вернуть в архив» и сообщение об этом", !seen.excluded.includes("Иван Петров")
        && seen.told.some((t) => t.includes("возвращён в архив")));
  await p.fill("#chats-q", "");
  await p.waitForFunction(() => document.querySelectorAll("#chats-list li.chat").length === 20);

  // списка в базе нет — подсказка нажать «Обновить»
  await chatsControl("/chats/forget?role=owner");
  await p.fill("#chats-q", "а");
  await p.waitForSelector("#chats-load.idle:not([hidden])");
  const notLoaded = await p.textContent("#chats-load");
  check("«Чаты»: списка в базе нет — приложение просит нажать «Обновить список из Telegram»",
        notLoaded.includes("ещё не загружен") && notLoaded.includes("Обновить список из Telegram") && (await rowsOf(p)).length === 0,
        notLoaded);
  await p.fill("#chats-q", "");
  await p.waitForTimeout(600);                             // поиск перечитывает список через 0,3 с после ввода
  await p.waitForSelector("#chats-load.idle:not([hidden])");
  await p.screenshot({ path: path.join(OUT, `${SHOT}chats-not-loaded-390-light.png`), fullPage: false });

  // большой аккаунт (Р-81): ещё 4 000 диалогов, страница «думает» 0,6 с, на 25-м запросе — «подождите 25 с»
  const pagesBefore = await (await fetch(CONTROL + "/chats/pages?role=owner")).json();
  const big = await chatsControl("/chats/big?role=owner&n=4000&delay=0.6&flood_at=25&seconds=25");
  const total = big.dialogs;                               // сколько диалогов у аккаунта теперь
  const pages = Math.ceil(total / 100) + (total % 100 ? 0 : 1);   // при ровной сотне — ещё пустая страница
  const clicked = Date.now();
  await p.click("#chats-refresh");
  await p.waitForFunction(() => !document.querySelector("#chats-load").hidden &&
    document.querySelector("#chats-load-title").textContent.includes("Загружаю"));
  check("«Чаты»: «Обновить» отвечает сразу, список грузится в фоне", Date.now() - clicked < 3000, `${Date.now() - clicked} мс`);
  await p.waitForFunction((n) => {
    const words = document.querySelector("#chats-load-text").textContent.replace(/\s/g, "");
    return /Загружено[1-9]/.test(words) && words.includes("из~" + n);
  }, total, { timeout: 15000 });
  await p.waitForFunction(() => document.querySelectorAll("#chats-list li.chat").length === 20, null, { timeout: 10000 });
  check(`«Чаты»: пока список грузится — «Загружено N из ~${total}», уже загруженные видны, «Обновить» недоступна`,
        await p.isDisabled("#chats-refresh") && (await rowsOf(p)).length === 20, await p.textContent("#chats-load"));
  await p.waitForTimeout(700);
  await p.locator("#screen-chats").screenshot({ path: path.join(LOAD_OUT, `${SHOT}app-dialogs-loading-390-light.png`) });
  // тот же экран в тёмной теме — второе окно приложения видит ту же загрузку
  const dark = await openChats(390, "dark", THEME);
  await dark.p.waitForFunction(() => !document.querySelector("#chats-load").hidden &&
    document.querySelector("#chats-load-title").textContent.includes("Загружаю"), null, { timeout: 10000 }).catch(() => {});
  check("«Чаты»: второе окно видит ту же загрузку и вторую не начинает",
        (await dark.p.textContent("#chats-load-title")).includes("Загружаю"), await dark.p.textContent("#chats-load"));
  await dark.p.waitForTimeout(700);
  await dark.p.locator("#screen-chats").screenshot({ path: path.join(LOAD_OUT, `${SHOT}app-dialogs-loading-390-dark.png`) });

  await p.waitForFunction(() => document.querySelector("#chats-load-title").textContent.includes("Telegram просит подождать"),
                          null, { timeout: 30000 });
  const waiting = await p.textContent("#chats-load-text");
  await p.waitForTimeout(1600);
  const later = await p.textContent("#chats-load-text");
  const left = (t) => Number((t.match(/через (\d+) с/) || [])[1]);
  check("«Чаты»: Telegram просит подождать — отсчёт идёт, продолжит сам с того же места",
        left(later) < left(waiting) && waiting.includes("с того же места") && await p.isDisabled("#chats-refresh"),
        `${waiting} → ${later}`);
  await p.locator("#screen-chats").screenshot({ path: path.join(LOAD_OUT, `${SHOT}app-dialogs-waiting-390-light.png`) });
  await dark.p.waitForFunction(() => document.querySelector("#chats-load-title").textContent.includes("Telegram просит подождать"),
                               null, { timeout: 10000 }).catch(() => {});
  await dark.p.locator("#screen-chats").screenshot({ path: path.join(LOAD_OUT, `${SHOT}app-dialogs-waiting-390-dark.png`) });
  const darkLayout = await layoutOk(dark.p);
  check("«Чаты» 390 тёмная, во время загрузки: страница не шире экрана, ничего не обрезано",
        darkLayout.wide && darkLayout.clipped === 0 && darkLayout.controls === 0, JSON.stringify(darkLayout));
  check("«Чаты», второе окно: в консоли нет ошибок", realErrors(dark.errs).length === 0, realErrors(dark.errs).join(" | "));
  await dark.ctx.close();

  await p.waitForFunction(() => document.querySelector("#chats-load").hidden, null, { timeout: 90000 });
  await p.waitForFunction((n) => document.querySelector("#chats-counter").textContent.replace(/\s/g, "").includes(`из${n}.`), total,
                          { timeout: 10000 });
  const pagesAfter = await (await fetch(CONTROL + "/chats/pages?role=owner")).json();
  const served = pagesAfter.pages.slice(pagesBefore.pages.length);
  check("«Чаты»: после ожидания загрузка продолжилась с той же страницы — каждая страница по одному разу",
        served.length === pages && new Set(served).size === pages && pagesAfter.calls - pagesBefore.calls === pages + 1,
        JSON.stringify({ pages: served.length, calls: pagesAfter.calls - pagesBefore.calls }));
  await p.locator("#screen-chats").screenshot({ path: path.join(LOAD_OUT, `${SHOT}app-dialogs-done-390-light.png`) });
  seen = await chatsSeen();

  // аккаунт помощника на паузе — «не подключён»
  await chatsControl("/chats/pause?role=assistant");
  await p.click("#chats-accounts button:nth-child(2)");
  await p.waitForSelector("#chats-offline:not([hidden])").catch(() => {});
  check("«Чаты»: аккаунт на паузе — «сейчас не подключён»", await p.isVisible("#chats-offline")
        && (await p.textContent("#chats-offline")).includes("странице «Переписка»"));
  await p.screenshot({ path: path.join(OUT, `${SHOT}chats-offline-390-light.png`), fullPage: false });
  await chatsControl("/chats/resume?role=assistant");
  await p.click("#chats-offline-retry");
  await p.waitForSelector("#chats-body:not([hidden])");
  check("«Чаты»: после «Проверить ещё раз» — список помощника", await p.isVisible("#chats-body"));

  check("«Чаты»: в консоли нет ошибок", realErrors(errs).length === 0, realErrors(errs).join(" | "));
  const layout = await layoutOk(p);
  check("«Чаты» 390: страница не шире экрана, ничего не обрезано",
        layout.wide && layout.clipped === 0 && layout.controls === 0, JSON.stringify(layout));
  await ctx.close();
}

// нет аккаунта и сбой сервера — подставным ответом в браузере
for (const [name, status, body] of [["none", 200, { accounts: [], mine: null }], ["failed", 502, null]]) {
  const ctx = await browser.newContext({ ...phone, viewport: { width: 360, height: 700 }, colorScheme: "dark" });
  const p = await ctx.newPage();
  await p.route("**/api/app/chats", (route) => body
    ? route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) })
    : route.fulfill({ status, contentType: "text/html", body: "<html>502 Bad Gateway</html>" }));
  await p.goto(launchUrl(await control("/init-data?who=owner")));
  await p.waitForSelector("#screen-approvals:not([hidden])");
  await p.click("#tab-chats");
  await p.waitForSelector(`#chats-${name}:not([hidden])`).catch(() => {});
  const text = await p.textContent(`#chats-${name}`);
  check(name === "none" ? "«Чаты»: аккаунта нет — «войдите на странице «Переписка»»"
                        : "«Чаты»: сбой сервера — понятный текст и «Повторить»",
        await p.isVisible(`#chats-${name}`) && (name === "none" ? text.includes("странице «Переписка»") : text.includes("Повторить")),
        text.slice(0, 120));
  await p.screenshot({ path: path.join(OUT, `${SHOT}chats-${name}-360-dark.png`), fullPage: true });
  await ctx.close();
}

// вид на двух размерах телефона в обеих темах
for (const [width, scheme, theme] of [[360, "light", THEME_LIGHT], [360, "dark", THEME], [390, "dark", THEME]]) {
  const { ctx, p, errs } = await openChats(width, scheme, theme);
  const tag = `${width}-${scheme}`;
  const layout = await layoutOk(p);
  check(`«Чаты» ${tag}: страница не шире экрана, ничего не обрезано`,
        layout.wide && layout.clipped === 0 && layout.controls === 0, JSON.stringify(layout));
  check(`«Чаты» ${tag}: в консоли нет ошибок`, realErrors(errs).length === 0, realErrors(errs).join(" | "));
  await p.screenshot({ path: path.join(OUT, `${SHOT}chats-list-${tag}.png`), fullPage: false });
  if (width === 360) {
    await p.fill("#chats-q", "Береговой");
    await p.waitForFunction(() => document.querySelectorAll("#chats-list li.chat").length === 1);
    await p.click("#chats-list li.chat .link");
    await p.waitForSelector("#chats-list .confirm");
    const narrow = await layoutOk(p);
    check(`«Чаты» ${tag}: длинное название и подтверждение помещаются`, narrow.wide && narrow.clipped === 0,
          JSON.stringify(narrow));
    await p.screenshot({ path: path.join(OUT, `${SHOT}chats-long-${tag}.png`), fullPage: false });
  }
  await ctx.close();
}

// --- 9. раскладки и сферы (Р-82) ---------------------------------------------------------------------
// Папки Telegram у подставного Telegram, тысячи диалогов (после блока большого аккаунта), три сферы.
// В приложении: раскладки со счётчиками, «Включить все» нет, «Выключить все» — с подтверждением и
// сообщением владельцу в бот; сферы чата из строки; вкладка «Сферы»: завести, связать папку, новый
// чат папки предлагается и принимается. Снимки — 360 и 390 пикселей, светлая и тёмная тема.
{
  const SPH_OUT = process.env.E2E_SPHERES_OUT || OUT;
  mkdirSync(SPH_OUT, { recursive: true });
  const spheresSeen = async () => (await fetch(CONTROL + "/spheres/seen")).json();
  const seeded = await chatsControl("/spheres/seed?role=owner");
  check("сферы: стенд завёл папки и сферы, список перечитан", seeded.folders === 4 && seeded.dialogs > 1000, JSON.stringify(seeded));
  const PREFIX = { folders: "folder:", spheres: "sphere:", activity: "activity:", kind: "kind:" };
  async function layout(p, value) {
    await p.selectOption("#chats-layout", value);
    await p.waitForSelector(`#chats-sections details.sect[data-section^="${PREFIX[value]}"]`, { timeout: 15000 });
  }
  async function openSection(p, title) {
    const det = p.locator("#chats-sections details.sect", { has: p.locator(".sect-title", { hasText: title }) }).first();
    await det.locator("summary").click();
    await det.locator(".chats li.chat").first().waitFor({ timeout: 15000 });
    return det;
  }
  async function wideOk(p) {
    return p.evaluate(() => {
      const wide = document.documentElement.scrollWidth <= window.innerWidth + 1;
      const out = [...document.querySelectorAll("main button, main select, main input")]
        .filter((x) => x.offsetParent !== null && x.getBoundingClientRect().right > window.innerWidth + 1).length;
      return { wide, out };
    });
  }

  const { ctx, p, errs } = await openChats(390, "light", THEME_LIGHT);
  const options = await p.$$eval("#chats-layout option", (o) => o.map((x) => x.textContent));
  check("приложение: раскладки «Все чаты / Папки Telegram / Сферы / По активности / По виду»",
        options.join("|") === "Все чаты|Папки Telegram|Сферы|По активности|По виду", options.join("|"));
  await layout(p, "folders");
  const folders = await p.$$eval("#chats-sections .sect-title", (n) => n.map((x) => x.textContent));
  check("приложение, «Папки»: папки Telegram и «Не в папках»",
        folders.join("|") === "💼 Работа|🏗 Объекты|📰 Новости|🏠 Семья|Не в папках", folders.join("|"));
  const work = await openSection(p, "Работа");
  check("приложение: «Включить все» нет — только на странице", (await work.locator("button", { hasText: "Включить все" }).count()) === 0);
  // «Стройка: чат» из папки уже читается (стенд) — «Выключить все» есть
  const readBefore = Number((/читается (\d+)/.exec(await work.locator(".sect-count").textContent()) || [])[1]);
  await work.locator("button", { hasText: "Выключить все" }).click();
  await work.locator(".confirm button", { hasText: "Выключить все" }).click();
  const off = await waitFor(async () => (await spheresSeen()).told.some((t) =>
    t.includes(`выключено чтение всех чатов раздела «Работа»: ${readBefore}.`)), 10000) && readBefore > 0 &&
    await waitFor(async () => /читается 0 из/.test(await work.locator(".sect-count").textContent()), 10000);
  check("приложение: «Выключить все» — с подтверждением, владельцу сообщение в бот, в журнале «из приложения»",
        off && (await spheresSeen()).audit.some((r) => r.action === "app.bulk_off"),
        JSON.stringify((await spheresSeen()).told.slice(-1)));
  await p.screenshot({ path: path.join(SPH_OUT, "app-layout-folders-390-light.png"), fullPage: false });

  await layout(p, "spheres");
  const sphereSections = await p.$$eval("#chats-sections .sect-title", (n) => n.map((x) => x.textContent));
  check("приложение, «Сферы»: сферы и «Без сферы»", sphereSections.join("|") === "Работа РАЗУМ|Личное|Преподавание|Без сферы",
        sphereSections.join("|"));
  const workSphere = await openSection(p, "Работа РАЗУМ");
  const ivan = workSphere.locator("li.chat", { hasText: "Иван Петров" }).first();
  check("приложение: в строке чата — все его сферы", (await ivan.locator(".chip").allTextContents()).join("|") === "Работа РАЗУМ|Личное");
  await ivan.locator("button.link", { hasText: "Сферы" }).click();
  await ivan.locator(".pick label", { hasText: "Преподавание" }).locator("input").check();
  await p.screenshot({ path: path.join(SPH_OUT, "app-chat-spheres-picker-390-light.png"), fullPage: false });
  await ivan.locator(".pick button", { hasText: "Сохранить" }).click();
  check("приложение: сферы чата сохранены", await waitFor(async () =>
    (await spheresSeen()).audit.some((r) => r.action === "app.sphere_chats"), 10000));
  await layout(p, "activity");
  const activity = await p.$$eval("#chats-sections .sect-title", (n) => n.map((x) => x.textContent));
  check("приложение, «По активности»", activity.join("|") === "За неделю|За месяц|За 3 месяца|Давно молчат", activity.join("|"));
  check("приложение, раскладки: в консоли нет ошибок", realErrors(errs).length === 0, realErrors(errs).join(" | "));
  await ctx.close();

  // вкладка «Сферы»
  {
    const { ctx, p, errs } = await openApp(390, "light", THEME_LIGHT);
    await p.click("#tab-spheres");
    await p.waitForSelector("#sph-list li.sph");
    const names = await p.$$eval("#sph-list .sph-name", (n) => n.map((x) => x.textContent));
    check("приложение, «Сферы»: сферы по порядку", names.join("|") === "Работа РАЗУМ|Личное|Преподавание", names.join("|"));
    await p.fill("#sph-new-title", "Учёба");
    await p.fill("#sph-new-description", "курсы и книги");
    await p.click("#sph-new-save");
    await p.waitForFunction(() => document.querySelector("#sph-list").textContent.includes("Учёба"));
    const row = p.locator("#sph-folders li", { hasText: "Работа" }).first();
    await row.locator("select").selectOption({ label: "Работа РАЗУМ" });
    await row.locator("button", { hasText: "Предлагать новые" }).click();
    await waitFor(async () => (await p.locator("#sph-offers .offer").count()) > 0, 10000);
    await chatsControl("/spheres/folder-new?role=owner");
    await p.click("#tab-chats"); await p.click("#tab-spheres");
    await p.waitForFunction(() => document.querySelector("#sph-offers").textContent.includes("Новый Заказчик"), null, { timeout: 15000 });
    const before = (await spheresSeen()).spheres.find((s) => s.title === "Работа РАЗУМ").chats;
    const offered = Number((/Новые чаты в папке «Работа»: (\d+)/.exec(await p.textContent("#sph-offers")) || [])[1]);
    await p.screenshot({ path: path.join(SPH_OUT, "app-spheres-390-light.png"), fullPage: true });
    await p.locator("#sph-offers button", { hasText: "Добавить" }).first().click();
    const added = await waitFor(async () => (await spheresSeen()).spheres.find((s) => s.title === "Работа РАЗУМ").chats === before + offered, 10000);
    check("приложение: связанная папка предложила новый чат, принят — в сфере", offered > 0 && added, `${before} + ${offered}`);
    const audit = (await spheresSeen()).audit;
    check("приложение: изменения сфер — в журнале «из приложения», без названий",
          ["app.sphere_create", "app.sphere_folder", "app.sphere_chats"].every((a) => audit.some((r) => r.action === a)) &&
          !JSON.stringify(audit).includes("Учёба") && !JSON.stringify(audit).includes("Иван"), audit.map((r) => r.action).join(","));
    check("приложение, «Сферы»: в консоли нет ошибок", realErrors(errs).length === 0, realErrors(errs).join(" | "));
    await ctx.close();
  }

  // вид на двух размерах телефона в обеих темах
  for (const [width, scheme, theme] of [[360, "light", THEME_LIGHT], [360, "dark", THEME], [390, "dark", THEME]]) {
    const tag = `${width}-${scheme}`;
    const opened = await openChats(width, scheme, theme);
    await layout(opened.p, "folders");
    await openSection(opened.p, "Работа");
    let ok = await wideOk(opened.p);
    check(`приложение, «Папки» ${tag}: страница не шире экрана, кнопки не выходят за край`, ok.wide && ok.out === 0, JSON.stringify(ok));
    await opened.p.screenshot({ path: path.join(SPH_OUT, `app-layout-folders-${tag}.png`), fullPage: false });
    await layout(opened.p, "activity");
    await openSection(opened.p, "За неделю");
    await opened.p.screenshot({ path: path.join(SPH_OUT, `app-layout-activity-${tag}.png`), fullPage: false });
    await opened.p.click("#tab-spheres");
    await opened.p.waitForSelector("#sph-list li.sph");
    await opened.p.locator("#sph-list li.sph").first().locator("button", { hasText: "Чаты" }).click();
    await opened.p.waitForSelector("#sph-list .sph-chats li");
    ok = await wideOk(opened.p);
    check(`приложение, «Сферы» ${tag}: страница не шире экрана, кнопки не выходят за край`, ok.wide && ok.out === 0, JSON.stringify(ok));
    await opened.p.screenshot({ path: path.join(SPH_OUT, `app-spheres-${tag}.png`), fullPage: true });
    await opened.ctx.close();
  }
}

// --- 10. «Уведомления» (Р-84) ----------------------------------------------------------------------
// Смотреть и сужать: выключить личные, обращения в группах, текст; время и тихие часы. Включить больше —
// только на странице: сервер отвергает расширение из приложения. Каждое изменение — сообщение в бот.
{
  const NOTIFY_OUT = process.env.E2E_NOTIFY_OUT || OUT;
  mkdirSync(NOTIFY_OUT, { recursive: true });
  const seen = async () => (await fetch(CONTROL + "/notify/seen")).json();
  const { ctx, p, errs } = await openApp(390, "light", THEME_LIGHT);
  await p.click("#tab-notify");
  await p.waitForSelector("#nt-body:not([hidden]) #nt-now li");
  const now = await p.$$eval("#nt-now li", (n) => n.map((x) => x.textContent));
  check("приложение, «Уведомления»: по умолчанию — личные от всех, обращения в группах, тихие часы 23:00–08:00",
        now[0] === "личные сообщения: от всех" && now.includes("прямые обращения в группах: да") &&
        now.includes("тихие часы: 23:00–08:00"), now.join(" | "));
  const offs = await p.$$eval("#nt-off button", (n) => n.map((x) => x.textContent));
  check("приложение, «Уведомления»: только кнопки «выключить» — включить отсюда нечем",
        offs.length === 3 && offs.every((t) => t.startsWith("Не ")), offs.join(" | "));
  await p.screenshot({ path: path.join(NOTIFY_OUT, "app-notify-390-light.png"), fullPage: true });
  await p.locator("#nt-off button", { hasText: "Не показывать текст сообщений" }).click();
  await p.waitForFunction(() => [...document.querySelectorAll("#nt-now li")].some((x) => x.textContent.includes("не показывать")));
  await p.selectOption("#nt-when", "d60");
  await p.fill("#nt-from", "22:30");
  await p.click("#nt-save");
  // сообщение владельцу уходит очередью заданий — ждём и его
  const saved = await waitFor(async () => { const x = await seen(); const s = x.settings;
    return s && s.mode === "digest" && s.quiet_from === "22:30" && x.told.length >= 2; }, 10000);
  const state = await seen();
  check("приложение, «Уведомления»: сужение и время сохранены, владельцу — сообщение в бот, в журнале «из приложения»",
        saved && state.settings.preview === false && state.told.length >= 2 &&
        state.audit.filter((r) => r.outcome === "ok").length >= 2, JSON.stringify(state));
  const refused = await p.evaluate(async () => {
    const key = sessionStorage.getItem("shturman-app-session");
    const res = await fetch("../api/app/notify", { method: "PUT", credentials: "omit",
      headers: { "Content-Type": "application/json", "X-Shturman-Setup": "1", "X-Shturman-App": key },
      body: JSON.stringify({ preview: true }) });
    return { status: res.status, body: await res.json() };
  });
  check("приложение: расширение уведомлений сервер отвергает (только на странице)",
        refused.status === 400 && refused.body.code === "page_only" && (await seen()).settings.preview === false, JSON.stringify(refused));
  check("приложение, «Уведомления»: в консоли нет ошибок", realErrors(errs).length === 0, realErrors(errs).join(" | "));
  await ctx.close();
  for (const [width, scheme, theme] of [[360, "light", THEME_LIGHT], [360, "dark", THEME], [390, "dark", THEME]]) {
    const tag = `${width}-${scheme}`;
    const opened = await openApp(width, scheme, theme);
    await opened.p.click("#tab-notify");
    await opened.p.waitForSelector("#nt-body:not([hidden]) #nt-now li");
    const wide = await opened.p.evaluate(() => {
      const out = [...document.querySelectorAll("main button, main select, main input, .tabs button")]
        .filter((x) => x.offsetParent !== null && x.getBoundingClientRect().right > window.innerWidth + 1).length;
      return { wide: document.documentElement.scrollWidth <= window.innerWidth + 1, out };
    });
    check(`приложение, «Уведомления» ${tag}: страница не шире экрана, кнопки и вкладки не выходят за край`,
          wide.wide && wide.out === 0, JSON.stringify(wide));
    await opened.p.screenshot({ path: path.join(NOTIFY_OUT, `app-notify-${tag}.png`), fullPage: true });
    await opened.ctx.close();
  }
}

await browser.close();
console.log(failed ? `ИТОГ: ${failed} FAIL` : "ИТОГ: OK");
process.exit(failed ? 1 : 0);
