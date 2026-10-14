// Браузерная проверка страницы настройки: ссылка → вход → бот согласований → способ «как вы» → ключи,
// вход, чаты (и список большого аккаунта: загрузка в фоне, «подождите» от Telegram; раскладки и сферы) → выгрузка → смена способа на «сотрудник»: бизнес-режим, аккаунт ассистента → «Дополнительно» →
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
const LOAD_OUT = process.env.E2E_LOAD_OUT || OUT;                // снимки загрузки списка чатов (Р-81)
const SRC = process.env.E2E_SRC || path.resolve(import.meta.dirname, "../../src");
const DSN = process.env.SHTURMAN_TEST_DSN;
const GOOD_TOKEN = "7000000001:SENTINEL-bot-token_DoNotLeak-0123456789";
const BUSY_TOKEN = "7000000002:BUSY-bot-token_polled-by-another-0123456789";
const PASSWORD = "очень-секретный-пароль-77";
const LLM_KEY = "sk-e2e-not-a-real-key-0123456789";
// at-/rt-/code-SENTINEL — токены и код входа подставного OpenAI (подписка ChatGPT): ни в ответы страницы,
// ни в журнал действий они не попадают никогда.
const SECRETS = [GOOD_TOKEN, BUSY_TOKEN, PASSWORD, LLM_KEY, "0123456789abcdef0123456789abcdef",
                 "at-SENTINEL", "rt-SENTINEL", "code-SENTINEL"];
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

// --- первый экран: бот согласований и выбор способа ------------------------------------------
await page.waitForSelector("#progress li");
check("сверху — строка «что уже сделано» (бот и способ) и счётчик собранного",
      (await page.locator("#progress li").count()) === 2 && (await page.textContent(".collected")).includes("Собрано сообщений"));
check("первый шаг — бот согласований, второй — выбор способа; остальные шаги до выбора скрыты",
      (await page.textContent("#s-bot .num")) === "1" && (await page.textContent("#s-mode .num")) === "2" &&
      !(await page.isVisible("#s-keys")) && !(await page.isVisible("#s-accounts")) && !(await page.isVisible("#s-business")));
check("у шагов есть «что это даст» и свёрнутые вопросы",
      (await page.locator("#s-bot .gives").count()) === 1 && !(await page.isVisible("#s-bot details.faq dl")));
check("«Дополнительно» свёрнуто", !(await page.isVisible("#s-llm")));
await shot(page, "20-start-desktop-light");
await variants("20-start");

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
check("бот: шаг 1 отмечен готовым", (await page.locator("#progress li.done").count()) === 1);

// способ подключения: «как вы»
await page.click("#mode-own");
await page.waitForSelector("#s-keys:not([hidden])");
check("способ «как вы»: появились ключи, вход и чаты под номерами 3–5; бизнес-режима и помощника нет",
      (await page.textContent("#s-keys .num")) === "3" && (await page.textContent("#s-accounts .num")) === "4" &&
      (await page.textContent("#s-chats .num")) === "5" && !(await page.isVisible("#s-business")) && !(await page.isVisible("#s-assistant")));
check("способ «как вы»: выбранная карточка отмечена", (await page.getAttribute("#choice-own", "class")).includes("is-chosen") && await page.isDisabled("#mode-own"));
check("строка «что уже сделано»: пять шагов", (await page.locator("#progress li").count()) === 5);
check("вход и чаты закрыты, пока нет ключей; сказано, когда откроются",
      (await page.textContent("#s-accounts .step-lock")).includes("ключи") && !(await page.isVisible("#accounts-body")));
await shot(page, "21-mode-own-desktop-light");
await variants("21-mode-own");

// ключи приложения
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
check("вход открылся, выбор чатов ещё закрыт", (await page.isVisible("#role-owner-state")) && (await page.isVisible("#s-chats .step-lock")));

// вход в аккаунт
await page.click("#role-owner-state button");
check("вход: без отметки согласия QR не показывается", !(await page.isVisible("#tg-login")));
await page.check("#consent-owner");
await page.click("#role-owner-state button");
await page.waitForSelector("#tg-login-qr svg");
await page.evaluate(() => { document.getElementById("toast").hidden = true; });
await page.locator("#tg-login-qr").screenshot({ path: path.join(OUT, "qr-login.png") });
if (DECODER) check("вход: QR со страницы распознаётся как ссылка входа Telegram", decodeQr(path.join(OUT, "qr-login.png")).startsWith("tg://login?token="));
await shot(page, "22-login-desktop-light");
await variants("22-login", (p) => p.waitForSelector("#tg-login-qr svg"));
await control("POST", "/scan?role=owner");
await page.waitForFunction(() => document.querySelector("#role-owner-state").textContent.includes("Подключён"), null, { timeout: 20000 });
check("вход: основной аккаунт подключён", (await page.textContent("#st-accounts")) === "Готово");

// чаты
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
check("строка «что уже сделано»: все пять шагов готовы", (await page.locator("#progress li.done").count()) === 5);
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
await shot(page, "23-chats-desktop-light");
await page.locator("#s-chats").screenshot({ path: path.join(OUT, "23b-chats-section-desktop-light.png") });
await variants("23-chats", (p) => p.waitForSelector("#chats-list li"));

