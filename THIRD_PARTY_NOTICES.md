# Сторонние компоненты

Три вида стороннего в продукте: компоненты, которые скачиваются при развёртывании; библиотеки, от которых зависит сервис переписки; фрагменты чужого кода, переработанные и включённые в этот репозиторий. Лицензии указаны по состоянию на 2026-10-06, для добавленного в версии 0.0.4 (модель защиты, `socksio`) — на 2026-10-07, и подлежат сверке при каждом обновлении версии.

Правило: компоненты под AGPL, LGPL и BSL в поставку не включаются без отдельной записи в `docs/decisions.md`; код под GPL в репозиторий не переносится (решение Р-18).

## Компоненты, которые скачиваются при развёртывании

| Компонент | Роль | Лицензия | Источник |
|---|---|---|---|
| Hermes Agent | Агент и шлюз сообщений | MIT | https://github.com/NousResearch/hermes-agent |
| hermes-telegram-business | Плагин бизнес-режима; ставится из мастера по SHA | MIT | https://github.com/NousResearch/hermes-telegram-business |
| PostgreSQL 16 | Хранилище архива | PostgreSQL License | https://www.postgresql.org |
| pgvector 0.8.7 | Векторный поиск в Postgres | PostgreSQL License | https://github.com/pgvector/pgvector |
| text-embeddings-inference 1.9.4 | Сервер эмбеддингов и сервер классификатора защиты (оба по желанию) | Apache-2.0 | https://github.com/huggingface/text-embeddings-inference |
| intfloat/multilingual-e5-small, ревизия `614241f` | Модель эмбеддингов по умолчанию (по желанию) | MIT (по карточке модели) | https://huggingface.co/intfloat/multilingual-e5-small |
| deepvk/USER2-small, ревизия `23f65b3` | Вторая модель эмбеддингов, по выбору владельца; основа — `deepvk/RuModernBERT-small` (Apache-2.0) | Apache-2.0 (по карточке модели) | https://huggingface.co/deepvk/USER2-small |
| Horizon-Labs/prompt-injection-guard-small, ревизия `3215a27` | Модель-классификатор защиты от внедрённых инструкций (по желанию); основа — `jhu-clsp/mmBERT-small` (MIT) | Apache-2.0 (по карточке модели; файла лицензии в репозитории модели нет) | https://huggingface.co/Horizon-Labs/prompt-injection-guard-small |
| Python 3.12, образ `python:3.12.12-slim-bookworm` | Основа образа сервиса переписки | PSF License; пакеты Debian — под своими лицензиями | https://hub.docker.com/_/python |
| git | История страниц памяти; ставится в образ сервиса как отдельная программа | GPL-2.0 | https://git-scm.com |

Модели эмбеддингов скачивает `./ops/embeddings.sh` — только ту, которую выбрал владелец, — по закреплённым ревизиям `614241f622f53c4eeff9890bdc4f31cfecc418b3` и `23f65b34cf7632032061f5cc66c14714e6d4cee4` с проверкой контрольной суммы каждого файла; в репозитории и в образе сервиса их нет. Лицензии сверены с карточками моделей на huggingface.co 2026-10-07.

Модель защиты скачивает `./ops/guard.sh` по закреплённой ревизии `3215a27edd62c5ba0bd786c57a9d243b2158e70e` с проверкой контрольных сумм; в репозитории и в образе сервиса её нет. Лицензия Apache-2.0 названа только в карточке модели: файла лицензии в её репозитории нет, автор малоизвестен — при обновлении ревизии сверить заново.

git вызывается как отдельная программа и с кодом сервиса не связывается; без него страницы собираются без истории. Python-библиотека python-telegram-bot (LGPL-3.0) приходит вместе с Hermes, в наш образ и в плагин не входит (решение Р-28).

## Библиотеки сервиса переписки

Прямые зависимости из `service/pyproject.toml`. Точные версии всех пакетов, включая транзитивные, — в `service/requirements.lock`.

