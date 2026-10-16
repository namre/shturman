// Браузерная проверка договорённостей в приложении «Штурман» (Р-96): «обещано 23.09» у каждой, срок-событие
// словами, список «В команде» со «Скрыть» и «Вернуть», «Не так» с причиной в «Договорённостях» и в
// «Согласованиях». Стенд — miniapp_stand.py (README.md); пульт: POST /r96/seed, GET /r96/seen.
// Печатает PASS/FAIL, код возврата 1 при FAIL; снимки — в E2E_R96_OUT (по умолчанию E2E_OUT).

import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.E2E_PLAYWRIGHT || "playwright");

const PORT = process.env.E2E_PORT || "8775";
const APP = `http://localhost:${PORT}/shturman-setup/app/`;
const CONTROL = `http://127.0.0.1:${Number(PORT) + 1}`;
const OUT = process.env.E2E_R96_OUT || process.env.E2E_OUT || path.join(tmpdir(), "shturman-r96-shots");
const THEME_DARK = { bg_color: "#1c242f", text_color: "#f5f5f5", hint_color: "#8d9aa8", button_color: "#3e88f7",
                     button_text_color: "#ffffff", section_bg_color: "#232e3c", accent_text_color: "#6ab2f2" };
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
const get = async (p) => (await fetch(CONTROL + p)).json();
const post = async (p) => (await fetch(CONTROL + p, { method: "POST" })).json();
async function waitFor(fn, ms = 5000) {
  const until = Date.now() + ms;
  while (Date.now() < until) { if (await fn()) return true; await new Promise((r) => setTimeout(r, 100)); }
  return false;
}

const browser = await chromium.launch();
const phone = { deviceScaleFactor: 2, isMobile: true, hasTouch: true, locale: "ru-RU" };
async function openApp(width, scheme, theme) {
  const ctx = await browser.newContext({ ...phone, viewport: { width, height: 820 }, colorScheme: scheme });
  const p = await ctx.newPage();
  const errs = [];
  p.on("console", (m) => { if (m.type() === "error") errs.push(m.text()); });
  const init = await (await fetch(CONTROL + "/init-data?who=owner")).text();
  const params = new URLSearchParams({ tgWebAppData: init, tgWebAppVersion: "8.0", tgWebAppPlatform: "ios",
                                       tgWebAppThemeParams: JSON.stringify(theme) });
  await p.goto(APP + "#" + params.toString());
  await p.waitForSelector("#screen-approvals:not([hidden])");
  return { ctx, p, errs };
}

const ids = await post("/r96/seed");
check("пульт завёл договорённости Р-96", Object.keys(ids).length === 5, JSON.stringify(ids));

// «Мне обещали»: дата обещания и срок-событие словами
{
  const { ctx, p, errs } = await openApp(390, "light", THEME_LIGHT);
  await p.click("#tab-commitments");
  await p.waitForSelector("#cm-body:not([hidden])");
  await p.click("#cm-dir-owed_to_owner");
  await p.waitForSelector(`#cm-item-${ids.event}`);
  const card = await p.$eval(`#cm-item-${ids.event}`, (n) => n.innerText);
  check("у договорённости — «обещано ДД.ММ»", /обещано \d\d\.\d\d/.test(card), card);
  check("срок-событие — словами: «по событию: «к уикли»»", card.includes("по событию: «к уикли»"), card);
  await p.locator(`#cm-item-${ids.event}`).screenshot({ path: path.join(OUT, "card-promised-event-390-light.png") });
  await p.screenshot({ path: path.join(OUT, "commitments-theirs-390-light.png"), fullPage: true });
  await p.click("#cm-dir-owner_owes");
  await p.waitForSelector(`#cm-item-${ids.mine}`);
  // «Не так» с причиной
  await p.locator(`#cm-item-${ids.mine} button`, { hasText: "Не так" }).click();
  await p.waitForSelector(`#cm-item-${ids.mine} .cm-reasons`);
  const reasons = await p.$$eval(`#cm-item-${ids.mine} .cm-reasons button`, (n) => n.map((b) => b.textContent));
  check("«Не так»: причины на выбор", reasons.join("|") === "не обещание|не тот человек|неверно что|неверный срок|дубль|уже сделано",
        reasons.join("|"));
  await p.locator(`#cm-item-${ids.mine} .cm-reasons button`, { hasText: "неверный срок" }).click();
  await p.locator(`#cm-item-${ids.mine}`).screenshot({ path: path.join(OUT, "commitments-wrong-reason-390-light.png") });
  await p.locator(`#cm-item-${ids.mine} .confirm button`, { hasText: "Убрать" }).click();
  check("«Не так» с причиной — в журнале договорённости", await waitFor(async () => {
    const seen = await get("/r96/seen");
    return seen.status.mine === "rejected" && seen.events.mine[0].details.reason === "wrong_due";
  }));
  check("в консоли нет ошибок", errs.length === 0, errs.join(" | "));
  await ctx.close();
}