// --- список чатов большого аккаунта (Р-81): загрузка в фоне, «подождите» от Telegram на середине ---
// У аккаунта становится на 4 000 диалогов больше (4 157, 42 страницы); подставной Telegram «думает» над страницей 0,6 с,
// а на 25-м запросе просит подождать 25 с. «Обновить» отвечает сразу, страница показывает ход, отсчёт,
// уже загруженное, и загрузка продолжается с той же страницы.
{
  mkdirSync(LOAD_OUT, { recursive: true });
  const loadTitle = () => page.evaluate(() => document.querySelector("#chats-load").hidden ? "" :
    document.querySelector("#chats-load-title").textContent + " | " + document.querySelector("#chats-load-text").textContent);
  async function loadShot(p, name) {
    // снимок шага целиком: липкая шапка страницы иначе ложится поверх строк списка
    await p.evaluate(() => { const t = document.getElementById("toast"); if (t) t.hidden = true;
                             document.querySelector(".top").style.position = "static"; });
    await p.locator("#s-chats").screenshot({ path: path.join(LOAD_OUT, name + ".png") });
    await p.evaluate(() => { document.querySelector(".top").style.position = ""; });
  }
  // тот же экран в остальных видах — пока загрузка в нужном состоянии
  async function loadVariants(stage, word) {
    const stored = await context.storageState();
    for (const [name, options] of VARIANTS) {
      const ctx = await browser.newContext({ ...options, storageState: stored });
      const p = await ctx.newPage();
      await p.goto(URL_PAGE);
      await p.waitForSelector("#screen-app:not([hidden])");
      await p.waitForSelector("#chats-list li");
      await p.evaluate(() => document.getElementById("s-chats").scrollIntoView());
      const seen = await p.waitForFunction((w) => !document.querySelector("#chats-load").hidden &&
        document.querySelector("#chats-load-title").textContent.includes(w), word, { timeout: 15000 }).then(() => true, () => false);
      await p.waitForTimeout(700);                         // полоса хода доезжает плавно
      await loadShot(p, `${stage}-${name}`);
      const overflow = await p.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      check(`большой аккаунт (${stage}, ${name}): «${word}» видно, страница не шире экрана`, seen && overflow <= 1,
            "лишних пикселей: " + overflow);
      await ctx.close();
    }
  }

  const before = await control("GET", "/dialogs/seen?role=owner");
  const big = await control("POST", "/dialogs/big?role=owner&n=4000&delay=0.6&flood_at=25&seconds=25");
  const total = big.dialogs;                               // сколько диалогов у аккаунта теперь
  const pageCount = Math.ceil(total / 100) + (total % 100 ? 0 : 1);   // при ровной сотне — ещё пустая страница
  await page.evaluate(() => document.getElementById("s-chats").scrollIntoView());
  const rowsBefore = await page.locator("#chats-list li").count();
  const started = Date.now();
  await page.click("#chats-refresh");
  await page.waitForFunction(() => !document.querySelector("#chats-load").hidden &&
    document.querySelector("#chats-load-title").textContent.includes("Загружаю"));
  check("большой аккаунт: «Обновить» отвечает сразу, загрузка идёт в фоне", Date.now() - started < 3000,
        `${Date.now() - started} мс`);
  check("большой аккаунт: кнопка «Обновить» недоступна, пока список грузится", await page.isDisabled("#chats-refresh"));
  check("большой аккаунт: прежний список виден, пока грузится новый",
        (await page.locator("#chats-list li").count()) === rowsBefore && rowsBefore > 0, String(rowsBefore));
  await page.waitForFunction((n) => {
    const words = document.querySelector("#chats-load-text").textContent.replace(/\s/g, "");
    return /Загружено[1-9]/.test(words) && words.includes("из~" + n);
  }, total, { timeout: 15000 });
  const loading = await loadTitle();
  check(`большой аккаунт: «Загружено N из ~${total}»`, loading.replace(/\s/g, "").includes("из~" + total), loading);
  await page.waitForTimeout(700);
  await loadShot(page, "dialogs-loading-desktop-light");
  await loadVariants("dialogs-loading", "Загружаю");

  await page.waitForFunction(() => document.querySelector("#chats-load-title").textContent.includes("Telegram просит подождать"),
                             null, { timeout: 30000 });
  const wait1 = await loadTitle();
  await page.waitForTimeout(1600);
  const wait2 = await loadTitle();
  const left = (t) => Number((t.match(/через (\d+) с/) || [])[1]);
  check("большой аккаунт: Telegram просит подождать — отсчёт идёт, «нажимать ничего не нужно»",
        left(wait2) < left(wait1) && wait1.includes("нажимать ничего не нужно") && wait1.includes("с того же места"),
        `${wait1} → ${wait2}`);
  check("большой аккаунт: во время ожидания уже загруженное на месте и «Обновить» недоступна",
        (await page.locator("#chats-list li").count()) >= rowsBefore && await page.isDisabled("#chats-refresh"));
  await loadShot(page, "dialogs-waiting-desktop-light");
  await loadVariants("dialogs-waiting", "Telegram просит подождать");

  await page.waitForFunction(() => document.querySelector("#chats-load").hidden, null, { timeout: 90000 });
  await page.waitForFunction((n) => document.querySelector("#chats-counter").textContent.replace(/\s/g, "").includes(`из${n}.`), total,
                             { timeout: 10000 });
  const after = await control("GET", "/dialogs/seen?role=owner");
  const pages = after.pages.slice(before.pages.length);
  check("большой аккаунт: после ожидания загрузка продолжилась с той же страницы — каждая страница по одному разу",
        pages.length === pageCount && new Set(pages).size === pageCount && after.calls - before.calls === pageCount + 1
        && after.stored === total,
        JSON.stringify({ pages: pages.length, calls: after.calls - before.calls, stored: after.stored, dialogs: big.dialogs }));
  check("большой аккаунт: счётчик говорит, когда список обновлён",
        (await page.textContent("#chats-counter")).includes("Список из Telegram обновлён только что"),
        await page.textContent("#chats-counter"));
  await page.fill("#chats-q", "Татьяна Фёдорова");
  await page.waitForFunction(() => document.querySelectorAll("#chats-list li").length > 0 &&
    [...document.querySelectorAll("#chats-list .chat-title")].every((n) => n.textContent.startsWith("Татьяна Фёдорова")));
  check("большой аккаунт: поиск находит диалог из конца длинного списка", (await page.locator("#chats-list li").count()) > 0);
  await page.fill("#chats-q", "");
  await page.waitForFunction(() => document.querySelectorAll("#chats-list li").length === 15);
  await loadShot(page, "dialogs-done-desktop-light");
}

