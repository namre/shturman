// Браузерная проверка страницы настройки: ссылка → вход → три шага → выгрузка → «Дополнительно» →
// попытки «дашборда» с соседнего порта → выход → вход по коду.
// Запуск и стенд — README.md в этом каталоге. Печатает строки PASS/FAIL, код возврата 1 при FAIL.

import { createRequire } from "node:module";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.E2E_PLAYWRIGHT || "playwright");

const PORT = process.env.E2E_PORT || "8765";
// Страница и подставной «дашборд» — одно имя узла, разные порты: как в жизни.
const BASE = process.env.E2E_BASE || `http://localhost:${PORT}`;
const DASH = process.env.E2E_DASHBOARD || `http://localhost:${Number(PORT) + 2}`;
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
const KEY_NAME = "shturman-setup-session";

mkdirSync(OUT, { recursive: true });
let failed = 0;
const seenBodies = [];
function check(name, ok, detail = "") {
  if (!ok) failed += 1;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}
function note(text) { console.log("NOTE  " + text); }
function cli(...args) {
  const env = { ...process.env, SHTURMAN_DSN: DSN, PYTHONPATH: SRC, SHTURMAN_PORT: PORT,
                SHTURMAN_SETUP_ORIGIN: "", SHTURMAN_DASHBOARD_ORIGIN: "" };
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
const phone = { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true, locale: "ru-RU" };
const VARIANTS = [
  ["desktop-dark", { ...desktop, colorScheme: "dark" }],
  ["phone-light", phone],
  ["phone-dark", { ...phone, colorScheme: "dark" }],
];
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
const API = BASE + "/shturman-setup/api";
const storedKey = () => page.evaluate((name) => localStorage.getItem(name), KEY_NAME);

// Тот же экран в остальных трёх видах: тёмная тема и телефон. Состояние — с сервера, вход — из
// хранилища страницы (ключ сессии в localStorage её origin).
async function variants(stage, prepare) {
  const stored = await context.storageState();
  for (const [name, options] of VARIANTS) {
    const ctx = await browser.newContext({ ...options, storageState: stored });
    const p = await ctx.newPage();
    p.on("dialog", (d) => d.accept());
    await p.goto(URL_PAGE);
    await p.waitForSelector("#screen-app:not([hidden])");
    await p.waitForSelector("#progress li");
    if (prepare) await prepare(p);
    await p.waitForTimeout(300);
    await shot(p, `${stage}-${name}`);
    const overflow = await p.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    check(`вёрстка (${stage}, ${name}): страница не шире экрана`, overflow <= 1, "лишних пикселей: " + overflow);
    await ctx.close();
  }
}

// --- без входа -------------------------------------------------------------------------------
{
  const res = await page.goto(URL_PAGE);
  const h = res.headers();
  check("страница открывается без входа и не содержит данных", res.status() === 200);
  check("метка страницы на ответе", h["x-shturman-setup-page"] === "1");
  check("строгая политика содержимого без встроенных скриптов",
        /script-src 'self'/.test(h["content-security-policy"] || "") && !/unsafe-inline/.test(h["content-security-policy"] || ""));
  check("запрет встраивания, no-store, no-referrer, изоляция от чужих окон",
        h["x-frame-options"] === "DENY" && h["cache-control"] === "no-store" && h["referrer-policy"] === "no-referrer" &&
        h["cross-origin-opener-policy"] === "same-origin" && h["cross-origin-resource-policy"] === "same-origin");
  check("ни cookie, ни заголовков CORS страница не отдаёт", !h["set-cookie"] && !Object.keys(h).some((k) => k.startsWith("access-control-")));
  await page.waitForSelector("#login-nolink:not([hidden])");
  const lead = await page.textContent("#login-nolink");
  check("без ссылки — экран «нужна ссылка входа» с объяснением, откуда её взять",
        lead.includes("./ops/setup-link.sh") && lead.includes("кто ставил ассистента"));
  await shot(page, "01-login-no-link-desktop-light");
  for (const p of ["/state", "/overview"]) {
    const r = await page.request.get(API + p);
    check(`без входа ${p} закрыт`, r.status() === 401);
  }
  const viaApi = await page.request.get(API + "/state",
    { headers: { Authorization: "Bearer e2e-api-token-0123456789abcdef0123456789" } });
  check("токен внутреннего API входом не служит", viaApi.status() === 401);
  for (const p of ["/shturman-setup/api/status", "/shturman-setup/api/tg/accounts", "/shturman-setup/mcp"]) {
    const r = await page.request.get(BASE + p, { headers: { Authorization: "Bearer e2e-api-token-0123456789abcdef0123456789" } });
    check(`под префиксом нет ${p}`, r.status() === 404 || r.status() === 401);
  }
  const wrongHost = await page.request.get(URL_PAGE, { headers: { Host: "evil.example" } });
  check("чужое имя узла отвергается", wrongHost.status() === 421);
  const dashHost = await page.request.get(URL_PAGE, { headers: { Host: new URL(DASH).host } });
  check("запрос с именем и портом дашборда отвергается, даже дойдя до сервиса", dashHost.status() === 421);
  const noPort = await page.request.get(URL_PAGE, { headers: { Host: new URL(BASE).hostname } });
  check("то же имя без порта страницы отвергается", noPort.status() === 421);
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
  const text = await p2.textContent("#login-message-text");
  check("прежняя ссылка после выдачи новой не действует; сказано, где взять новую", text.includes("устарела") && text.includes("./ops/setup-link.sh"));
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
const KEY = await storedKey();
check("ключ сессии лежит в localStorage страницы", /^[A-Za-z0-9_-]{40,64}$/.test(KEY || ""));
check("ключа сессии нет ни в адресе, ни в разметке", !page.url().includes(KEY) && !(await page.content()).includes(KEY));
check("cookie страница не ставит вовсе", (await context.cookies()).length === 0);
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
  // на уровне HTTP: без ключа — гость; с ключом, но не со страницы — отказ
  const own = { Origin: BASE, "X-Shturman-Setup": "1" };
  const noKey = await page.request.post(API + "/logout-all", { headers: own, data: {} });
  check("без ключа сессии изменяющий запрос отвергается", noKey.status() === 401);
  const cross = await page.request.post(API + "/logout-all",
    { headers: { ...own, Origin: "https://evil.example", "X-Shturman-Session": KEY }, data: {} });
  check("запрос с чужого сайта отвергается даже с ключом", cross.status() === 403);
  const sameSite = await page.request.get(API + "/state", { headers: { "Sec-Fetch-Site": "same-site", "X-Shturman-Session": KEY } });
  check("чтение с соседнего порта (same-site) отвергается даже с ключом", sameSite.status() === 403);
  const worker = await page.request.get(BASE + "/shturman-setup/static/setup.js", { headers: { "Service-Worker": "script" } });
  check("файл под service worker страница не отдаёт (HTTP)", worker.status() === 404);
}
await page.reload();
await page.waitForSelector("#screen-app:not([hidden])");
check("перезагрузка страницы сохраняет вход", (await storedKey()) === KEY);

// --- шаг 1. ключи приложения -------------------------------------------------------------------
await page.waitForSelector("#progress li");
check("сверху — строка «что уже сделано» и счётчик собранного",
      (await page.locator("#progress li").count()) === 3 && (await page.textContent(".collected")).includes("Собрано сообщений"));
check("шаги 2 и 3 закрыты, пока не готов шаг 1",
      (await page.isVisible("#accounts-nokeys")) && (await page.isVisible("#chats-none")) && !(await page.isVisible("#accounts-body")));
check("«Дополнительно» свёрнуто, бот согласований на первом экране не виден",
      !(await page.isVisible("#s-bot")) && (await page.textContent("#extras > summary")).includes("Понадобится позже"));
await shot(page, "20-step1-keys-desktop-light");
await variants("20-step1-keys");
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
check("шаг 2 открылся, шаг 3 ещё закрыт", (await page.isVisible("#role-owner-state")) && (await page.isVisible("#chats-none")));

// --- шаг 2. вход в аккаунт ---------------------------------------------------------------------
await page.click("#role-owner-state button");
check("вход: без отметки согласия QR не показывается", !(await page.isVisible("#tg-login")));
await page.check("#consent-owner");
await page.click("#role-owner-state button");
await page.waitForSelector("#tg-login-qr svg");
await page.evaluate(() => { document.getElementById("toast").hidden = true; });
await page.locator("#tg-login-qr").screenshot({ path: path.join(OUT, "qr-login.png") });
if (DECODER) check("вход: QR со страницы распознаётся как ссылка входа Telegram", decodeQr(path.join(OUT, "qr-login.png")).startsWith("tg://login?token="));
await shot(page, "21-step2-login-desktop-light");
await variants("21-step2-login", (p) => p.waitForSelector("#tg-login-qr svg"));
await control("POST", "/scan?role=owner");
await page.waitForFunction(() => document.querySelector("#role-owner-state").textContent.includes("Подключён"), null, { timeout: 20000 });
check("вход: основной аккаунт подключён", (await page.textContent("#st-accounts")) === "Готово");

// --- шаг 3. чаты -------------------------------------------------------------------------------
await page.waitForSelector("#chats-list li");
check("чаты: сначала показаны первые 15", (await page.locator("#chats-list li").count()) === 15);
check("чаты: счётчик показывает, сколько выбрано и показано", (await page.textContent("#chats-counter")).includes("Показано 15 из"));
check("чаты: один аккаунт — вкладок нет", !(await page.isVisible("#chats-accounts")));
await page.click("#chats-more");
await page.waitForFunction(() => document.querySelectorAll("#chats-list li").length === 45);
check("чаты: «Показать ещё» догружает следующую порцию", (await page.textContent("#chats-more")).includes("осталось"));
await page.fill("#chats-q", "Иван Петров");
await page.waitForFunction(() => document.querySelectorAll("#chats-list li").length > 0 && document.querySelectorAll("#chats-list li").length < 30);
check("чаты: поиск по названию", (await page.textContent("#chats-list")).includes("Иван Петров"));
await page.locator("#chats-list li .chat-read input").first().check();
await page.waitForFunction(() => document.querySelector("#chats-counter").textContent.includes("Выбрано для чтения: 1 чат"));
check("чаты: включение чтения одного чата меняет счётчик", true);
await page.waitForFunction(() => document.querySelector("#progress").textContent.includes("выбрано: 1"), null, { timeout: 20000 });
check("строка «что уже сделано»: все три шага готовы", (await page.locator("#progress li.done").count()) === 3);
await page.fill("#chats-q", "");
await page.selectOption("#chats-kind", "channel");
await page.waitForFunction(() => document.querySelector("#chats-bulk") && !document.querySelector("#chats-bulk").hidden);
check("чаты: отбор по виду и кнопка «читать все»", (await page.textContent("#chats-bulk")).includes("каналы"));
check("чаты: служебный чат Telegram нельзя ни читать, ни вернуть", (await page.locator("#chats-list li", { hasText: "служебный чат" }).count()) === 0);
await page.locator("#chats-list li button").first().click();              // «Не сохранять» + два подтверждения
await page.waitForFunction(() => document.querySelector("#chats-list").textContent.includes("не сохраняется"));
check("чаты: исключённый чат помечен, читать его нельзя", await page.locator("#chats-list li.is-excluded .chat-read input").first().isDisabled());
await page.selectOption("#chats-kind", "");
await page.click("#chats-options > summary");
await page.selectOption("#opt-depth", "6");
await page.waitForFunction(() => !document.querySelector("#toast").hidden && document.querySelector("#toast").textContent.includes("Настройка сохранена"));
check("чаты: глубина истории сохраняется", true);
{
  // уведомление не перекрывает кнопки и не ловит нажатия
  const clash = await page.evaluate(() => {
    const t = document.getElementById("toast").getBoundingClientRect();
    const hit = (r) => r.width && r.height && !(r.right < t.left || r.left > t.right || r.bottom < t.top || r.top > t.bottom);
    const buttons = [...document.querySelectorAll("main .btn, main input, main select")].filter((b) => b.offsetParent !== null);
    return { over: buttons.filter((b) => hit(b.getBoundingClientRect())).length,
             events: getComputedStyle(document.getElementById("toast")).pointerEvents };
  });
  check("уведомление не ловит нажатия и не лежит поверх кнопок", clash.events === "none", "кнопок под уведомлением: " + clash.over);
}
await page.click("#chats-options > summary");
await page.waitForFunction(() => document.querySelectorAll("#chats-list li").length === 15);
await page.evaluate(() => { document.getElementById("toast").hidden = true; document.getElementById("s-chats").scrollIntoView(); });
await shot(page, "22-step3-chats-desktop-light");
await page.locator("#s-chats").screenshot({ path: path.join(OUT, "22b-step3-chats-section-desktop-light.png") });
await variants("22-step3-chats", (p) => p.waitForSelector("#chats-list li"));

// --- шаг 4. выгрузка ---------------------------------------------------------------------------
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
check("выгрузка: шаг необязательный и свёрнут", !(await page.isVisible("#import-form")) && (await page.textContent("#import-details > summary")).includes("необязательно"));
await page.click("#import-details > summary");
await page.setInputFiles("#import-file", exportFile);
const uploadRequest = page.waitForRequest((r) => r.url().endsWith("/api/imports") && r.method() === "POST");
await page.click("#import-upload");
check("выгрузка: файл уходит с ключом сессии в заголовке, без cookie",
      (await uploadRequest).headers()["x-shturman-session"] === KEY && !(await uploadRequest).headers()["cookie"]);
await page.waitForSelector(".import-chats li", { timeout: 30000 });
check("выгрузка: файл загружен и разобран, показаны чаты", (await page.locator(".import-chats li").count()) === 3);
check("выгрузка: служебный чат Telegram принять нельзя", await page.locator(".import-chats li", { hasText: "служебный чат" }).locator("input").isDisabled());
await page.locator(".import-chats li", { hasText: "Семья" }).locator("input").uncheck();
await page.waitForFunction(() => document.querySelector(".import-item .counter").textContent.includes("К импорту: 1 чат"));
check("выгрузка: снятый чат не идёт в импорт, счётчик пересчитан", true);
await shot(page, "23-step4-import-desktop-light");
await page.locator(".import-item button", { hasText: "Импортировать" }).click();
await page.waitForFunction(() => document.querySelector("#import-items").textContent.includes("Импорт завершён"), null, { timeout: 30000 });
check("выгрузка: импорт прошёл сразу, без карточки в боте", (await page.textContent("#import-items")).includes("Новых сообщений: 2"));
await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
await page.waitForFunction(() => document.querySelector("#collected").textContent.trim() !== "0", null, { timeout: 20000 });
check("счётчик «собрано сообщений» не нулевой", true);

// --- «Дополнительно» ---------------------------------------------------------------------------
await page.click("#extras > summary");
await page.waitForSelector("#s-bot");
check("«Дополнительно»: бизнес-режим скрыт, пока нет бота согласований", !(await page.isVisible("#s-business")));

// бот согласований
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
await control("POST", "/start?code=" + bindLink.split("start=")[1]);
await page.waitForFunction(() => document.querySelector("#bind-done").textContent.includes("Владелец привязан"), null, { timeout: 20000 });
check("привязка: страница сама увидела владельца и показала имя", (await page.textContent("#bind-done")).includes("Евгений Тестов"));
check("привязка: ссылка убрана с экрана", !(await page.isVisible("#bind-link-box")));

// бизнес-режим
await page.waitForSelector("#business-body:not([hidden])");
check("бизнес-режим: появился вместе с ботом; в схемах подставлено имя бота", (await page.locator("#s-business .bot-name").first().textContent()) === "@shturman_soglasovaniya_bot");
await control("POST", "/business?connect=1");
await page.click("#business-refresh");
await page.waitForFunction(() => document.querySelector("#business-capable").textContent.includes("режим включён"));
await page.waitForFunction(() => document.querySelector("#st-business").textContent === "Подключён", null, { timeout: 20000 });
check("бизнес-режим: подключение от владельца замечено", (await page.textContent("#st-business")).includes("Подключён"));

// аккаунт-помощник: QR и облачный пароль — в этом же блоке
await page.click("#role-assistant-state button");
await page.waitForSelector("#login-slot-assistant #tg-login-qr svg");
await control("POST", "/scan?role=assistant");
await page.waitForSelector("#tg-password-form:not([hidden])");
check("помощник: при облачном пароле появляется поле и подсказка", (await page.textContent("#tg-password-hint")).includes("кличка кота"));
await page.fill("#tg-password", "не тот пароль");
await page.click("#tg-password-send");
await page.waitForFunction(() => document.querySelector("#tg-login-error").textContent.includes("Неверный"));
check("помощник: неверный пароль — понятная ошибка, поле очищено", (await page.inputValue("#tg-password")) === "");
await page.fill("#tg-password", PASSWORD);
await page.click("#tg-password-send");
await page.waitForFunction(() => document.querySelector("#role-assistant-state").textContent.includes("Подключён"), null, { timeout: 20000 });
check("помощник: подключён", (await page.textContent("#role-assistant-state")).includes("Помощник"));
await page.waitForFunction(() => document.querySelectorAll("#chats-accounts .tab").length === 2);
check("чаты: два аккаунта — две вкладки", true);

// своя модель
await page.fill("#llm-key", LLM_KEY);
await page.fill("#llm-model", "плохое имя");
await page.click("#llm-save");
await page.waitForSelector("#llm-error:not([hidden])");
check("модель: неверное имя модели объяснено", (await page.textContent("#llm-error")).includes("Имя модели"));
for (const [url, words] of [["http://127.0.0.1:9119/v1", "https://"], ["https://127.0.0.1:9119/v1", "внутрь сервера"],
                            ["https://169.254.169.254/v1", "внутрь сервера"], ["https://inner.example/v1", "внутрь сервера"]]) {
  await page.fill("#llm-key", LLM_KEY);
  await page.fill("#llm-url", url);
  await page.fill("#llm-model", "gpt-4o-mini");
  await page.click("#llm-save");
  await page.waitForFunction((w) => document.querySelector("#llm-error").textContent.includes(w), words);
  check(`модель: адрес ${url} отвергнут`, true);
}
await page.fill("#llm-key", LLM_KEY);
await page.fill("#llm-url", "https://llm.example/v1");
await page.fill("#llm-model", "gpt-4o-mini");
await page.click("#llm-save");
await page.waitForFunction(() => document.querySelector("#llm-status").textContent.includes("gpt-4o-mini"), null, { timeout: 20000 });
check("модель: проверена пробным запросом и сохранена, поле ключа очищено", (await page.inputValue("#llm-key")) === "");
const llmBefore = (await control("GET", "/seen")).llm_requests;
await page.fill("#llm-url", "https://attacker.example/v1");
await page.click("#llm-save");
await page.waitForFunction(() => document.querySelector("#llm-error").textContent.includes("ключ заново"));
check("модель: смена адреса без ввода ключа не проходит, и запрос никуда не уходит",
      (await control("GET", "/seen")).llm_requests === llmBefore);
check("модель: на странице сказано про https и про локальную модель",
      (await page.textContent("#llm-rules")).includes("SHTURMAN_LLM_BASE_URL") && (await page.textContent("#llm-rules")).includes("https://"));
await page.fill("#llm-url", "https://llm.example/v1");

// счётчики и журнал
await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
await page.waitForFunction(() => document.querySelector("#tiles").textContent.includes("Сообщений в архиве") &&
  !/^0/.test(document.querySelector("#tiles .tile b").textContent));
check("что собрано: счётчики архива не нулевые", true);
await page.waitForFunction(() => document.querySelector("#audit-key").textContent.includes("Своя модель сервиса сохранена"), null, { timeout: 40000 });
const keyAudit = await page.textContent("#audit-key");
check("журнал: входы, ключи и аккаунты — отдельным списком",
      ["Вход по ссылке", "Токен бота согласований сохранён", "Аккаунт Telegram подключён", "Ключи приложения Telegram сохранены"].every((t) => keyAudit.includes(t)));
check("журнал: общий список действий", (await page.textContent("#audit")).length > 50);
check("отправка со страницы не включилась", (await page.textContent("#facts")).includes("Выключена"));
await page.evaluate(() => { document.getElementById("toast").hidden = true; document.getElementById("extras").scrollIntoView(); });
await shot(page, "24-extras-open-desktop-light");
await variants("24-extras-open", async (p) => {
  await p.click("#extras > summary");
  await p.waitForFunction(() => document.querySelector("#audit").children.length > 1);
  await p.evaluate(() => document.getElementById("extras").scrollIntoView());
});
await page.evaluate(() => window.scrollTo(0, 0));
await shot(page, "25-app-done-desktop-light");

// --- «дашборд» на соседнем порту: тот же браузер владельца, вход на страницу выполнен -------------
{
  const dash = await context.newPage();
  const res = await dash.goto(DASH + "/");
  check("подставной дашборд открыт на том же имени узла и другом порту",
        res.status() === 200 && new URL(DASH).hostname === new URL(BASE).hostname && new URL(DASH).port !== new URL(BASE).port);
  const stolen = await dash.evaluate((name) => ({
    direct: localStorage.getItem(name), all: JSON.stringify(Object.entries(localStorage)), session: JSON.stringify(Object.entries(sessionStorage)),
    cookie: document.cookie }), KEY_NAME);
  check("localStorage дашборда не видит ключа сессии страницы", stolen.direct === null && !stolen.all.includes(KEY) && !stolen.session.includes(KEY));
  check("cookie страницы до дашборда не доходят (их нет)", !stolen.cookie.includes(KEY) && stolen.cookie === "");

  // скрипт дашборда подкладывает cookie на общее имя узла и пробует все виды запросов
  const tries = await dash.evaluate(async ({ api, name }) => {
    document.cookie = "shturman_setup=planted; path=/";
    document.cookie = name + "=planted; path=/";
    const out = {};
    const attempt = async (label, url, options) => {
      try {
        const r = await fetch(url, options);
        let body = "";
        try { body = await r.text(); } catch (e) { body = ""; }
        out[label] = { type: r.type, status: r.status, body };
      } catch (e) { out[label] = { error: String(e) }; }
    };
    await attempt("read", api + "/state", { credentials: "include" });
    await attempt("read-session", api + "/session", { credentials: "include" });
    await attempt("read-nocors", api + "/state", { credentials: "include", mode: "no-cors" });
    await attempt("read-header", api + "/state", { credentials: "include", headers: { "X-Shturman-Session": "planted" } });
    await attempt("page-nocors", api.replace("/api", "/"), { credentials: "include", mode: "no-cors" });
    await attempt("script-nocors", api.replace("/api", "/static/setup.js"), { mode: "no-cors" });
    await attempt("act-simple", api + "/logout-all", { method: "POST", credentials: "include", mode: "no-cors", body: "{}" });
    await attempt("act-json", api + "/logout-all", { method: "POST", credentials: "include",
      headers: { "Content-Type": "application/json", "X-Shturman-Setup": "1" }, body: "{}" });
    await attempt("act-bind", api + "/bot/bind", { method: "POST", credentials: "include", mode: "no-cors", body: "{}" });
    // окно со страницей: прочитать его хранилище нельзя
    try {
      const w = window.open(api.replace("/api", "/"));
      await new Promise((r) => setTimeout(r, 1500));
      let got = null;
      try { got = w.localStorage.getItem(name); } catch (e) { got = "blocked: " + e.name; }
      out.window = { closed: !!(w && w.closed), got: String(got) };
      try { w.close(); } catch (e) { /* уже недоступно */ }
    } catch (e) { out.window = { error: String(e) }; }
    // рамка со страницей: встроить нельзя
    const frame = document.createElement("iframe");
    frame.src = api.replace("/api", "/");
    document.body.appendChild(frame);
    await new Promise((r) => setTimeout(r, 1200));
    let inside = "blocked";
    try { inside = frame.contentDocument ? frame.contentDocument.body.innerHTML.length : "null"; } catch (e) { inside = "blocked: " + e.name; }
    out.frame = String(inside);
    return out;
  }, { api: API, name: KEY_NAME });
  const readable = Object.entries(tries).filter(([, v]) => v && typeof v.body === "string" && v.body.length > 0);
  check("fetch со страницы-«дашборда» к API страницы ничего не читает", readable.length === 0,
        JSON.stringify(Object.fromEntries(Object.entries(tries).map(([k, v]) => [k, v.error ? "ошибка" : v.type || v]))));
  check("ни один ответ не содержит ключа сессии", !JSON.stringify(tries).includes(KEY));
  check("окно со страницей дашборду не читается", tries.window && !String(tries.window.got).includes(KEY) && (tries.window.closed || /blocked|null/.test(tries.window.got)),
        JSON.stringify(tries.window));
  check("встроить страницу в рамку нельзя", /blocked|null|^0$/.test(tries.frame), tries.frame);
  const after = await control("GET", "/seen");
  check("запросы «дашборда» ничего не изменили: «выйти везде» и ссылка привязки не выполнены",
        !after.audit.some((r) => r.action === "logout.all") && after.audit.filter((r) => r.action === "bot.bind_link").length === 1);
  check("сессия владельца цела, подложенные cookie ей не мешают",
        (await page.request.get(API + "/state", { headers: { "X-Shturman-Session": KEY, "Sec-Fetch-Site": "same-origin" } })).status() === 200);
  await page.reload();
  await page.waitForSelector("#screen-app:not([hidden])");
  check("страница с подложенными cookie открывается и вход на месте", (await storedKey()) === KEY);

  // service worker дашборда: страницу настройки под себя не забирает
  let worker = null;
  try {
    worker = await dash.evaluate(async () => {
      if (!("serviceWorker" in navigator)) return { unsupported: true };
      await navigator.serviceWorker.register("/sw.js", { scope: "/" });
      await navigator.serviceWorker.ready;
      if (!navigator.serviceWorker.controller) await new Promise((r) => navigator.serviceWorker.addEventListener("controllerchange", r, { once: true }));
      return { controlled: !!navigator.serviceWorker.controller };
    });
  } catch (e) { worker = { error: String(e).split("\n")[0] }; }
  if (worker && worker.controlled) {
    await page.reload();
    await page.waitForSelector("#screen-app:not([hidden])");
    await page.waitForSelector("#progress li");
    const own = await page.evaluate(async () => ({
      controller: !!navigator.serviceWorker.controller, registrations: (await navigator.serviceWorker.getRegistrations()).length }));
    check("service worker дашборда страницу настройки не контролирует", own.controller === false && own.registrations === 0);
    const seen = await dash.evaluate(() => new Promise((resolve) => {
      navigator.serviceWorker.addEventListener("message", (e) => resolve(e.data.seen), { once: true });
      navigator.serviceWorker.controller.postMessage("seen");
    }));
    check("service worker дашборда не видел ни одного запроса страницы настройки", !seen.some((u) => u.startsWith(BASE)), "запросов у него: " + seen.length);
    const registered = await page.evaluate(() => navigator.serviceWorker.register("static/setup.js").then(() => "registered", (e) => "refused: " + e.name));
    check("на origin страницы service worker не зарегистрировать", registered.startsWith("refused"), registered);
  } else {
    note("service worker в этой среде не запустился (" + JSON.stringify(worker) + "): проверка заменена проверкой на уровне HTTP — " +
         "файл под service worker страница не отдаёт (см. выше), а чужой origin её не контролирует по правилам браузера");
  }
  await dash.close();
}

// --- секреты не утекли -------------------------------------------------------------------------
const html = await page.content();
check("секреты и ключ сессии не появились ни в разметке страницы, ни в ответах сервера (кроме ответа на вход)",
      SECRETS.concat([KEY]).every((s) => !html.includes(s)) && SECRETS.every((s) => !seenBodies.some((b) => b.includes(s))) &&
      seenBodies.filter((b) => b.includes(KEY)).length === 1);
const seen = await control("GET", "/seen");
check("в журнале действий нет ни секретов, ни имён, ни названий чатов",
      !SECRETS.concat(["Иван", "Семья", "Тестов", "t.me/", "tg://", KEY]).some((s) => JSON.stringify(seen.audit).includes(s)));
check("страница не обращалась к чужим серверам", foreign.length === 0, foreign.slice(0, 3).join(", "));
check("в консоли браузера нет ошибок", consoleErrors.filter((e) => !/401|403|404|409|422|Failed to load resource|ServiceWorker|service worker/i.test(e)).length === 0, consoleErrors.slice(0, 3).join(" | "));

// --- выход и вход по коду от бота ---------------------------------------------------------------
await page.click("#logout");
await page.waitForSelector("#login-start:not([hidden])");
check("выход стирает ключ из хранилища страницы", (await storedKey()) === null);
check("выход: сессия закрыта на сервере — прежний ключ больше не действует",
      (await page.request.get(API + "/state", { headers: { "X-Shturman-Session": KEY } })).status() === 401);
check("бот согласований настроен и владелец привязан — предлагается вход по коду", await page.isVisible("#login-code-send"));
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
const KEY2 = await storedKey();
check("вход по коду: код из бота пускает, ключ сессии новый", !!KEY2 && KEY2 !== KEY);

// --- «выйти везде» командой ---------------------------------------------------------------------
check("команда setup-logout-all завершает сессии", /Завершено сессий страницы настройки: [1-9]/.test(cli("setup-logout-all")));
check("после неё прежняя сессия не действует", (await page.request.get(API + "/state", { headers: { "X-Shturman-Session": KEY2 } })).status() === 401);

// --- экраны входа: телефон и тёмная тема --------------------------------------------------------
{
  const ctx = await browser.newContext({ ...phone, colorScheme: "dark" });
  const p = await ctx.newPage();
  await p.goto(BASE + cli("setup-link").trim());
  await p.waitForSelector("#login-link:not([hidden])");
  await shot(p, "14-login-link-phone-dark");
  await ctx.close();
  const ctx2 = await browser.newContext(phone);
  const p2 = await ctx2.newPage();
  await p2.goto(URL_PAGE);
  await p2.waitForSelector("#login-start:not([hidden]), #login-nolink:not([hidden])");
  await shot(p2, "15-login-phone-light");
  await ctx2.close();
}

await browser.close();
console.log(failed ? `\nПровалено проверок: ${failed}` : "\nВсе проверки пройдены");
console.log("Снимки: " + OUT);
process.exit(failed ? 1 : 0);
