import { chromium } from 'playwright';
import { execSync } from 'node:child_process';
const OUT = process.env.E2E_OUT || '.';
const PYTHON = process.env.E2E_PYTHON || 'python3';   // интерпретатор, в котором установлен Hermes
const PLUGIN = new URL('../../', import.meta.url).pathname;
const HOME = process.env.HERMES_HOME;
const BASE = process.env.E2E_BASE || 'http://shturman.test:8080';
const cli = (args) => execSync(`${PYTHON} ${PLUGIN}cli.py ${args}`, {encoding:'utf8', stdio:['ignore','pipe','ignore']}).trim();
// чистое состояние: владельца нет, мастер не пройден (ключ подписи оставляем)
execSync(`rm -f ${HOME}/plugin-data/shturman/{wizard,owner,pairing,business,login,activation}.json`, {shell:'/bin/bash'});
const browser = await chromium.launch();
const out = [];
const check = (name, ok, extra='') => { out.push(`${ok ? 'PASS' : 'FAIL'}  ${name}${extra ? ' — ' + extra : ''}`); };

// 1. без сессии: дашборд уводит на страницу входа, владельца нет
{
  const ctx = await browser.newContext(); const page = await ctx.newPage();
  const errors = []; page.on('console', m => { if (m.type()==='error') errors.push(m.text()); });
  await page.goto(BASE + '/');
  await page.waitForSelector('#send');
  check('страница входа ничего не раскрывает до нажатия', !new URL(page.url()).search.includes('m='), new URL(page.url()).search.replace(/state=[^&]+/, 'state=…'));
  await page.click('#send');
  await page.waitForSelector('#view-message:not([hidden])');
  check('без владельца — сообщение вместо поля кода', (await page.textContent('#message-text')).includes('ссылке активации'), page.url().split('?')[0]);
  // Отказ 400 на просьбу прислать код — штатный ответ, браузер пишет его в консоль; ищем только нарушения политики содержимого.
  const csp = errors.filter(e => /Content Security Policy|Refused to/i.test(e));
  check('страница входа не нарушает политику содержимого', csp.length === 0, csp.join(' | '));
  await page.screenshot({ path: `${OUT}/shot-login-noowner.png` });
  await ctx.close();
}
// 2. вход по ссылке активации
const link = cli(`activation-link ${BASE}`);
check('ссылка ведёт на страницу активации', link.startsWith(BASE + '/shturman-auth/activate.html#'));
let cookies;
{
  const ctx = await browser.newContext(); const page = await ctx.newPage();
  await page.goto(link);
  await page.waitForSelector('#view-ok:not([hidden])');
  await page.screenshot({ path: `${OUT}/shot-activate.png` });
  await page.click('#go');
  await page.waitForURL(u => !u.pathname.startsWith('/shturman-auth') && !u.pathname.startsWith('/auth'), { timeout: 20000 });
  check('после активации открыт дашборд', true, new URL(page.url()).pathname);
  const me = await page.evaluate(() => fetch('/api/auth/me', {credentials:'same-origin'}).then(r => r.json()));
  check('сессия принадлежит провайдеру shturman', me.provider === 'shturman' || me.session?.provider === 'shturman', JSON.stringify(me).slice(0,160));
  cookies = await ctx.cookies();
  check('сессионные куки HttpOnly', cookies.filter(c => c.name.includes('hermes_session_at') || c.name.includes('hermes_session_rt')).every(c => c.httpOnly));
  await ctx.close();
}
// 3. ссылка одноразовая
{
  const ctx = await browser.newContext(); const page = await ctx.newPage();
  await page.goto(link);
  await page.click('#go');
  await page.waitForSelector('#view-message:not([hidden])');
  check('повторное использование ссылки отклонено', (await page.textContent('#message-text')).includes('уже использована'));
  await ctx.close();
}
// 4. неполная ссылка
{
  const ctx = await browser.newContext(); const page = await ctx.newPage();
  await page.goto(BASE + '/shturman-auth/activate.html');
  check('ссылка без кода — понятное сообщение', await page.isVisible('#view-bad'));
  await ctx.close();
}
// 5. API без сессии закрыт, страницы плагина тоже
{
  const ctx = await browser.newContext(); const page = await ctx.newPage();
  const r1 = await page.request.get(BASE + '/api/env'); 
  const r2 = await page.request.get(BASE + '/api/plugins/shturman/state');
  check('API без сессии: 401', r1.status() === 401 && r2.status() === 401, `${r1.status()}/${r2.status()}`);
  await ctx.close();
}
console.log(out.join('\n'));
await browser.close();
process.exit(out.some(l => l.startsWith('FAIL')) ? 1 : 0);