// --- раскладки списка чатов и сферы (Р-82) ------------------------------------------------------
// У аккаунта — 4 000+ диалогов (после блока выше), у подставного Telegram — четыре папки, у диалогов —
// разное время последнего сообщения; пульт заводит три сферы с чатами. Раскладки «Папки Telegram /
// Сферы / По активности / По виду»: разделы со счётчиком, постранично внутри раздела, «Включить все»
// с подтверждением, «Сделать сферу из папки», сферы чата в строке; раздел «Сферы»: завести, связать
// папку — новый чат папки предлагается, а не добавляется; снимки в четырёх видах.
{
  const SPH_OUT = process.env.E2E_SPHERES_OUT || OUT;
  mkdirSync(SPH_OUT, { recursive: true });
  const seeded = await control("POST", "/spheres/seed?role=owner");
  check("сферы: стенд завёл папки Telegram и сферы, список перечитан", seeded.folders === 4 && seeded.dialogs > 1000,
        JSON.stringify(seeded));
  async function chatsShot(p, name, selector = "#s-chats") {
    await p.evaluate(() => { const t = document.getElementById("toast"); if (t) t.hidden = true;
                             document.querySelector(".top").style.position = "static"; });
    await p.locator(selector).screenshot({ path: path.join(SPH_OUT, name + ".png") });
    await p.evaluate(() => { document.querySelector(".top").style.position = ""; });
  }
  const PREFIX = { folders: "folder:", spheres: "sphere:", activity: "activity:", kind: "kind:" };
  async function openLayout(p, id) {
    await p.click(`#chats-layout-${id}`);
    await p.waitForSelector(`#chats-sections details.sect[data-section^="${PREFIX[id]}"]`, { timeout: 15000 });
  }
  async function openSection(p, title) {
    const det = p.locator("#chats-sections details.sect", { has: p.locator(".sect-title", { hasText: title }) }).first();
    if (!(await det.evaluate((d) => d.open))) await det.locator("summary").click();
    await det.locator(".chatlist li").first().waitFor({ timeout: 15000 });
    return det;
  }
  async function waitFor(test, timeout = 10000) {
    const until = Date.now() + timeout;
    while (Date.now() < until) {
      if (await test()) return true;
      await new Promise((r) => setTimeout(r, 200));
    }
    return false;
  }
  async function sphereVariants(stage, prepare, selector = "#s-chats") {
    const stored = await context.storageState();
    for (const [name, options] of VARIANTS) {
      const ctx = await browser.newContext({ ...options, storageState: stored });
      const p = await ctx.newPage();
      p.on("dialog", (d) => d.accept());
      await p.goto(URL_PAGE);
      await p.waitForSelector("#screen-app:not([hidden])");
      await p.waitForSelector("#chats-list li");
      await prepare(p);
      await p.waitForTimeout(400);
      await chatsShot(p, `${stage}-${name}`, selector);
      const overflow = await p.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      check(`сферы (${stage}, ${name}): страница не шире экрана`, overflow <= 1, "лишних пикселей: " + overflow);
      await ctx.close();
    }
  }

  await page.reload();
  await page.waitForSelector("#chats-list li");
  await page.evaluate(() => document.getElementById("s-chats").scrollIntoView());
  check("раскладки: переключатель «Все чаты / Папки Telegram / Сферы / По активности / По виду»",
        (await page.locator("#chats-layout .tab").allTextContents()).join("|") === "Все чаты|Папки Telegram|Сферы|По активности|По виду");

  // папки Telegram
  await openLayout(page, "folders");
  const folderTitles = await page.locator("#chats-sections .sect-title").allTextContents();
  check("раскладка «Папки»: разделы — папки Telegram и «Не в папках»",
        folderTitles.join("|") === "💼 Работа|🏗 Объекты|📰 Новости|🏠 Семья|Не в папках", folderTitles.join("|"));
  check("раскладка «Папки»: список чатов скрыт, вместо него разделы", !(await page.isVisible("#chats-list")) && !(await page.isVisible("#chats-more")));
  const work = await openSection(page, "Работа");
  check("папка «Работа»: в разделе её чаты постранично", (await work.locator(".chatlist li").count()) === 20 &&
        (await work.locator("button", { hasText: "Показать ещё" }).count()) === 1);
  const objects = page.locator("#chats-sections details.sect", { has: page.locator(".sect-title", { hasText: "Объекты" }) });
  check("папка с правилом «без звука»: сказано, что сервис видит не всё",
        (await objects.locator(".hint").allTextContents()).join(" ").includes("общие настройки"));
  const before = (await work.locator(".sect-count").textContent()).trim();
  await work.locator("button", { hasText: "Включить все" }).click();         // подтверждение принимается само
  await page.waitForFunction(() => document.querySelector("#toast").textContent.includes("Включено чатов"), null, { timeout: 20000 });
  const workAfter = page.locator("#chats-sections details.sect", { has: page.locator(".sect-title", { hasText: "Работа" }) }).first();
  await page.waitForFunction(() => {
    const d = [...document.querySelectorAll("#chats-sections details.sect")].find((x) => x.textContent.includes("Работа"));
    return d && /читается (\d+) из \1$/.test(d.querySelector(".sect-count").textContent.trim());
  }, null, { timeout: 15000 });
  check("папка «Работа»: «Включить все» — все чаты раздела читаются", true,
        before + " → " + (await workAfter.locator(".sect-count").textContent()).trim());
  await chatsShot(page, "30-layout-folders-desktop-light");
  await sphereVariants("30-layout-folders", async (p) => { await openLayout(p, "folders"); await openSection(p, "Работа"); });

  // «Выключить все» на странице: каналы папки «Новости» включаются разом и разом выключаются
  {
    const countOf = async (title) => page.evaluate((t) => {
      const d = [...document.querySelectorAll("#chats-sections details.sect")].find((x) => x.querySelector(".sect-title").textContent.includes(t));
      return d ? d.querySelector(".sect-count").textContent.trim() : "";
    }, title);
    const newsSect = await openSection(page, "Новости");
    await newsSect.locator("button", { hasText: "Включить все" }).click();
    await page.waitForFunction(() => document.querySelector("#toast").textContent.includes("Включено чатов"), null, { timeout: 20000 });
    await waitFor(async () => /читается (\d+) из \1$/.test(await countOf("Новости")), 15000);
    const on = await countOf("Новости");
    const newsAgain = page.locator("#chats-sections details.sect", { has: page.locator(".sect-title", { hasText: "Новости" }) }).first();
    await newsAgain.locator("button", { hasText: "Выключить все" }).click();
    await page.waitForFunction(() => document.querySelector("#toast").textContent.includes("Выключено чатов"), null, { timeout: 20000 });
    const off = await waitFor(async () => /читается 0 из/.test(await countOf("Новости")), 15000);
    const audit = (await control("GET", "/spheres/seen")).audit;
    check("страница: «Выключить все» в разделе — ни один чат папки не читается, в журнале tg.bulk_off",
          off && audit.some((r) => r.action === "tg.bulk_off"), on + " → " + (await countOf("Новости")));
  }

  // «Сделать сферу из папки»
  const news = await openSection(page, "Новости");
  await news.locator("button", { hasText: "Сделать сферу из папки" }).click();
  await page.waitForFunction(() => document.querySelector("#toast").textContent.includes("Сфера «Новости» заведена"), null, { timeout: 15000 });
  check("«Сделать сферу из папки»: сфера заведена с чатами папки",
        (await control("GET", "/spheres/seen")).spheres.some((s) => s.title === "Новости" && s.chats >= 25));

  // сферы
  await openLayout(page, "spheres");
  const sphereTitles = await page.locator("#chats-sections .sect-title").allTextContents();
  check("раскладка «Сферы»: разделы — сферы по порядку и «Без сферы»",
        sphereTitles.join("|") === "Работа РАЗУМ|Личное|Преподавание|Новости|Без сферы", sphereTitles.join("|"));
  const workSphere = await openSection(page, "Работа РАЗУМ");
  const ivanRow = workSphere.locator(".chatlist li", { hasText: "Иван Петров" }).first();
  check("строка чата: подписаны все его сферы", (await ivanRow.locator(".sph-chip").allTextContents()).join("|") === "Работа РАЗУМ|Личное");
  await ivanRow.locator("button", { hasText: "Сферы" }).click();
  await ivanRow.locator(".sph-pick label", { hasText: "Преподавание" }).locator("input").check();
  await chatsShot(page, "31-chat-spheres-picker-desktop-light", "#chats-sections");
  await ivanRow.locator(".sph-pick button", { hasText: "Сохранить" }).click();
  await page.waitForFunction(() => document.querySelector("#toast").textContent.includes("Сферы чата сохранены"), null, { timeout: 15000 });
  await page.waitForFunction(() => [...document.querySelectorAll("#chats-sections .sect-title")].some((x) => x.textContent === "Преподавание"));
  await chatsShot(page, "32-layout-spheres-desktop-light");
  await sphereVariants("32-layout-spheres", async (p) => { await openLayout(p, "spheres"); await openSection(p, "Работа РАЗУМ"); });

  // активность
  await openLayout(page, "activity");
  const activity = await page.locator("#chats-sections .sect-title").allTextContents();
  check("раскладка «По активности»: за неделю, месяц, 3 месяца, давно молчат",
        activity.join("|") === "За неделю|За месяц|За 3 месяца|Давно молчат", activity.join("|"));
  const silent = await openSection(page, "Давно молчат");
  const silentCount = (await silent.locator(".sect-count").textContent()).trim();
  check("«Давно молчат» у большого аккаунта — тысячи чатов, постранично", /из [1-9]\s?\d{3}/.test(silentCount) &&
        (await silent.locator("button", { hasText: "Показать ещё" }).count()) === 1, silentCount);
  await chatsShot(page, "33-layout-activity-desktop-light");
  await sphereVariants("33-layout-activity", async (p) => { await openLayout(p, "activity"); await openSection(p, "За неделю"); });

  // вид
  await openLayout(page, "kind");
  check("раскладка «По виду»: личные, группы, каналы, боты",
        (await page.locator("#chats-sections .sect-title").allTextContents()).join("|") === "Личные|Группы|Каналы|Боты");
  await chatsShot(page, "34-layout-kind-desktop-light");

  // раздел «Сферы»: завести, связать папку, новый чат папки предлагается
  await page.click("#chats-layout-all");
  await page.waitForSelector("#chats-list li");
  await page.click("#spheres-details > summary");
  await page.waitForSelector("#sph-list li.sph");
  check("раздел «Сферы»: сферы с описанием, числом чатов и папкой",
        (await page.textContent("#sph-list")).includes("стройка, продажи, совещания") &&
        (await page.textContent("#sph-list")).includes("папка «Новости»"));
  await page.fill("#sph-new-title", "Учёба");
  await page.fill("#sph-new-description", "курсы и книги");
  await page.click("#sph-new-save");
  await page.waitForFunction(() => document.querySelector("#sph-list").textContent.includes("Учёба"));
  check("раздел «Сферы»: новая сфера заведена", true);
  const folderRow = page.locator("#sph-folders li", { hasText: "Работа" }).first();
  await folderRow.locator("select").selectOption({ label: "Работа РАЗУМ" });
  await folderRow.locator("button", { hasText: "Предлагать новые" }).click();
  await page.waitForFunction(() => document.querySelector("#toast").textContent.includes("будут предлагаться"), null, { timeout: 15000 });
  const linkedProposals = await page.locator("#sph-proposals .sph-offer").count();
  await control("POST", "/spheres/folder-new?role=owner");
  await page.click("#spheres-details > summary");
  await page.click("#spheres-details > summary");
  await page.waitForFunction(() => document.querySelector("#sph-proposals").textContent.includes("Новый Заказчик"), null, { timeout: 15000 });
  const seenBefore = await control("GET", "/spheres/seen");
  check("связанная папка: новый чат папки предложен, но в сферу сам не добавлен",
        (await page.textContent("#sph-proposals")).includes("Добавить в сферу «Работа РАЗУМ»?") &&
        seenBefore.spheres.find((s) => s.title === "Работа РАЗУМ").chats > 0, String(linkedProposals));
  await page.evaluate(() => document.getElementById("s-spheres").scrollIntoView());
  await chatsShot(page, "35-spheres-card-desktop-light", "#s-spheres");
  await sphereVariants("35-spheres-card", async (p) => {
    await p.click("#spheres-details > summary");
    await p.waitForSelector("#sph-list li.sph");
    await p.locator("#sph-list li.sph").first().locator("button", { hasText: "Чаты" }).click();
    await p.waitForSelector("#sph-list .sph-chats li");
  }, "#s-spheres");
  // связанная папка предлагает всё, чего в сфере ещё нет: и прежние чаты папки, и новый
  const workBefore = seenBefore.spheres.find((s) => s.title === "Работа РАЗУМ").chats;
  const offered = Number((/Новые чаты в папке «Работа»: (\d+)/.exec(await page.textContent("#sph-proposals")) || [])[1]);
  await page.locator("#sph-proposals button", { hasText: "Добавить в сферу" }).first().click();
  await page.waitForFunction(() => !document.querySelector("#sph-proposals").textContent.includes("Новый Заказчик"), null, { timeout: 15000 });
  const seenAfter = await control("GET", "/spheres/seen");
  check("предложение принято: предложенные чаты добавлены в сферу",
        offered > 0 && seenAfter.spheres.find((s) => s.title === "Работа РАЗУМ").chats === workBefore + offered,
        `${workBefore} + ${offered}`);
  check("журнал: сферы и массовое включение записаны без названий",
        seenAfter.audit.some((r) => r.action === "tg.bulk_on") && seenAfter.audit.some((r) => r.action === "sphere.folder") &&
        !JSON.stringify(seenAfter.audit).includes("Работа РАЗУМ") && !JSON.stringify(seenAfter.audit).includes("Иван"),
        seenAfter.audit.map((r) => r.action).join(","));

  // карточка «Сферы»: переименовать, поднять выше, удалить (чаты сферы остаются в архиве)
  {
    const named = (t) => page.locator("#sph-list li.sph", { has: page.locator(".sph-name", { hasText: t }) }).first();
    const names = () => page.$$eval("#sph-list .sph-name", (n) => n.map((x) => x.textContent));
    await named("Учёба").locator("button", { hasText: "Изменить" }).click();
    await page.fill("#sph-edit-title", "Учёба и курсы");
    await page.locator("#sph-list form.sph-edit button", { hasText: "Сохранить" }).click();
    const renamed = await waitFor(async () => (await names()).includes("Учёба и курсы"), 10000);
    const was = (await names()).indexOf("Учёба и курсы");
    await named("Учёба и курсы").locator('button[aria-label^="Выше"]').click();
    const moved = await waitFor(async () => (await names()).indexOf("Учёба и курсы") === was - 1, 10000);
    check("карточка «Сферы»: переименование и порядок", renamed && moved && was > 0, (await names()).join("|"));
    const news = (await control("GET", "/spheres/seen")).spheres.find((s) => s.title === "Новости");
    await named("Новости").locator("button", { hasText: "Удалить" }).click();     // подтверждение принимается само
    const gone = await waitFor(async () => !(await names()).includes("Новости"), 10000);
    const seenGone = await control("GET", "/spheres/seen");
    check("удаление сферы с чатами: сфера пропала из списка, в журнале sphere.delete",
          gone && news && news.chats > 0 && !seenGone.spheres.some((s) => s.title === "Новости") &&
          seenGone.audit.some((r) => r.action === "sphere.delete"));
  }
  await page.click("#spheres-details > summary");
  await page.evaluate(() => document.getElementById("s-chats").scrollIntoView());
}

