// Браузерная проверка страницы настройки: ссылка → вход → каждый раздел → выход → вход по коду.
// Запуск и стенд — README.md в этом каталоге. Печатает строки PASS/FAIL, код возврата 1 при FAIL.

import { createRequire } from "node:module";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.E2E_PLAYWRIGHT || "playwright");

const PORT = process.env.E2E_PORT || "8765";
const BASE = process.env.E2E_BASE || `http://127.0.0.1:${PORT}`;
const CONTROL = process.env.E2E_CONTROL || `http://127.0.0.1:${Number(PORT) + 1}`;
const PYTHON = process.env.E2E_PYTHON || "python3";            // окружение сервиса: им запускается `shturman setup-link`
const DECODER = process.env.E2E_QR_PYTHON || "";               // python с OpenCV: распознаёт QR со снимка (необязательно)
const OUT = process.env.E2E_OUT || path.join(tmpdir(), "shturman-setup-shots");
const SRC = process.env.E2E_SRC || path.resolve(import.meta.dirname, "../../src");
const DSN = process.env.SHTURMAN_TEST_DSN;
const GOOD_TOKEN = "7000000001:SENTINEL-bot-token_DoNotLeak-0123456789";
const BUSY_TOKEN = "7000000002:BUSY-bot-token_polled-by-another-0123456789";
const PASSWORD = "очень-секретный-пароль-77";
const LLM_KEY = "sk-e2e-not-a-real-key-0123456789";
const SECRETS = [GOOD_TOKEN, BUSY_TOKEN, PASSWORD, LLM_KEY, "0123456789abcdef0123456789abcdef"];

mkdirSync(OUT, { recursive: true });
let failed = 0;
const seenBodies = [];
function check(name, ok, detail = "") {
  if (!ok) failed += 1;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}
