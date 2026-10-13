// Браузерная проверка Mini App бота согласований: вход по данным Telegram → «Согласования» →
// решение → пункт ушёл; чужой пользователь, устаревшие и повторённые данные входа не проходят;
// сессия приложения не открывает страницу настройки; рамка — только у web.telegram.org.
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

await browser.close();
console.log(failed ? `ИТОГ: ${failed} FAIL` : "ИТОГ: OK");
process.exit(failed ? 1 : 0);