// --- выгрузка ----------------------------------------------------------------------------------
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
await shot(page, "24-import-desktop-light");
await page.locator(".import-item button", { hasText: "Импортировать" }).click();
await page.waitForFunction(() => document.querySelector("#import-items").textContent.includes("Импорт завершён"), null, { timeout: 30000 });
// Эти два сообщения сервис мог уже получить из аккаунта — тогда они «уже были в архиве».
const importText = await page.textContent("#import-items");
const importNums = /Новых сообщений: (\d+), уже были в архиве: (\d+)/.exec(importText);
check("выгрузка: импорт прошёл сразу, без карточки в боте", !!importNums && Number(importNums[1]) + Number(importNums[2]) === 2, importText.slice(0, 200));

// Та же выгрузка архивом zip папки: result.json внутри папки, рядом — фото.
const zipFile = path.join(OUT, "export.zip");
execFileSync(process.env.E2E_PYTHON || "python3", ["-c", [
  "import sys, zipfile",
  "with zipfile.ZipFile(sys.argv[2], 'w', zipfile.ZIP_DEFLATED) as z:",
  "    z.write(sys.argv[1], 'ChatExport_2026-10-01/result.json')",
  "    z.writestr('ChatExport_2026-10-01/photos/photo_1.jpg', b'jpeg')"].join("\n"), exportFile, zipFile]);