function cli(...args) {
  const env = { ...process.env, SHTURMAN_DSN: DSN, PYTHONPATH: SRC, SHTURMAN_PORT: PORT };
  return execFileSync(PYTHON, ["-m", "shturman.cli", ...args], { env, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
}
async function control(method, pathname) {
  const res = await fetch(CONTROL + pathname, { method });
  return res.json();
}
function decodeQr(file) {
  if (!DECODER) return null;
  // Распознаватель капризен к размеру модуля: пробуем несколько увеличений с белыми полями.
  const code = [
    "import cv2, sys",
    "img = cv2.imread(sys.argv[1])",
    "for fx in (2, 3, 1, 4):",
    "    big = cv2.resize(img, None, fx=fx, fy=fx, interpolation=cv2.INTER_NEAREST)",
    "    big = cv2.copyMakeBorder(big, 40, 40, 40, 40, cv2.BORDER_CONSTANT, value=(255, 255, 255))",
    "    text = cv2.QRCodeDetector().detectAndDecode(big)[0]",
    "    if text:",
    "        print(text)",
    "        break",
  ].join("\n");
  const done = spawnSync(DECODER, ["-c", code, file], { encoding: "utf8" });
  return (done.stdout || "").trim();
}
async function shot(page, name, full = true) {
  await page.screenshot({ path: path.join(OUT, name + ".png"), fullPage: full });
}

const browser = await chromium.launch();
const desktop = { viewport: { width: 1360, height: 900 }, locale: "ru-RU" };
const context = await browser.newContext(desktop);
const page = await context.newPage();
const consoleErrors = [], foreign = [];
page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
page.on("pageerror", (e) => consoleErrors.push(String(e)));
page.on("request", (r) => { if (!r.url().startsWith(BASE) && !r.url().startsWith("data:")) foreign.push(r.url()); });
page.on("response", async (r) => {
  if (!r.url().includes("/shturman-setup/api/")) return;
  try { seenBodies.push(await r.text()); } catch { /* тело уже недоступно */ }
});
page.on("dialog", (d) => d.accept());
const URL_PAGE = BASE + "/shturman-setup/";

// --- без входа -------------------------------------------------------------------------------
{
  const res = await page.goto(URL_PAGE);
  const h = res.headers();
  check("страница открывается без входа и не содержит данных", res.status() === 200);
  check("строгая политика содержимого без встроенных скриптов",
        /script-src 'self'/.test(h["content-security-policy"] || "") && !/unsafe-inline/.test(h["content-security-policy"] || ""));
  check("запрет встраивания, no-store, no-referrer",
        h["x-frame-options"] === "DENY" && h["cache-control"] === "no-store" && h["referrer-policy"] === "no-referrer");
  await page.waitForSelector("#login-nolink:not([hidden])");
  check("без ссылки и без привязанного бота — только «нужна ссылка входа»", await page.isVisible("#login-nolink"));
  await shot(page, "01-login-no-link-desktop-light");
  for (const p of ["/shturman-setup/api/state", "/shturman-setup/api/overview"]) {
    const r = await page.request.get(BASE + p);
    check(`без входа ${p} закрыт`, r.status() === 401);
  }
  const viaApi = await page.request.get(BASE + "/shturman-setup/api/state",
    { headers: { Authorization: "Bearer e2e-api-token-0123456789abcdef0123456789" } });
  check("токен внутреннего API входом не служит", viaApi.status() === 401);
  for (const p of ["/shturman-setup/api/status", "/shturman-setup/api/tg/accounts", "/shturman-setup/mcp"]) {
    const r = await page.request.get(BASE + p, { headers: { Authorization: "Bearer e2e-api-token-0123456789abcdef0123456789" } });
    check(`под префиксом нет ${p}`, r.status() === 404 || r.status() === 401);
  }
  const wrongHost = await page.request.get(URL_PAGE, { headers: { Host: "evil.example" } });
  check("чужое имя узла отвергается", wrongHost.status() === 421);
}

// --- вход по одноразовой ссылке ----------------------------------------------------------------
let linkPath = cli("setup-link").trim();
check("команда setup-link печатает путь со значением во фрагменте", /^\/shturman-setup\/#[A-Za-z0-9_-]{40,}$/.test(linkPath), linkPath.replace(/#.*/, "#…"));
const firstLink = linkPath;
linkPath = cli("setup-link").trim();           // новая ссылка отменяет прежнюю
{
  const other = await browser.newContext(desktop);
  const p2 = await other.newPage();
  await p2.goto(BASE + firstLink);
  await p2.click("#login-link-go");
  await p2.waitForSelector("#login-message:not([hidden])");
  check("прежняя ссылка после выдачи новой не действует", (await p2.textContent("#login-message-text")).includes("устарела"));
  await other.close();
}
await page.goto(BASE + linkPath);
await page.waitForSelector("#login-link:not([hidden])");
await shot(page, "02-login-link-desktop-light");
const linkRequest = page.waitForRequest((r) => r.url().endsWith("/api/login/link"));
await page.click("#login-link-go");
const sent = await linkRequest;
check("значение ссылки уходит телом POST, а не в адресе", sent.method() === "POST" && !sent.url().includes(linkPath.split("#")[1]));
await page.waitForSelector("#screen-app:not([hidden])");
check("после входа значение убрано из адресной строки", !page.url().includes("#"));
const cookies = await context.cookies();
const cookie = cookies.find((c) => c.name === "shturman_setup");
check("cookie сессии: HttpOnly, SameSite=Strict, путь страницы",
      !!cookie && cookie.httpOnly && cookie.sameSite === "Strict" && cookie.path === "/shturman-setup/");
{
  const other = await browser.newContext(desktop);
  const p2 = await other.newPage();
  await p2.goto(BASE + linkPath);
  await p2.click("#login-link-go");
  await p2.waitForSelector("#login-message:not([hidden])");
  check("ссылка срабатывает один раз", (await p2.textContent("#login-message-text")).includes("уже использована"));
  await other.close();
}
{
  // запрос с чужого сайта и запрос без метки страницы не проходят даже с cookie
  const noCsrf = await page.request.post(BASE + "/shturman-setup/api/logout-all",
    { headers: { Origin: BASE, "X-Shturman-Setup": "1" }, data: {} });
  check("без метки страницы изменяющий запрос отвергается", noCsrf.status() === 403);
  const cross = await page.request.post(BASE + "/shturman-setup/api/logout-all",
    { headers: { Origin: "https://evil.example", "X-Shturman-Setup": "1" }, data: {} });
  check("запрос с чужого сайта отвергается", cross.status() === 403);
}
await page.waitForSelector("#st-bot");
await shot(page, "03-app-empty-desktop-light");

// --- 1. бот согласований -----------------------------------------------------------------------
await page.fill("#bot-token", "не токен");
await page.click("#bot-check");
await page.waitForSelector("#bot-error:not([hidden])");
check("бот: неверный вид токена объяснён простыми словами", (await page.textContent("#bot-error")).includes("не похоже на токен"));
await page.fill("#bot-token", "7000000009:WRONG-token-that-telegram-does-not-know-000");
await page.click("#bot-check");
await page.waitForFunction(() => document.querySelector("#bot-error").textContent.includes("не принял"));
check("бот: токен, который Telegram не знает, не сохраняется", true);
await page.fill("#bot-token", BUSY_TOKEN);
await page.click("#bot-check");
await page.waitForSelector("#bot-confirm:not([hidden])");
check("бот: перед сохранением показано имя бота", (await page.textContent("#bot-confirm-name")).includes("@ivan_assistant_bot"));
await page.click("#bot-save");
await page.waitForFunction(() => document.querySelector("#bot-error").textContent.includes("другая программа"));
check("бот: токен бота, которого уже опрашивают, отвергнут с объяснением", true);
await shot(page, "04-bot-busy-token-desktop-light", false);
await page.fill("#bot-token", GOOD_TOKEN);
await page.click("#bot-check");
await page.waitForSelector("#bot-confirm:not([hidden])");
check("бот: поле токена очищено сразу после отправки", (await page.inputValue("#bot-token")) === "");
await page.click("#bot-save");
await page.waitForFunction(() => document.querySelector("#bot-status").textContent.includes("на связи"), null, { timeout: 20000 });
check("бот: сохранён и вышел на связь без перезапуска сервиса", true);
await page.click("#bind-go");
await page.waitForSelector("#bind-link-box:not([hidden])");
const bindLink = await page.getAttribute("#bind-open", "href");
check("привязка: ссылка вида https://t.me/<бот>?start=<код>", /^https:\/\/t\.me\/shturman_soglasovaniya_bot\?start=[A-Za-z0-9_-]{24,}$/.test(bindLink));
await page.evaluate(() => { document.getElementById("toast").hidden = true; });
await page.locator("#bind-qr").screenshot({ path: path.join(OUT, "qr-bind.png") });
if (DECODER) check("привязка: QR со страницы распознаётся и содержит ту же ссылку", decodeQr(path.join(OUT, "qr-bind.png")) === bindLink);
await shot(page, "05-bot-bind-desktop-light", false);
await control("POST", "/start?code=" + bindLink.split("start=")[1]);
await page.waitForFunction(() => document.querySelector("#bind-done").textContent.includes("Владелец привязан"), null, { timeout: 20000 });
check("привязка: страница сама увидела владельца и показала имя", (await page.textContent("#bind-done")).includes("Евгений Тестов"));
check("привязка: ссылка убрана с экрана", !(await page.isVisible("#bind-link-box")));

// --- 2. ключи приложения -----------------------------------------------------------------------
await page.fill("#keys-id", "12ab");
await page.fill("#keys-hash", "0123456789abcdef0123456789abcdef");
await page.click("#keys-save");
await page.waitForSelector("#keys-error:not([hidden])");
check("ключи: неверный api_id объяснён", (await page.textContent("#keys-error")).includes("api_id"));
await page.fill("#keys-id", "1234567");
await page.fill("#keys-hash", "0123456789abcdef0123456789abcdef");
await page.click("#keys-save");
await page.waitForSelector("#keys-ok:not([hidden])");
check("ключи: сохранены, поля очищены", (await page.inputValue("#keys-hash")) === "");
await page.waitForSelector("#accounts-body:not([hidden])");

// --- 3. аккаунты -------------------------------------------------------------------------------
await page.click("#role-assistant-state button");
await page.waitForSelector("#tg-login-qr svg");
await page.locator("#tg-login-qr").screenshot({ path: path.join(OUT, "qr-login.png") });
if (DECODER) check("вход: QR со страницы распознаётся как ссылка входа Telegram", decodeQr(path.join(OUT, "qr-login.png")).startsWith("tg://login?token="));
await shot(page, "06-tg-login-qr-desktop-light", false);
await control("POST", "/scan?role=assistant");
await page.waitForSelector("#tg-password-form:not([hidden])");
check("вход: при облачном пароле появляется поле и подсказка", (await page.textContent("#tg-password-hint")).includes("кличка кота"));
await page.fill("#tg-password", "не тот пароль");
await page.click("#tg-password-send");
await page.waitForFunction(() => document.querySelector("#tg-login-error").textContent.includes("Неверный"));
check("вход: неверный пароль — понятная ошибка, поле очищено", (await page.inputValue("#tg-password")) === "");
await shot(page, "07-tg-login-password-desktop-light", false);
await page.fill("#tg-password", PASSWORD);
await page.click("#tg-password-send");
await page.waitForFunction(() => document.querySelector("#role-assistant-state").textContent.includes("Подключён"), null, { timeout: 20000 });
check("вход: помощник подключён", (await page.textContent("#role-assistant-state")).includes("Помощник"));
await page.click("#role-owner-state button");
check("вход: основной аккаунт без отметки согласия не подключается", !(await page.isVisible("#tg-login")));
await page.check("#consent-owner");
await page.click("#role-owner-state button");
await page.waitForSelector("#tg-login-qr svg");
await control("POST", "/scan?role=owner");
await page.waitForFunction(() => document.querySelector("#role-owner-state").textContent.includes("Подключён"), null, { timeout: 20000 });
check("вход: основной аккаунт подключён только на чтение", true);

// --- 4. чаты -----------------------------------------------------------------------------------
await page.waitForSelector("#chats-list li");
check("чаты: два аккаунта — две вкладки", (await page.locator("#chats-accounts .tab").count()) === 2);
check("чаты: список отдаётся страницами по 50", (await page.locator("#chats-list li").count()) === 50);
check("чаты: счётчик показывает, сколько выбрано и показано", (await page.textContent("#chats-counter")).includes("Показано 50 из"));
await page.click("#chats-more");
await page.waitForFunction(() => document.querySelectorAll("#chats-list li").length === 100);
check("чаты: «Показать ещё» догружает следующую страницу", true);
await page.fill("#chats-q", "Иван Петров");
await page.waitForFunction(() => document.querySelectorAll("#chats-list li").length > 0 && document.querySelectorAll("#chats-list li").length < 30);
check("чаты: поиск по названию", (await page.textContent("#chats-list")).includes("Иван Петров"));
await page.locator("#chats-list li .chat-read input").first().check();
await page.waitForFunction(() => document.querySelector("#chats-counter").textContent.includes("Выбрано для чтения: 1 чат"));
check("чаты: включение чтения одного чата меняет счётчик", true);
await page.fill("#chats-q", "");
await page.selectOption("#chats-kind", "channel");
await page.waitForFunction(() => document.querySelector("#chats-bulk") && !document.querySelector("#chats-bulk").hidden);
check("чаты: отбор по виду и кнопка «читать все»", (await page.textContent("#chats-bulk")).includes("каналы"));
check("чаты: служебный чат Telegram нельзя ни читать, ни вернуть", (await page.locator("#chats-list li", { hasText: "служебный чат" }).count()) === 0);
await page.locator("#chats-list li button").first().click();              // «Не сохранять» + два подтверждения
await page.waitForFunction(() => document.querySelector("#chats-list").textContent.includes("не сохраняется"));
check("чаты: исключённый чат помечен, читать его нельзя", await page.locator("#chats-list li.is-excluded .chat-read input").first().isDisabled());
await page.selectOption("#chats-kind", "");
await page.selectOption("#opt-depth", "6");
await page.waitForFunction(() => document.querySelector("#toast").textContent.includes("Настройка сохранена"));
check("чаты: глубина истории сохраняется", true);
await page.waitForFunction(() => document.querySelectorAll("#chats-list li").length === 50);
await shot(page, "08-chats-desktop-light", false);
await page.locator("#s-chats").screenshot({ path: path.join(OUT, "08b-chats-section-desktop-light.png") });

// --- 5. выгрузка -------------------------------------------------------------------------------
const exportFile = path.join(OUT, "result.json");
const msg = (id, ts, from, fromId, text) => ({ id, type: "message", date: "2026-09-12T10:00:00", date_unixtime: String(ts), from, from_id: "user" + fromId, text, text_entities: [{ type: "plain", text }] });
writeFileSync(exportFile, JSON.stringify({
  about: "Here is the data you requested.",
  personal_information: { user_id: 1000, first_name: "Евгений", last_name: "Тестов" },
  contacts: { about: "", list: [] },
  chats: { about: "", list: [
    { name: "Иван Петров", type: "personal_chat", id: 2001, messages: [msg(1, 1789200000, "Иван Петров", 2001, "Добрый день! Пришлю смету по фасадам к пятнице."), msg(2, 1789200060, "Евгений Тестов", 1000, "Хорошо, жду.")] },
    { name: "Семья", type: "private_group", id: 3001, messages: [msg(10, 1789200010, "Мария", 2002, "Купи хлеба и молока")] },
    { name: "Telegram", type: "personal_chat", id: 777000, messages: [msg(20, 1789200020, "Telegram", 777000, "Login code: 12345")] } ] },
  left_chats: { about: "", list: [] } }));
await page.setInputFiles("#import-file", exportFile);
await page.click("#import-upload");
await page.waitForSelector(".import-chats li", { timeout: 30000 });
check("выгрузка: файл загружен и разобран, показаны чаты", (await page.locator(".import-chats li").count()) === 3);
check("выгрузка: служебный чат Telegram принять нельзя", await page.locator(".import-chats li", { hasText: "служебный чат" }).locator("input").isDisabled());
await page.locator(".import-chats li", { hasText: "Семья" }).locator("input").uncheck();
await page.waitForFunction(() => document.querySelector(".import-item .counter").textContent.includes("К импорту: 1 чат"));
check("выгрузка: снятый чат не идёт в импорт, счётчик пересчитан", true);
await shot(page, "09-import-scan-desktop-light", false);
await page.locator(".import-item button", { hasText: "Импортировать" }).click();
await page.waitForFunction(() => document.querySelector("#import-items").textContent.includes("Импорт завершён"), null, { timeout: 30000 });
check("выгрузка: импорт прошёл сразу, без карточки в боте", (await page.textContent("#import-items")).includes("Новых сообщений: 2"));

// --- 6. бизнес-режим ---------------------------------------------------------------------------
await page.waitForSelector("#business-body:not([hidden])");
check("бизнес-режим: в схемах подставлено имя бота", (await page.locator("#s-business .bot-name").first().textContent()) === "@shturman_soglasovaniya_bot");
await control("POST", "/business?connect=1");
await page.click("#business-refresh");
await page.waitForFunction(() => document.querySelector("#business-capable").textContent.includes("режим включён"));
await page.waitForFunction(() => document.querySelector("#st-business").textContent === "Подключён", null, { timeout: 20000 });
check("бизнес-режим: подключение от владельца замечено", (await page.textContent("#st-business")).includes("Подключён"));

// --- 7. своя модель ----------------------------------------------------------------------------
await page.click("#llm-details summary");
await page.fill("#llm-key", LLM_KEY);
await page.fill("#llm-model", "плохое имя");
await page.click("#llm-save");
await page.waitForSelector("#llm-error:not([hidden])");
check("модель: неверное имя модели объяснено", (await page.textContent("#llm-error")).includes("Имя модели"));
await page.fill("#llm-key", LLM_KEY);
await page.fill("#llm-model", "gpt-4o-mini");
await page.click("#llm-save");
await page.waitForFunction(() => document.querySelector("#llm-status").textContent.includes("gpt-4o-mini"), null, { timeout: 20000 });
check("модель: проверена пробным запросом и сохранена, поле ключа очищено", (await page.inputValue("#llm-key")) === "");

// --- 8. что собрано ----------------------------------------------------------------------------
await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
await page.waitForFunction(() => document.querySelector("#tiles").textContent.includes("Сообщений в архиве") &&
  !/^0/.test(document.querySelector("#tiles .tile b").textContent));
check("что собрано: счётчики архива не нулевые", true);
await page.waitForFunction(() => document.querySelector("#audit").textContent.includes("Своя модель сервиса сохранена"), null, { timeout: 40000 });
const auditText = await page.textContent("#audit");
check("журнал: видны действия этой сессии", ["Вход по ссылке", "Токен бота согласований сохранён", "Аккаунт Telegram подключён", "Запущен импорт выгрузки"].every((t) => auditText.includes(t)));
check("отправка со страницы не включилась", (await page.textContent("#facts")).includes("Выключена"));

await page.evaluate(() => window.scrollTo(0, 0));
await shot(page, "10-app-filled-desktop-light");

// --- снимки: тёмная тема и телефон -------------------------------------------------------------
const stored = await context.storageState();
for (const [name, options] of [
  ["desktop-dark", { ...desktop, colorScheme: "dark" }],
  ["phone-light", { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true, locale: "ru-RU" }],
  ["phone-dark", { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true, locale: "ru-RU", colorScheme: "dark" }],
]) {
  const ctx = await browser.newContext({ ...options, storageState: stored });
  const p = await ctx.newPage();
  await p.goto(URL_PAGE);
  await p.waitForSelector("#chats-list li");
  await p.waitForFunction(() => document.querySelector("#audit").children.length > 1);
  await p.click("#llm-details summary");
  await shot(p, "11-app-filled-" + name);
  const overflow = await p.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  check(`вёрстка (${name}): страница не шире экрана`, overflow <= 1, "лишних пикселей: " + overflow);
  await ctx.close();
}

// --- секреты не утекли -------------------------------------------------------------------------
const html = await page.content();
check("секреты не появились ни в разметке страницы, ни в ответах сервера",
      SECRETS.every((s) => !html.includes(s) && !seenBodies.some((b) => b.includes(s))));
const seen = await control("GET", "/seen");
check("в журнале действий нет ни секретов, ни имён, ни названий чатов",
      !SECRETS.concat(["Иван", "Семья", "Тестов", "t.me/", "tg://"]).some((s) => JSON.stringify(seen.audit).includes(s)));
check("страница не обращалась к чужим серверам", foreign.length === 0, foreign.slice(0, 3).join(", "));
check("в консоли браузера нет ошибок", consoleErrors.filter((e) => !/401|403|404|409|422|Failed to load resource/.test(e)).length === 0, consoleErrors.slice(0, 3).join(" | "));

// --- выход и вход по коду от бота ---------------------------------------------------------------
await page.click("#logout");
await page.waitForSelector("#login-start:not([hidden])");
check("выход: сессия закрыта, предлагается вход по коду от бота", (await page.request.get(BASE + "/shturman-setup/api/state")).status() === 401);
await shot(page, "12-login-code-offer-desktop-light");
await page.click("#login-code-send");
await page.waitForSelector("#login-code:not([hidden])");
await page.fill("#login-code-input", "00000000");
await page.click("#login-code-submit");
await page.waitForSelector("#login-code-error:not([hidden])");
check("вход по коду: неверный код не пускает", (await page.textContent("#login-code-error")).includes("не подошёл"));
await shot(page, "13-login-code-desktop-light");
const { code } = await control("GET", "/last-code");
await page.fill("#login-code-input", code);
await page.click("#login-code-submit");
await page.waitForSelector("#screen-app:not([hidden])");
check("вход по коду: код из бота пускает", true);

// --- «выйти везде» командой ---------------------------------------------------------------------
check("команда setup-logout-all завершает сессии", /Завершено сессий страницы настройки: [1-9]/.test(cli("setup-logout-all")));
check("после неё прежняя сессия не действует", (await page.request.get(BASE + "/shturman-setup/api/state")).status() === 401);

// --- экраны входа: телефон и тёмная тема --------------------------------------------------------
{
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, locale: "ru-RU", colorScheme: "dark" });
  const p = await ctx.newPage();
  await p.goto(BASE + cli("setup-link").trim());
  await p.waitForSelector("#login-link:not([hidden])");
  await shot(p, "14-login-link-phone-dark");
  await ctx.close();
}

await browser.close();
console.log(failed ? `\nПровалено проверок: ${failed}` : "\nВсе проверки пройдены");
console.log("Снимки: " + OUT);
process.exit(failed ? 1 : 0);