// «В команде» — 390 светлая, 360 тёмная; «Скрыть» и «Вернуть»
for (const [width, scheme, theme] of [[390, "light", THEME_LIGHT], [360, "dark", THEME_DARK]]) {
  const { ctx, p, errs } = await openApp(width, scheme, theme);
  await p.click("#tab-commitments");
  await p.waitForSelector("#cm-body:not([hidden])");
  await p.click("#cm-dir-team");
  await p.waitForSelector(`#cm-item-${ids.pies}`);
  const text = await p.evaluate(() => document.getElementById("cm-groups").innerText);
  check(`«В команде» (${width}, ${scheme}): кто → кому, что, когда обещано, без «Верно/Выполнено»`,
        text.includes("привезти пироги") && text.includes("Анна Белова") && text.includes("сходить привиться")
        && /обещано \d\d\.\d\d/.test(text) && !/Выполнено|Перенести|Верно/.test(text), text);
  const label = await p.textContent("#cm-dir-team-label");
  check("вкладка «В команде» — со счётчиком", label === "В команде · 2", label);
  const wide = await p.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1);
  check(`«В команде» (${width}): не шире экрана`, wide);
  await p.screenshot({ path: path.join(OUT, `team-${width}-${scheme}.png`), fullPage: true });
  if (width === 390) {
    await p.locator(`#cm-item-${ids.pies} button`, { hasText: "Скрыть" }).click();
    await p.waitForSelector("#toast:not([hidden]) .toast-btn");
    check("«Скрыть» — сразу", await waitFor(async () => (await get("/r96/seen")).status.pies === "rejected"));
    await p.click("#toast .toast-btn");
    check("«Вернуть» — снова «В команде»", await waitFor(async () => (await get("/r96/seen")).status.pies === "team"));
  }
  check(`в консоли нет ошибок (${width})`, errs.length === 0, errs.join(" | "));
  await ctx.close();
}

// «Согласования»: новое предложение видно сразу, «Не так» — с причиной
{
  const { ctx, p, errs } = await openApp(390, "light", THEME_LIGHT);
  const card = p.locator(".card", { hasText: "проверить смету по лифтам" });
  check("новое предложение — в «Согласованиях» до сводки в боте", await card.count() === 1);
  const body = await card.innerText();
  check("карточка: «обещано ДД.ММ»", /обещано \d\d\.\d\d/.test(body), body);
  await card.locator("button", { hasText: "Не так" }).click();
  await p.waitForSelector(".card .why");
  await card.screenshot({ path: path.join(OUT, "approvals-why-390-light.png") });
  await card.locator(".why button", { hasText: "уже сделано" }).click();
  check("«Не так» → «уже сделано» — отклонено с причиной", await waitFor(async () => {
    const seen = await get("/r96/seen");
    return seen.status.proposed === "rejected" && seen.events.proposed[0].details.reason === "already_done";
  }));
  check("в консоли нет ошибок («Согласования»)", errs.length === 0, errs.join(" | "));
  await ctx.close();
}

await browser.close();
console.log(failed ? `FAIL: ${failed}` : "OK");
process.exit(failed ? 1 : 0);
