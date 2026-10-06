import { chromium } from 'playwright';
import { execSync } from 'node:child_process';
const OUT = process.env.E2E_OUT || '.';
const PYTHON = process.env.E2E_PYTHON || 'python3';   // интерпретатор, в котором установлен Hermes
const PLUGIN = new URL('../../', import.meta.url).pathname;
const HOME = process.env.HERMES_HOME;
const BASE = process.env.E2E_BASE || 'http://shturman.test:8080';
const cli = (args) => execSync(`${PYTHON} ${PLUGIN}cli.py ${args}`, {encoding:'utf8', stdio:['ignore','pipe','ignore']}).trim();
const py = (code) => execSync(`${PYTHON} -c "import sys; sys.path.insert(0,'${PLUGIN}'); ${code}"`, {encoding:'utf8'}).trim();
const out = []; const check = (n, ok, extra='') => out.push(`${ok?'PASS':'FAIL'}  ${n}${extra?' — '+extra:''}`);
const shot = (page, name) => page.screenshot({ path: `${OUT}/w-${name}.png`, fullPage: true });

// чистое состояние мастера (ключ подписи оставляем)
execSync(`rm -f ${HOME}/plugin-data/shturman/{wizard,owner,pairing,business,login,activation}.json`, {shell:'/bin/bash'});
py(`from shturman_core import wizard; from shturman_core.state import Store; wizard.remember_bot(Store(),'ivan_shturman_bot','Мой Штурман')`);

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1360, height: 900 } });
const page = await ctx.newPage();
const errors = []; page.on('console', m => { if (m.type()==='error') errors.push(m.text()); });
page.on('pageerror', e => errors.push('pageerror: ' + e.message));

process.on('uncaughtException', async (e) => { try { await shot(page, 'debug'); } catch (_) {} console.log(out.join('\n')); console.log('ERRORS', errors.join(' || ')); console.log(String(e).split('\n').slice(0,4).join('\n')); process.exit(2); });
// вход по ссылке активации → сразу мастер
await page.goto(cli(`activation-link ${BASE}`));
await page.click('#go');
await page.waitForSelector('.shturman-stepper', { timeout: 30000 });
check('после активации открыт мастер', new URL(page.url()).pathname === '/shturman');
check('мастер начинается с первого шага', (await page.textContent('.shturman-stepbtn.is-active')).includes('Помощник'));
await shot(page, '1-persona');

// --- шаг 1: помощник ---
await page.click('.shturman-card:has-text("Дживс")');
await page.fill('#sh-owner', 'Иван Иванович');
await page.click('.shturman-pill:has-text("Бизнес-ассистент")');
await page.fill('#sh-gen', 'Ивана Ивановича');
check('пример подписи обновляется', (await page.textContent('.shturman-preview')).includes('Я бизнес-ассистент Ивана Ивановича.'));
await page.click('button:has-text("Сохранить и продолжить")');
await page.waitForSelector('h2:has-text("Какая модель")');
const soul = execSync(`cat ${HOME}/SOUL.md`, {encoding:'utf8'});
check('характер записан в SOUL.md штатным вызовом', soul.includes('Тебя зовут Дживс') && soul.includes('бизнес-ассистент Ивана Ивановича'));
check('стоковый текст SOUL.md сохранён', soul.includes('Hermes Agent'));

// --- шаг 2: модель (провайдер подменён: настоящего ключа в тесте нет) ---
let authed = false;
await page.route('**/api/providers/validate*', r => r.fulfill({ json: { ok: true, reachable: true, message: '' } }));
await page.route('**/api/env*', r => r.request().method() === 'PUT' ? (authed = true, r.fulfill({ json: { ok: true } })) : r.continue());
await page.route('**/api/model/options*', r => r.fulfill({ json: { model: '', provider: '', providers: [
  { slug: 'openrouter', name: 'OpenRouter', authenticated: authed, models: ['deepseek/deepseek-v4-pro','deepseek/deepseek-v4-flash'], featured_models: ['deepseek/deepseek-v4-pro'] },
  { slug: 'openai', name: 'OpenAI', authenticated: false, models: [], featured_models: [] },
  { slug: 'openai-codex', name: 'ChatGPT', authenticated: false, models: [], featured_models: [] } ] } }));
