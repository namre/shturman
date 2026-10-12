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
await page.screenshot({ path: path.join(OUT, "approvals-phone-dark.png"), fullPage: true });
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
await page.screenshot({ path: path.join(OUT, "approvals-after.png"), fullPage: true });
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

await browser.close();
console.log(failed ? `ИТОГ: ${failed} FAIL` : "ИТОГ: OK");
process.exit(failed ? 1 : 0);
