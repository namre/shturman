-- Сессии пользовательских аккаунтов Telegram (Telethon) и синхронизация чатов.
--
-- Сама сессия (ключ авторизации) лежит в файле в каталоге данных сервиса, не в базе.
-- Здесь — только то, что нужно, чтобы знать, какой аккаунт занимает роль и что из него брать.

-- Сессия аккаунта. На роль — одна сессия; файл сессии называется <slot>.session.
CREATE TABLE tg_sessions (
    account_id      bigint PRIMARY KEY REFERENCES accounts (id) ON DELETE CASCADE,
    -- owner: основной аккаунт владельца, только чтение; assistant: аккаунт-помощник
    slot            text NOT NULL UNIQUE CHECK (slot IN ('owner', 'assistant')),
    -- владелец поставил аккаунт на паузу: сервис к Telegram от его имени не подключается
    paused          boolean NOT NULL DEFAULT false,
    -- брать ли без спроса чаты, которых не было, когда настройку включили; по умолчанию — нет
    auto_personal   boolean NOT NULL DEFAULT false,
    auto_groups     boolean NOT NULL DEFAULT false,
    logged_in_at    timestamptz NOT NULL DEFAULT now(),
    last_started_at timestamptz,
    last_error      text
);

-- Что известно сервису о чате аккаунта с точки зрения синхронизации. Строка без chat_id —
-- чат, который сервис видел, но владелец не выбирал: из него ничего не хранится, даже название.
CREATE TABLE tg_sync_chats (
    account_id          bigint NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    peer_class          text   NOT NULL CHECK (peer_class IN ('user', 'chat', 'channel')),
    tg_id               bigint NOT NULL,
    chat_id             bigint REFERENCES chats (id) ON DELETE CASCADE,
    enabled             boolean NOT NULL DEFAULT false,
    -- включён не владельцем, а настройкой «брать новые чаты»
    auto_enabled        boolean NOT NULL DEFAULT false,
    enabled_at          timestamptz,
    -- загрузка истории вглубь: наименьший уже сохранённый идентификатор сообщения.
    -- Пишется в той же транзакции, что и страница сообщений, поэтому продолжается точно с места.
    backfill_before     bigint,
    backfill_done       boolean NOT NULL DEFAULT false,
    -- дозагрузка вперёд: наибольший идентификатор, до которого история прочитана подряд.
    -- Двигается только чтением истории, не живыми событиями: событие могло потеряться.
    forward_id          bigint,
    gap_checked_at      timestamptz,
    -- последняя сверка удалений
    reconciled_at       timestamptz,
    -- доступ к чату потерян (вышли, исключили, канал закрыт): чат не опрашивается до повторного включения
    access_lost_at      timestamptz,
    access_lost_reason  text,
    last_error          text,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, peer_class, tg_id),
    CHECK (NOT enabled OR chat_id IS NOT NULL)
);
CREATE INDEX tg_sync_chats_enabled ON tg_sync_chats (account_id) WHERE enabled;
CREATE UNIQUE INDEX tg_sync_chats_chat ON tg_sync_chats (chat_id) WHERE chat_id IS NOT NULL;