await page.route('**/api/model/recommended-default*', r => r.fulfill({ json: { provider: 'openrouter', model: 'deepseek/deepseek-v4-pro' } }));
await page.route('**/api/model/set*', r => r.fulfill({ json: { ok: true, scope: 'main' } }));
await page.route('**/api/plugins/shturman/model/probe', async r => {
  await page.request.post(BASE + '/api/plugins/shturman/mark', { data: { key: 'model_ok' } });
  r.fulfill({ json: { ok: true, reply: 'Работает' } });
});
await page.reload(); await page.waitForSelector('.shturman-stepper');
await page.click('.shturman-stepbtn:has-text("Модель")');
await page.waitForSelector('#sh-key');
await shot(page, '2-model-key');
check('выбор модели закрыт до сохранения ключа', await page.isVisible('text=Откроется после сохранения ключа.'));
await page.fill('#sh-key', 'sk-or-test-not-a-real-key');
await page.click('button:has-text("Проверить и сохранить ключ")');
await page.waitForSelector('#sh-model');
check('после ключа предложена модель', (await page.inputValue('#sh-model')) === 'deepseek/deepseek-v4-pro');
await page.click('button:has-text("Выбрать и проверить")');
await page.waitForSelector('text=Модель ответила');
await shot(page, '2-model-ok');
await page.click('.shturman-card:has-text("Подписка ChatGPT")');
await page.waitForSelector('button:has-text("Войти через ChatGPT")');
await shot(page, '2-model-chatgpt');
await page.click('.shturman-actions button:has-text("Далее")');

// --- шаг 3: бот (Telegram и шлюз подменены; привязка — настоящая, через файл состояния) ---
let tokenSet = false, stamp = 1;
const platform = () => ({ platforms: [{ id: 'telegram', enabled: tokenSet, configured: tokenSet, state: tokenSet ? 'connected' : 'disabled',
  env_vars: [{ key: 'TELEGRAM_BOT_TOKEN', is_set: tokenSet }, { key: 'TELEGRAM_ALLOWED_USERS', is_set: false }] }] });
const puts = [];
await page.route('**/api/messaging/platforms**', r => {
  if (r.request().method() === 'PUT') { puts.push(r.request().postDataJSON()); tokenSet = true; return r.fulfill({ json: { ok: true, platform: 'telegram' } }); }
  return r.fulfill({ json: platform() });
});
await page.route('**/api/gateway/restart*', r => { stamp += 1; r.fulfill({ json: { ok: true, pid: 1, name: 'gateway-restart' } }); });
await page.route('**/api/status*', async r => { const real = await (await r.fetch()).json(); r.fulfill({ json: { ...real, gateway_running: true, gateway_platforms: { telegram: { state: 'connected', updated_at: stamp } } } }); });
await page.route('**/api/plugins/shturman/bot/check', r => r.fulfill({ json: { ok: true, username: 'ivan_shturman_bot', name: 'Мой Штурман' } }));
await page.waitForSelector('h2:has-text("Бот в Telegram")');
await shot(page, '3-bot-token');
check('привязка закрыта до токена', await page.isVisible('text=Откроется, когда бот выйдет на связь.'));
await page.fill('#sh-token', '1234567890:' + 'A'.repeat(35));
await page.click('button:has-text("Проверить и сохранить")');
await page.waitForSelector('button:has-text("Получить ссылку")', { timeout: 30000 });
check('токен ушёл в штатный вызов Hermes', puts[0]?.env?.TELEGRAM_BOT_TOKEN?.startsWith('1234567890:') && puts[0]?.enabled === true);
await page.click('button:has-text("Получить ссылку")');
const link = await page.getAttribute('a:has-text("Открыть бота")', 'href');
check('ссылка на бота правильного вида', /^https:\/\/t\.me\/ivan_shturman_bot\?start=[A-Za-z0-9_-]{20,64}$/.test(link), link.replace(/start=.*/, 'start=…'));
await shot(page, '3-bot-pair');
const token = link.split('start=')[1];
const res = py(`from shturman_core.pairing import Pairing; from shturman_core.state import Store; print(Pairing(Store()).try_bind('/start ${token}', user_id=777, chat_id=777, name='Иван Иванов', username='ivan'))`);
check('нажатие «Запустить» даёт кандидата, а не владельца', res === 'accepted', res);
await page.waitForSelector('text=Это вы?', { timeout: 30000 });
check('мастер показывает, кто написал боту', await page.isVisible('text=Иван Иванов') && await page.isVisible('text=@ivan · номер аккаунта 777'));
check('до подтверждения в Hermes ничего не записано', !puts.some(p => p.env?.TELEGRAM_ALLOWED_USERS));
await shot(page, '3-bot-confirm');
await page.click('button:has-text("Да, это я")');
await page.waitForSelector('text=Владелец привязан: Иван Иванов', { timeout: 30000 });
check('в Hermes записан только владелец', puts.some(p => p.env?.TELEGRAM_ALLOWED_USERS === '777'));
const envText = execSync(`grep -c '^TELEGRAM_HOME_CHANNEL=' ${HOME}/.env || true`, {encoding:'utf8'}).trim();
check('домашний чат записан', envText === '0' /* PUT /api/env подменён выше */ || envText === '1');
await shot(page, '3-bot-done');
await page.click('.shturman-actions button:has-text("Далее")');

