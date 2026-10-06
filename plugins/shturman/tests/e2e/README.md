# Браузерная проверка плагина

Три сценария на Playwright против настоящего Hermes за Caddy. В CI не запускаются: им нужен установленный Hermes и собранный дашборд. Запускать вручную после изменений входа, страниц входа или мастера.

| Файл | Что проверяет |
|---|---|
| `auth.mjs` | Вход по ссылке активации, одноразовость ссылки, закрытость API без сессии |
| `login.mjs` | Страница ввода кода: сбой отправки, формат ввода, узкий экран |
| `wizard.mjs` | Все шаги мастера. Ответы провайдера модели, Telegram и перезапуск шлюза подменяются |

## Стенд

1. Hermes той версии, для которой написан плагин (`git clone --branch v2026.9.24`), установленный в отдельное окружение Python: `pip install -e 'hermes-agent[web,messaging]'`.
2. Пустой каталог `HERMES_HOME`; в нём `plugins/shturman` — ссылка на этот плагин, а в `config.yaml` — `plugins: {enabled: [shturman]}`.
3. Имя `shturman.test`, указывающее на `127.0.0.1`.
4. Caddy с настройкой из `config/Caddyfile.example`: адрес `http://shturman.test:8080`, каталог страниц входа — `plugins/shturman/public` этого репозитория.
5. Дашборд: `HERMES_DASHBOARD_PUBLIC_URL=http://shturman.test:8080 hermes dashboard --host 127.0.0.1 --port 9119 --no-open`.

## Запуск

Нужны Node.js и Playwright с Chromium (`npm install playwright` в этом каталоге; `node_modules` в git не попадает).

```
export HERMES_HOME=/путь/к/каталогу/стенда
export E2E_PYTHON=/путь/к/окружению/bin/python
export E2E_OUT=/куда/сохранять/снимки
node auth.mjs && node wizard.mjs && node login.mjs
```

Порядок важен: `login.mjs` рассчитывает, что владелец уже привязан — это делает `wizard.mjs`. Каждый сценарий печатает строки `PASS`/`FAIL` и завершается с кодом 1, если есть `FAIL`.

`auth.mjs` и `wizard.mjs` в начале удаляют состояние плагина в `HERMES_HOME/plugin-data/shturman`. На рабочем экземпляре не запускать.