check("выгрузка: поле принимает и zip, и result.json", (await page.getAttribute("#import-file", "accept")) === ".json,.zip");
const importHint = await page.textContent("#import-hint") + " " + await page.textContent("#import-howto");
check("выгрузка: подсказка — лучше архив папки, как сжать, предел размера",
      importHint.includes("архив всей папки выгрузки (zip)") && importHint.includes("«Сжатая ZIP-папка»") &&
      importHint.includes("«Сжать»") && /Предел размера — [\d,]+ ГБ/.test(importHint), importHint.slice(0, 120));
await page.setInputFiles("#import-file", zipFile);
await page.click("#import-upload");
await page.waitForSelector(".import-chats li", { timeout: 30000 });
check("выгрузка архивом: архив загружен, result.json из него разобран, показаны чаты", (await page.locator(".import-chats li").count()) === 3);
await shot(page, "24c-import-zip-desktop-light");
await page.locator(".import-item button", { hasText: "Импортировать" }).click();
await page.waitForFunction(() => document.querySelector("#import-items").textContent.includes("Файлы из архива"), null, { timeout: 30000 });
check("выгрузка архивом: после импорта сказано про файлы из архива", (await page.textContent("#import-items")).includes("разбор фото и документов выключены"));
await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
await page.waitForFunction(() => document.querySelector("#collected").textContent.trim() !== "0", null, { timeout: 20000 });
check("счётчик «собрано сообщений» не нулевой", true);

// --- смена способа: «отдельный сотрудник» ------------------------------------------------------
await page.click("#mode-staff");                                    // подтверждение принимается само
await page.waitForSelector("#s-business:not([hidden])");
check("способ «сотрудник»: бизнес-режим — шаг 3, ключи — 4, аккаунт ассистента — 5, группы — 6",
      (await page.textContent("#s-business .num")) === "3" && (await page.textContent("#s-keys .num")) === "4" &&
      (await page.textContent("#s-assistant .num")) === "5" && (await page.textContent("#s-chats .num")) === "6");
check("способ «сотрудник»: вход в основной аккаунт виден без номера и с пояснением — он уже подключён",
      (await page.isVisible("#s-accounts")) && (await page.textContent("#s-accounts .num")) === "" &&
      (await page.textContent("#s-accounts .other-note")).includes("Не входит в выбранный способ"));
check("способ «сотрудник»: тексты шагов — свой вариант", (await page.innerText("#h-chats")).trim() === "Какие группы читать" &&
      (await page.isVisible("#s-keys .optional-tag")));
check("смена способа ничего не отключила", (await page.textContent("#st-accounts")) === "Готово");
check("бизнес-режим: в схемах подставлено имя бота", (await page.locator("#s-business .bot-name").first().textContent()) === "@shturman_soglasovaniya_bot");
await control("POST", "/business?connect=1");
await page.click("#business-refresh");
await page.waitForFunction(() => document.querySelector("#business-capable").textContent.includes("режим включён"));
await page.waitForFunction(() => document.querySelector("#st-business").textContent === "Подключён", null, { timeout: 20000 });
check("бизнес-режим: подключение от владельца замечено", (await page.textContent("#st-business")).includes("Подключён"));

// аккаунт ассистента: QR и облачный пароль — в этом же блоке
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
await page.evaluate(() => { document.getElementById("toast").hidden = true; window.scrollTo(0, 0); });
await shot(page, "25-mode-staff-desktop-light");
await variants("25-mode-staff");

