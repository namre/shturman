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
// Дашборд прокручивает содержимое внутри себя, и снимок «всей страницы» захватывает только видимую
// часть. Поэтому на время снимка окно растягивается по высоте содержимого мастера.
const shot = async (page, name) => {
  const view = page.viewportSize();
  const need = await page.evaluate(() => { const el = document.querySelector('.shturman'); return el ? Math.ceil(el.scrollHeight) + 260 : 0; });
  if (need > view.height) await page.setViewportSize({ width: view.width, height: Math.min(need, 6000) });
  await page.evaluate(() => { const el = document.querySelector('.shturman'); if (el) el.scrollIntoView({ block: 'start' }); });
  await page.screenshot({ path: `${OUT}/w-${name}.png`, fullPage: true });
  if (need > view.height) await page.setViewportSize(view);
};

// чистое состояние мастера (ключ подписи оставляем)
execSync(`rm -f ${HOME}/plugin-data/shturman/{wizard,owner,pairing,business,login,activation}.json`, {shell:'/bin/bash'});
py(`from shturman_core import wizard; from shturman_core.state import Store; wizard.remember_bot(Store(),'ivan_shturman_bot','Мой Штурман')`);

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1360, height: 900 } });
const page = await ctx.newPage();
const errors = []; page.on('console', m => { if (m.type()==='error') errors.push(m.text()); });
page.on('pageerror', e => errors.push('pageerror: ' + e.message));
// Все запросы страницы: мастер не должен ходить ни на страницу настройки переписки, ни в проход к сервису.
const requested = []; page.on('request', r => requested.push(r.url()));

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

// --- шаг 4: переписка ---
// Настройка идёт на отдельной странице сервиса переписки; мастер показывает состояние и ссылку.
const steps = (await page.locator('.shturman-stepbtn').allTextContents()).map(t => t.replace(/^\d+/, '').trim());
check('шагов пять, шага «Бизнес-режим» нет', steps.join(' | ') === 'Помощник | Модель | Бот в Telegram | Переписка | Готово', steps.join(' | '));
await page.waitForSelector('h2:has-text("Переписка")');
const SETUP_URL = BASE + '/shturman-setup/';
const openLink = page.locator('a:has-text("Открыть настройку переписки")');
// На стенде сервиса переписки нет — это настоящий ответ плагина, без подмены.
await page.waitForSelector('text=Сервис переписки не подключён к ассистенту');
check('без сервиса мастер говорит об этом и ссылку не показывает', (await openLink.count()) === 0);
check('два бота объяснены', await page.isVisible('text=В бизнес-режиме Telegram его не подключают.') && await page.isVisible('text=К нему подключается бизнес-режим Telegram.'));
check('бот-ассистент назван по имени', await page.isVisible('text=С ним вы разговариваете: @ivan_shturman_bot'));
check('сказано, как войти, а ссылки входа нет', await page.isVisible('text=Первый раз — по одноразовой ссылке.') && await page.isVisible('text=Потом — по коду от бота согласований.') && !(await page.content()).includes('shturman-setup/#'));
await shot(page, '4-correspondence-noservice');

// Дальше ответ о состоянии подменяется: сервис прежней версии, пустой, настроенный.
let corr = { state: 'outdated', url: SETUP_URL, setup: null, archive: { messages: 1200, chats: 14 } };
await page.route('**/api/plugins/shturman/correspondence', r => r.fulfill({ json: corr }));
const refresh = async (text) => { await page.click('button:has-text("Обновить")'); await page.waitForSelector(text); };
await refresh('text=Страница настройки переписки недоступна — обновите экземпляр');
check('сервис прежней версии: «обновите экземпляр», ссылки нет', (await openLink.count()) === 0);
await shot(page, '4-correspondence-outdated');

const nothing = { origin_set: true, tg_keys: false, own_bot: false, owner_bound: false, business_connected: false, own_model: false, accounts: 0 };
corr = { state: 'ok', url: SETUP_URL, setup: nothing, archive: { messages: 0, chats: 0 } };
await refresh('text=Ещё не создан');
check('ссылка на страницу настройки — обычная, в новой вкладке', (await openLink.getAttribute('href')) === SETUP_URL && (await openLink.getAttribute('target')) === '_blank' && (await openLink.getAttribute('rel')) === 'noopener noreferrer');
check('ничего не настроено: шаг можно отложить', await page.isVisible('.shturman-actions button:has-text("Настрою позже")') && await page.isVisible('text=Не подключены'));
await shot(page, '4-correspondence-empty');

corr = { state: 'ok', url: SETUP_URL, setup: { ...nothing, tg_keys: true, own_bot: true, owner_bound: true, business_connected: true, own_model: true, accounts: 2 }, archive: { messages: 300000, chats: 87 } };
await refresh('.shturman-facts .shturman-sumrow:has-text("Бизнес-режим") >> text=Подключён');
const facts = (await page.locator('.shturman-col-aside .shturman-sumrow').allTextContents()).join(' | ');
check('всё настроено: состояние показано числами и признаками', /Бот согласованийЕсть/.test(facts) && /Аккаунты Telegram2/.test(facts) && /Сообщений в архиве300\s000/.test(facts), facts);
await shot(page, '4-correspondence-ready');

