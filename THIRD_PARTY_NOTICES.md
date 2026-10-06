# Сторонние компоненты

Репозиторий не включает код сторонних компонентов; они скачиваются при развёртывании. Лицензии указаны по состоянию на 2026-10-06 и подлежат сверке при каждом обновлении версии.

| Компонент | Роль | Лицензия | Источник |
|---|---|---|---|
| Hermes Agent | Агент и шлюз сообщений | MIT | https://github.com/NousResearch/hermes-agent |
| hermes-telegram-business | Основа бизнес-плагина (форк ведётся отдельным репозиторием) | MIT | https://github.com/NousResearch/hermes-telegram-business |
| Telethon 1.x | Клиент пользовательского аккаунта Telegram | MIT | https://codeberg.org/Lonami/Telethon |
| PostgreSQL, pgvector | Хранилище и векторный поиск | PostgreSQL License | https://www.postgresql.org · https://github.com/pgvector/pgvector |
| bge-m3 | Локальная модель эмбеддингов | MIT | https://huggingface.co/BAAI/bge-m3 |
| tg-parser | Основа импорта экспорта Telegram Desktop | MIT | https://github.com/mdemyanov/tg-parser |
| GigaAM-v3 | Распознавание русской речи | MIT | https://github.com/salute-developers/GigaAM |
| text-embeddings-inference | Сервер эмбеддингов | Apache-2.0 | https://github.com/huggingface/text-embeddings-inference |

Правило: компоненты под AGPL, LGPL и BSL в поставку не включаются без отдельной записи в `docs/decisions.md`.