// --- «Дополнительно» ---------------------------------------------------------------------------
await page.click("#extras > summary");
await page.waitForSelector("#s-llm");

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

// подписка ChatGPT вместо ключа: вход вставкой адреса. Страница входа OpenAI — подставная, в сеть не ходим.
await context.route("https://auth.openai.com/**", (route) => route.fulfill({
  status: 200, contentType: "text/html; charset=utf-8", body: "<!doctype html><title>ChatGPT</title><p>Подставной вход</p>" }));
check("подписка: блок виден, кнопка «Войти через ChatGPT» есть",
      (await page.isVisible("#sub-start")) && (await page.textContent("#sub-block")).includes("мастере Hermes"));
const popupWait = context.waitForEvent("page", { timeout: 10000 }).catch(() => null);
await page.click("#sub-start");                     // ключ API уже сохранён: страница спрашивает о замене — да
await page.waitForSelector("#sub-paste:not([hidden])");
const popup = await popupWait;
if (popup) await popup.waitForLoadState("domcontentloaded").catch(() => {});
const authUrl = popup ? popup.url() : (await page.getAttribute("#sub-open", "href")) || "";
check("подписка: страница входа ChatGPT открыта в новой вкладке",
      !!popup && authUrl.startsWith("https://auth.openai.com/api/accounts/authorize?"), authUrl.slice(0, 70));
if (popup) await popup.close();
check("подписка: объяснено, что вкладка покажет ошибку и адрес надо скопировать",
      (await page.textContent("#sub-howto")).includes("127.0.0.1:1455/auth/callback"));
await shot(page, "26a-subscription-paste-desktop-light");
await page.fill("#sub-address", "http://127.0.0.1:1455/auth/callback?code=x&state=chuzhoy&client_id=oaiapp_x");
await page.click("#sub-finish");
await page.waitForFunction(() => document.querySelector("#sub-error").textContent.includes("не от последней"));
check("подписка: адрес от чужой попытки отвергнут, поле очищено", (await page.inputValue("#sub-address")) === "");
const { address } = await control("POST", "/chatgpt/authorize?url=" + encodeURIComponent(authUrl));
await page.fill("#sub-address", address);
await page.click("#sub-finish");
await page.waitForFunction(() => document.querySelector("#sub-status").textContent.includes("Используется подписка ChatGPT"),
                           null, { timeout: 20000 });
check("подписка: подключена — видны учётная запись, модель и ссылка «Лимиты»",
      (await page.textContent("#sub-status")).includes("owner@example.com") && (await page.isVisible("#sub-usage")) &&
      (await page.isVisible("#sub-model")) && (await page.getAttribute("#sub-usage", "href")) === "https://chatgpt.com/settings/usage");
check("подписка: заменила ключ API", (await page.textContent("#st-llm")).includes("Подписка"));
await page.evaluate(() => { document.getElementById("toast").hidden = true; document.getElementById("s-llm").scrollIntoView(); });
await shot(page, "26b-subscription-connected-desktop-light");

// счётчики и журнал
await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
await page.waitForFunction(() => document.querySelector("#tiles").textContent.includes("Сообщений в архиве") &&
  !/^0/.test(document.querySelector("#tiles .tile b").textContent));
check("что собрано: счётчики архива не нулевые", true);
await page.waitForFunction(() => document.querySelector("#audit-key").textContent.includes("Своя модель сервиса сохранена"), null, { timeout: 40000 });
const keyAudit = await page.textContent("#audit-key");
check("журнал: входы, ключи и аккаунты — отдельным списком",
      ["Токен бота согласований сохранён", "Аккаунт Telegram подключён", "Своя модель сервиса сохранена"].every((t) => keyAudit.includes(t)), keyAudit.slice(0, 300));
check("журнал: общий список действий", (await page.textContent("#audit")).length > 50);
check("отправка со страницы не включилась", (await page.textContent("#facts")).includes("Выключена"));
await page.evaluate(() => { document.getElementById("toast").hidden = true; document.getElementById("extras").scrollIntoView(); });
await shot(page, "26-extras-open-desktop-light");
await variants("26-extras-open", async (p) => {
  await p.click("#extras > summary");
  await p.waitForFunction(() => document.querySelector("#audit").children.length > 1);
  await p.evaluate(() => document.getElementById("extras").scrollIntoView());
});
await page.evaluate(() => window.scrollTo(0, 0));
await shot(page, "27-app-done-desktop-light");