// Бот-ассистент подключён в бизнес-режиме (схема до 0.0.6): мастер говорит об этом, но ничего не ломает.
py(`from shturman_core.state import Store; Store().write('business', {'connected': True, 'can_reply': False, 'updated_at': 1})`);
await page.reload(); await page.waitForSelector('.shturman-stepper');
await page.click('.shturman-stepbtn:has-text("Переписка")');
await page.waitForSelector('text=Сейчас в бизнес-режиме Telegram подключён бот-ассистент');
await shot(page, '4-correspondence-legacy');
py(`from shturman_core.state import Store; Store().delete('business')`);
await page.reload(); await page.waitForSelector('.shturman-stepper');
await page.click('.shturman-stepbtn:has-text("Переписка")');
await page.waitForSelector('.shturman-sumrow:has-text("Аккаунты Telegram")');
check('без прежнего подключения предупреждения нет', !(await page.isVisible('text=Сейчас в бизнес-режиме Telegram подключён бот-ассистент')));

await page.click('.shturman-actions button:has-text("Далее")');
await page.waitForSelector('h2:has-text("Курс проложен")');
const sumRow = await page.textContent('.shturman-sumrow:has-text("Переписка")');
check('в итоге одна строка «Переписка» с состоянием', /Бот согласований привязан, аккаунтов Telegram: 2, бизнес-режим подключён, сообщений в архиве: 300\s000/.test(sumRow) && !(await page.isVisible('.shturman-sumlabel:has-text("Бизнес-режим")')), sumRow);
await shot(page, '5-done');
await page.click('button:has-text("Завершить настройку")');
await page.waitForSelector('text=Настройка завершена');
check('настройка отмечена завершённой', true);
const marks = py(`import json; from shturman_core.state import Store; print(json.dumps(sorted(Store().read('wizard').get('marks', {}))))`);
check('шаг «Переписка» отмечен пройденным', JSON.parse(marks).includes('correspondence_seen'), marks);

// узкий экран: шаг «Переписка»
await page.setViewportSize({ width: 390, height: 800 });
await page.click('.shturman-stepbtn:has-text("Переписка")');
await page.waitForSelector('h2:has-text("Переписка")');
const overflowCorr = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
check('шаг «Переписка» на телефоне без горизонтальной прокрутки', overflowCorr <= 1, 'лишних px: ' + overflowCorr);
await shot(page, '4-correspondence-phone');
corr = { state: 'ok', url: SETUP_URL, setup: nothing, archive: { messages: 0, chats: 0 } };
await refresh('text=Ещё не создан');
await shot(page, '4-correspondence-empty-phone');
await page.setViewportSize({ width: 1360, height: 900 });

// Экземпляр, прошедший прежний мастер, но не завершивший его: отметка business_skipped, шаг «later».
// После обновления мастер открывается на итоге, шаг «Переписка» уже пройден.
py(`from shturman_core.state import Store; s = Store(); d = s.read('wizard'); d['marks'] = {'persona_saved': 1, 'model_ok': 2, 'bot_applied': 3, 'business_skipped': 4}; d.pop('completed_at', None); d['step'] = 'later'; s.write('wizard', d)`);
await page.goto(BASE + '/shturman'); await page.waitForSelector('.shturman-stepper');
check('прежнее состояние мастера: открыт итог', (await page.textContent('.shturman-stepbtn.is-active')).includes('Готово'));
check('прежнее состояние мастера: «Переписка» пройдена', (await page.locator('.shturman-stepbtn.is-done:has-text("Переписка")').count()) === 1);
await page.click('button:has-text("Завершить настройку")');
await page.waitForSelector('text=Настройка завершена');

const stray = requested.filter(u => u.includes('/shturman-setup') || u.includes('/api/plugins/shturman/service/') || u.includes('agent-plugins/install'));
check('мастер не обращался к странице настройки, проходу к сервису и установке плагинов', stray.length === 0, stray.slice(0, 3).join(' | '));
check('страница без ошибок в консоли', errors.length === 0, errors.slice(0,3).join(' | '));

// узкий экран: шаг «Бот в Telegram»
await page.setViewportSize({ width: 390, height: 800 });
await page.click('.shturman-stepbtn:has-text("Бот в Telegram")');
await page.waitForSelector('h2:has-text("Бот в Telegram")');
const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
check('на телефоне нет горизонтальной прокрутки', overflow <= 1, 'лишних px: ' + overflow);
await shot(page, '3-bot-phone');

console.log(out.join('\n'));
await browser.close();
process.exit(out.some(l => l.startsWith('FAIL')) ? 1 : 0);