| Библиотека | Роль | Лицензия | Источник |
|---|---|---|---|
| asyncpg | Доступ к базе | Apache-2.0 | https://github.com/MagicStack/asyncpg |
| ijson | Потоковое чтение экспорта | BSD-3-Clause | https://github.com/ICRAR/ijson |
| starlette | Каркас HTTP | BSD-3-Clause | https://pypi.org/project/starlette/ |
| uvicorn | Сервер HTTP | BSD-3-Clause | https://pypi.org/project/uvicorn/ |
| httpx | Клиент сервера эмбеддингов, классификатора защиты, Bot API и модели | BSD-3-Clause | https://pypi.org/project/httpx/ |
| socksio | Поддержка SOCKS-прокси для httpx | MIT | https://pypi.org/project/socksio/ |
| mcp | SDK протокола MCP, сервер архива | MIT | https://pypi.org/project/mcp/ |
| pydantic | Модели ответов инструментов (приходит с `mcp`) | MIT | https://pypi.org/project/pydantic/ |
| Telethon 1.x | Клиент пользовательского аккаунта Telegram | MIT | https://codeberg.org/Lonami/Telethon |
| segno | QR-код для входа | BSD-3-Clause | https://pypi.org/project/segno/ |
| pymorphy3 | Начальные формы русских слов | MIT | https://pypi.org/project/pymorphy3/ |
| pymorphy3-dicts-ru | Словари для pymorphy3 (приходят с ним) | **Требует повторной сверки** | https://pypi.org/project/pymorphy3-dicts-ru/ |
| pytrovich | Падежи имён, отчеств и фамилий | MIT | https://pypi.org/project/pytrovich/ |
| rapidfuzz | Похожесть строк | MIT | https://pypi.org/project/rapidfuzz/ |
| dateparser | Запасной разбор сроков вида «через …», когда свои правила не справились | BSD-3-Clause | https://pypi.org/project/dateparser/ |
| regex | Выражения наблюдателя с пределом времени | Apache-2.0 | https://pypi.org/project/regex/ |
| python-socks | Исходящий прокси для Telegram | Apache-2.0 | https://pypi.org/project/python-socks/ |
| tzdata | Часовые пояса | Apache-2.0 | https://pypi.org/project/tzdata/ |

Лицензии транзитивных зависимостей из `service/requirements.lock` по одной не сверялись.

Плагин `shturman` своих зависимостей не привозит: его логика написана на стандартной библиотеке Python, остальное он берёт из окружения Hermes.

## Переработанные фрагменты чужого кода в этом репозитории

В каждом файле источник назван в шапке строкой «Основано на …» с файлом и коммитом.

| Проект | Лицензия | Что взято | Где у нас |
|---|---|---|---|
| VsevaTech/promise-tracker | MIT, © 2026 VsevaTech | Ход разбора сроков; инструкция модели; основы глаголов-обещаний и оговорок; проверка «срок есть в исходном тексте» | `service/src/shturman/processing/dates.py` (полный текст лицензии — в шапке файла), `processing/extract.py` |
| NousResearch/hermes-telegram-business | MIT | Жизненный цикл черновика, разбор нажатий и клавиатура; сбор «пачки» сообщений | `service/src/shturman/outbox/drafts.py`, `outbox/autoreply.py` |
| NousResearch/hermes-agent | MIT | Порядок сборки запроса «JSON по схеме» и разбор ответа модели | `service/src/shturman/executor/llm.py` |
| Luan-X/hermes-telegram-business | MIT | Нормализация текста, отпечаток содержимого, разбор ответа модели | `service/src/shturman/outbox/watcher.py`, `outbox/text.py` |
| paulpierre/informer, aahnik/tgcf | MIT | Образец цикла «чат из списка → слова → уведомление» | `service/src/shturman/outbox/watcher.py` |
| j2h4u/mcp-telegram (форк sparfenyuk/mcp-telegram) | MIT | Порядок запросов и курсоры синхронизации; состав обработчиков событий; разбор вложений и пересылок; рамка «чужой текст»; связка «поиск → окно вокруг найденного»; запас времени QR-кода | `service/src/shturman/tg/sync.py`, `tg/live.py`, `tg/normalize.py`, `tg/qr.py`, `sanitize.py`, `mcp_server.py`; через `sanitize.py` — `plugins/shturman/shturman_core/tools.py` |
| chigwell/telegram-mcp | Apache-2.0 | Порядок чистки чужого текста; блокировка файла сессии; разбор сообщений | `service/src/shturman/sanitize.py`, `tg/lock.py`, `tg/normalize.py`; через `sanitize.py` — `plugins/shturman/shturman_core/tools.py` |
| leshchenko1979/fast-mcp-telegram | MIT | Набор состояний входа по QR | `service/src/shturman/tg/qr.py` |
| kawaiiDango/telegram-delete-logger | Apache-2.0 | Поиск удалённого сообщения без указания чата | `service/src/shturman/tg/live.py`, `store.py` |
| mukhanov/telemcp | MIT | Формулировки описаний инструментов, состав полей чата и фильтры | `service/src/shturman/mcp_server.py` |
| pgvector/pgvector-python | MIT | Слияние рангов двух веток поиска | `service/src/shturman/search.py` |
| Telethon | MIT | Преобразование коротких обновлений | `service/src/shturman/tg/normalize.py` |

Только замысел, без кода (названы в шапках файлов): getzep/graphiti (Apache-2.0), nearai/ironclaw (MIT / Apache-2.0), garrytan/gbrain (MIT), скилл `llm-wiki` из NousResearch/hermes-agent (MIT), tolboy/telegram-mcp-tdlib (Apache-2.0), Prgebish/mcp-telegram (MIT).

Только справочник формата, без кода: исходный код экспортёра Telegram Desktop (GPL-3.0) — по нему сверены имена полей и значений экспорта. Фрагменты из него не заимствованы.