// --- память ассистента: не шаг, отдельная карточка после шагов ----------------------------------
{
  const seeded = await control("POST", "/seed-memory");
  check("память: стенд завёл страницу о человеке и то, что ждёт решения", seeded.ok === true, JSON.stringify(seeded));
  check("память: карточка после шагов и перед «Дополнительно», без номера шага, свёрнута", await page.evaluate(() => {
    const card = document.getElementById("s-memory");
    const after = (a, b) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    return after(document.getElementById("s-media"), card) && after(card, document.getElementById("extras")) &&
      !card.classList.contains("step") && !card.querySelector(".num") && !document.getElementById("memory-details").open;
  }));
  check("память: в свёрнутом виде — короткое пояснение", (await page.textContent("#s-memory .memory-lead")).includes("проектах"));
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await page.waitForFunction(() => document.querySelector("#st-memory").textContent === "ждут решения: 4");
  check("память: на свёрнутой карточке видно, сколько ждёт решения", true);

  await page.click("#memory-details > summary");
  await page.waitForSelector("#mem-list li");
  const explained = await page.textContent("#s-memory .memory-body > p");
  check("память: объяснено, что такое страница, что сводка — каждую ночь, а «ваш блок» ассистент не меняет",
        explained.includes("страницы") && explained.includes("каждую ночь") && explained.includes("ассистент их не меняет"));
  check("память: вкладки «Люди», «Проекты», «Профиль» и «Ждут решения» с числом",
        (await page.locator("#mem-tabs [role=tab]").allTextContents()).join("|") === "Люди|Проекты|Профиль|Ждут решения4");
  check("память: в списке людей — страница с датой обновления",
        (await page.textContent("#mem-list")).includes("Ольга Смирнова") && (await page.textContent("#mem-list")).includes("обновлена"));

  await page.fill("#mem-q", "чертежам");
  await page.waitForFunction(() => document.querySelector("#mem-list").textContent.includes("найдено в"));
  check("память: поиск по словам со страниц находит человека и показывает, где нашлось",
        (await page.textContent("#mem-list")).includes("Ольга Смирнова"));
  await page.fill("#mem-q", "нетакогослова");
  await page.waitForFunction(() => document.querySelector("#mem-list").textContent.includes("Ничего не нашлось"));
  check("память: поиск без совпадений объяснён", true);
  await page.fill("#mem-q", "");
  await page.waitForFunction(() => document.querySelectorAll("#mem-list .mem-row").length === 1);
  await page.evaluate(() => { document.getElementById("toast").hidden = true; document.getElementById("s-memory").scrollIntoView(); });
  await shot(page, "28-memory-people-desktop-light", false);

  await page.click("#mem-list .mem-row");
  await page.waitForSelector("#mem-owner");
  const shown = await page.textContent("#mem-panel");
  check("память: страница — сводка, факты, ваши заметки, договорённости, хронология",
        ["Сводка", "Факты", "должность: руководитель проектного отдела", "Ваши заметки", "Договорённости", "Хронология",
         "Руководит проектным отделом", "прислать проект фасада по корпусу 2"].every((t) => shown.includes(t)) &&
        (await page.locator(".mem-fact button:has-text('Неверно')").count()) === 1, shown.slice(0, 200));
  check("память: источник — словом «сообщение», без ссылок и разметки",
        shown.includes("сообщение · сказал собеседник") && (await page.locator("#mem-panel a").count()) === 0 &&
        !shown.includes("msg:") && !shown.includes("](") && !shown.includes("<!--"));
  check("память: сказано, что блок заметок пишете только вы", (await page.textContent("#mem-owner-hint")).includes("только вы"));
  check("память: «Сохранить» недоступна, пока текст не изменён", await page.isDisabled("#mem-owner-save"));
  await page.evaluate(() => document.getElementById("s-memory").scrollIntoView());
  await shot(page, "29-memory-person-desktop-light", false);

  const NOTE = "Не писать после 19:00.\nРешения по деньгам — только через меня.";
  await page.fill("#mem-owner", NOTE);
  await page.click("#mem-owner-save");
  await page.waitForFunction(() => document.querySelector("#mem-owner-note").textContent.includes("Сохранено"));
  await page.click(".mem-back");
  await page.waitForSelector("#mem-list .mem-row");
  await page.click("#mem-list .mem-row");
  await page.waitForSelector("#mem-owner");
  check("память: заметки сохранены и видны после повторного открытия", (await page.inputValue("#mem-owner")) === NOTE);
  const audited = (await control("GET", "/seen")).audit.filter((r) => r.action.startsWith("memory."));
  check("память: правка заметок — в журнале действий, без текста и имён",
        audited.length === 1 && audited[0].action === "memory.owner_block" && !audited[0].detail.includes("19:00"),
        JSON.stringify(audited));

  await page.click("#mem-tab-pending");
  await page.waitForSelector(".mem-item");
  const waiting = await page.textContent("#mem-panel");
  check("память: «Ждут решения» — проект, факт о вас, страница и договорённость",
        (await page.locator(".mem-item").count()) === 4 && ["Склад на Окружной", "групповой чат: сообщений за 30 дн. — 24", "Чаты: Склад на Окружной: снабжение",
          "роль: отвечаю за бюджет фасадов", "Сергей Ковалёв", "прислать график поставок бетона"].every((t) => waiting.includes(t)));
  await page.evaluate(() => document.getElementById("s-memory").scrollIntoView());
  await shot(page, "30-memory-pending-desktop-light", false);
  await page.click(".mem-item:has-text('поставок бетона') >> text=✓ Верно");
  await page.waitForFunction(() => document.querySelectorAll(".mem-item").length === 3);
  await page.waitForFunction(() => document.querySelector("#st-memory").textContent === "ждут решения: 3");
  const decided = (await control("GET", "/seen")).audit.filter((r) => r.action === "memory.commitment_accept");
  check("память: договорённость принята со страницы сразу, без карточки в боте; в журнале — номер, без текста",
        decided.length === 1 && /^№ \d+$/.test(decided[0].detail), JSON.stringify(decided));
  await page.evaluate(() => { document.getElementById("toast").hidden = true; });

  // проекты: заведённый проект, «Неверно», чаты, новый проект, архив
  await page.click("#mem-tab-projects");
  await page.waitForSelector("#mem-projects .mem-row");
  check("проекты: заведённый проект в списке с его чатом",
        (await page.textContent("#mem-projects")).includes("ЖК «Береговой»") && (await page.textContent("#mem-projects")).includes("Береговой: стройка"));
  await page.click("#mem-projects .mem-row:has-text('Береговой')");
  await page.waitForSelector("#mem-add-chat");
  const project = await page.textContent("#mem-panel");
  check("проекты: страница — сводка, заметки, решения, факты, договорённости, хронология, чаты, архив",
        ["Сводка", "Ваши заметки", "Решения", "фасад корпуса 2 — керамогранит", "Факты", "смета фасада: 12 млн",
         "срок сдачи корпуса: март", "Договорённости", "Хронология", "Чаты проекта", "Береговой: стройка", "В архив",
         "Ещё называют: Береговой"].every((t) => project.includes(t)), project.slice(0, 300));
  await page.evaluate(() => { document.getElementById("toast").hidden = true; document.getElementById("s-memory").scrollIntoView(); });
  await shot(page, "31-memory-project-desktop-light", false);
  await page.click(".mem-fact:has-text('смета фасада') >> text=Неверно");
  await page.waitForFunction(() => document.querySelector("#mem-add-chat") &&
    ![...document.querySelectorAll(".mem-fact")].some((li) => li.textContent.includes("смета фасада")));
  check("проекты: «Неверно» убирает факт, остальные на месте",
        [...await page.locator(".mem-fact").allTextContents()].some((t) => t.includes("срок сдачи корпуса")));
  await page.selectOption("#mem-add-chat", { label: "Ольга Смирнова · личный чат" });
  await page.click("#mem-add-chat-go");
  await page.waitForFunction(() => document.querySelectorAll("#mem-project-chats li").length === 2);
  check("проекты: чат добавлен к проекту", (await page.textContent("#mem-project-chats")).includes("Ольга Смирнова"));
  await page.click("#mem-project-chats li:has-text('Ольга Смирнова') >> text=Убрать");
  await page.waitForFunction(() => document.querySelectorAll("#mem-project-chats li").length === 1);
  check("проекты: чат убран из проекта", true);
  // сфера проекта (Р-82): одна, выбирается из сфер владельца
  await page.selectOption("#mem-project-sphere", { label: "Работа РАЗУМ" });
  await page.click("#mem-project-sphere-save");
  await page.waitForFunction(() => document.querySelector("#toast").textContent.includes("Сфера проекта сохранена"));
  await page.waitForFunction(() => document.querySelector("#mem-project-sphere") &&
    document.querySelector("#mem-project-sphere").selectedOptions[0].textContent === "Работа РАЗУМ");
  check("проекты: сфера проекта сохранена", true);

  await page.click(".mem-back");
  await page.click("#mem-project-new");
  await page.waitForSelector("#mem-new-title");
  await page.fill("#mem-new-title", "Офис на Ленина");
  await page.fill("#mem-new-aliases", "Ленина, офис");
  await page.fill("#mem-new-chats-q", "Сергей");
  await page.check("#mem-new-chats-list label:has-text('Сергей Ковалёв') input");
  await page.evaluate(() => { document.getElementById("toast").hidden = true; document.getElementById("s-memory").scrollIntoView(); });
  await shot(page, "32-memory-new-project-desktop-light", false);
  await page.click("#mem-new-save");
  await page.waitForFunction(() => (document.querySelector("#mem-panel h3") || {}).textContent === "Офис на Ленина");
  check("проекты: новый проект заведён сразу, с чатом и другими названиями",
        (await page.textContent("#mem-project-chats")).includes("Сергей Ковалёв") &&
        (await page.textContent("#mem-panel")).includes("Ещё называют: Ленина, офис"));
  await page.click("#mem-archive");
  await page.waitForSelector(".mem-archive summary");
  check("проекты: «В архив» — проект в свёрнутом списке архива",
        (await page.textContent(".mem-archive summary")) === "В архиве: 1" && !(await page.textContent("#mem-projects")).includes("Ленина"));

  // профиль: правила и факты о вас
  await page.click("#mem-tab-profile");
  await page.waitForSelector("#mem-owner");
  check("профиль: объяснено, что факты о вас появляются только после вашего «✓», а правила ассистент не меняет",
        (await page.textContent("#mem-panel")).includes("только после вашего «✓»") &&
        (await page.textContent("#mem-owner-hint")).includes("никогда их не меняет"));
  const RULES = "Отвечать коротко.\nПо выходным не беспокоить, кроме срочного по стройке.";
  await page.fill("#mem-owner", RULES);
  await page.click("#mem-owner-save");
  await page.waitForFunction(() => document.querySelector("#mem-owner-note").textContent.includes("Сохранено"));

  await page.click("#mem-tab-pending");
  await page.waitForSelector(".mem-item");
  await page.click(".mem-item:has-text('отвечаю за бюджет фасадов') >> text=✓ Верно");
  await page.waitForFunction(() => document.querySelectorAll(".mem-item").length === 2);
  await page.click(".mem-item:has-text('Склад на Окружной') >> text=✓ Завести проект");
  await page.waitForFunction(() => document.querySelectorAll(".mem-item").length === 1);
  await page.waitForFunction(() => document.querySelector("#st-memory").textContent === "ждут решения: 1");
  check("ждут решения: факт о вас и проект приняты со страницы", true);
  await page.click("#mem-tab-profile");
  await page.waitForSelector("#mem-owner");
  check("профиль: принятый факт о вас и сохранённые правила видны",
        (await page.textContent("#mem-panel")).includes("роль: отвечаю за бюджет фасадов") && (await page.inputValue("#mem-owner")) === RULES);
  await page.evaluate(() => { document.getElementById("toast").hidden = true; document.getElementById("s-memory").scrollIntoView(); });
  await shot(page, "33-memory-profile-desktop-light", false);
  const memoryAudit = (await control("GET", "/seen")).audit.filter((r) => r.action.startsWith("memory."));
  const memoryActions = memoryAudit.map((r) => r.action);
  check("журнал: действия экрана памяти записаны, без названий и текстов",
        ["memory.fact_retract", "memory.project_chats", "memory.project_create", "memory.project_archive", "memory.profile_block",
         "memory.owner_fact_accept", "memory.project_accept"].every((a) => memoryActions.includes(a)) &&
        !["Береговой", "Ленина", "Окружной", "коротко", "бюджет", "смета"].some((w) => JSON.stringify(memoryAudit).includes(w)),
        JSON.stringify(memoryAudit));

  // те же экраны в тёмной теме и на телефоне — только карточка памяти, она длинная
  const views = [["desktop-dark", { ...desktop, colorScheme: "dark" }], ["phone-light", phone],
                 ["phone-dark", { ...phone, colorScheme: "dark" }],
                 ["phone360-light", { ...phone, viewport: { width: 360, height: 740 } }]];
  const stored = await context.storageState();
  for (const [name, options] of views) {
    const ctx = await browser.newContext({ ...options, storageState: stored });
    const p = await ctx.newPage();
    await p.goto(URL_PAGE);
    await p.waitForSelector("#screen-app:not([hidden])");
    await p.click("#memory-details > summary");
    await p.waitForSelector("#mem-list .mem-row");
    for (const [stage, open] of [
      ["29-memory-person", async () => { await p.click("#mem-list .mem-row"); await p.waitForSelector("#mem-owner"); }],
      ["31-memory-project", async () => {
        await p.click("#mem-tab-projects"); await p.click("#mem-projects .mem-row:has-text('Береговой')"); await p.waitForSelector("#mem-add-chat"); }],
      ["32-memory-new-project", async () => {
        await p.click(".mem-back"); await p.click("#mem-project-new"); await p.waitForSelector("#mem-new-title");
        await p.fill("#mem-new-title", "Офис на Ленина"); }],
      ["33-memory-profile", async () => { await p.click("#mem-tab-profile"); await p.waitForSelector("#mem-owner"); }],
      ["30-memory-pending", async () => { await p.click("#mem-tab-pending"); await p.waitForSelector(".mem-item"); }]]) {
      await open();
      await p.waitForTimeout(200);
      // снимок одной карточки: липкая шапка страницы иначе ложится поверх её середины
      await p.evaluate(() => { document.querySelector(".top").style.position = "static"; });
      await p.locator("#s-memory").screenshot({ path: path.join(OUT, `${stage}-${name}.png`) });
      const overflow = await p.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      check(`вёрстка (${stage}, ${name}): страница не шире экрана`, overflow <= 1, "лишних пикселей: " + overflow);
    }
    await ctx.close();
  }
}

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