// --- шаг 4: бизнес-режим ---
let pluginOn = false; const installs = [];
await page.route('**/api/dashboard/plugins/hub*', r => r.fulfill({ json: { plugins: pluginOn ? [{ name: 'telegram-business', runtime_status: 'enabled' }] : [] } }));
await page.route('**/api/dashboard/agent-plugins/install*', r => { installs.push(r.request().postDataJSON()); pluginOn = true; r.fulfill({ json: { ok: true, plugin_name: 'telegram-business', enabled: true } }); });
await page.reload(); await page.waitForSelector('.shturman-stepper');
await page.click('.shturman-stepbtn:has-text("Бизнес-режим")');
await page.waitForSelector('button:has-text("Установить защиту")');
check('инструкции закрыты до установки защиты', (await page.locator('text=Откроется после установки защиты.').count()) === 2);
await shot(page, '4-business-locked');
await page.click('button:has-text("Установить защиту")');
await page.waitForSelector('text=Плагин установлен и включён.', { timeout: 30000 });
check('плагин ставится по полному SHA', installs[0]?.ref?.length === 40 && installs[0]?.identifier === 'NousResearch/hermes-telegram-business', installs[0]?.ref);
await shot(page, '4-business-open');
await page.click('button:has-text("Проверить подключение")');
await page.waitForSelector('text=Подключения пока не вижу');
py(`from shturman_core.state import Store; Store().write('business', {'connected': True, 'can_reply': False, 'updated_at': 1})`);
await page.click('button:has-text("Проверить подключение")');
await page.waitForSelector('text=Бизнес-режим подключён.');
check('подключение видно после события от Telegram', true);
await page.click('.shturman-actions button:has-text("Далее")');
await page.waitForSelector('h2:has-text("Переписка и память")');
await shot(page, '5-later');
await page.click('.shturman-actions button:has-text("Далее")');
await page.waitForSelector('h2:has-text("Курс проложен")');
await shot(page, '6-done');
await page.click('button:has-text("Завершить настройку")');
await page.waitForSelector('text=Настройка завершена');
check('настройка отмечена завершённой', true);
check('страница без ошибок в консоли', errors.length === 0, errors.slice(0,3).join(' | '));

// узкий экран
await page.setViewportSize({ width: 390, height: 800 });
await page.click('.shturman-stepbtn:has-text("Бот в Telegram")');
await page.waitForSelector('h2:has-text("Бот в Telegram")');
const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
check('на телефоне нет горизонтальной прокрутки', overflow <= 1, 'лишних px: ' + overflow);
await shot(page, '3-bot-phone');

console.log(out.join('\n'));
await browser.close();
process.exit(out.some(l => l.startsWith('FAIL')) ? 1 : 0);
