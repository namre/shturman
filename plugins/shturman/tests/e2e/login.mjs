import { chromium } from 'playwright';
const OUT = process.env.E2E_OUT || '.';
const PYTHON = process.env.E2E_PYTHON || 'python3';   // интерпретатор, в котором установлен Hermes
const PLUGIN = new URL('../../', import.meta.url).pathname;
const HOME = process.env.HERMES_HOME; const BASE = process.env.E2E_BASE || 'http://shturman.test:8080';
const browser = await chromium.launch(); const out = [];
const check = (n, ok, extra='') => out.push(`${ok?'PASS':'FAIL'}  ${n}${extra?' — '+extra:''}`);
{ // владелец привязан, но бот написать не может (в стенде нет токена): честное сообщение, без поля кода
  const ctx = await browser.newContext(); const page = await ctx.newPage();
  await page.goto(BASE + '/sessions');
  await page.waitForSelector('#view-message:not([hidden])');
  check('сбой отправки кода показан понятно', (await page.textContent('#message-text')).includes('Не удалось отправить код'), new URL(page.url()).searchParams.get('m'));
  await ctx.close();
}
{ // вид страницы с полем кода и реакция на неверный код (state поддельный → «страница устарела»)
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 860 } }); const page = await ctx.newPage();
  await page.goto(BASE + '/shturman-auth/login.html?state=abc&m=sent');
  await page.waitForSelector('#code');
  await page.fill('#code', '12345678');
  check('код форматируется при вводе', (await page.inputValue('#code')) === '1234 5678');
  await page.screenshot({ path: `${OUT}/shot-login-code.png` });
  await page.click('#code-submit');
  await page.waitForSelector('#view-message:not([hidden])');
  check('без начатого входа код не принимается', (await page.textContent('#message-text')).includes('устарела'));
  await ctx.close();
}
{ // телефон
  const ctx = await browser.newContext({ viewport: { width: 390, height: 800 } }); const page = await ctx.newPage();
  await page.goto(BASE + '/shturman-auth/login.html?state=abc&m=sent');
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  check('страница входа на телефоне без горизонтальной прокрутки', overflow <= 1, String(overflow));
  await page.screenshot({ path: `${OUT}/shot-login-phone.png`, fullPage: true });
  await ctx.close();
}
console.log(out.join('\n')); await browser.close();
process.exit(out.some(l => l.startsWith('FAIL')) ? 1 : 0);
