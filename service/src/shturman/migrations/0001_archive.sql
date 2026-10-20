-- Архив переписки. Сообщения хранятся дословно; всё производное строится поверх.
--
-- Ключевая идея: «аккаунт» — это точка зрения, с которой пронумерованы чаты и сообщения.
-- Экспорт основного аккаунта, бизнес-подключение того же аккаунта и его же сессия на чтение
-- дают одни и те же идентификаторы, поэтому складываются в одни строки без дублей.
-- Дополнительный аккаунт — другая точка зрения и другие строки.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE accounts (
    id          bigserial PRIMARY KEY,
    tg_user_id  bigint      NOT NULL UNIQUE,
    label       text        NOT NULL,
    -- owner: основной аккаунт владельца; assistant: дополнительный аккаунт-помощник
    role        text        NOT NULL CHECK (role IN ('owner', 'assistant')),
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Собеседники и чаты как сущности Telegram. Идентификаторы Telegram глобальны
-- внутри своего класса, поэтому таблица общая для всех аккаунтов.
CREATE TABLE peers (
    id          bigserial PRIMARY KEY,
    -- user: человек или бот; chat: обычная группа; channel: супергруппа или канал
    class       text   NOT NULL CHECK (class IN ('user', 'chat', 'channel')),
    tg_id       bigint NOT NULL,
    name        text,
    username    text,
    is_bot      boolean,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (class, tg_id)
);
CREATE INDEX peers_name_trgm ON peers USING gin (name gin_trgm_ops);

CREATE TABLE chats (
    id          bigserial PRIMARY KEY,
    account_id  bigint NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    peer_id     bigint NOT NULL REFERENCES peers (id),
    -- тип, как его называет Telegram: personal_chat, bot_chat, saved_messages,
    -- private_group, private_supergroup, public_supergroup, private_channel, public_channel
    type        text   NOT NULL,
    title       text,
    -- исключённый чат: сообщения из него в архив не принимаются ни из одного источника
    excluded    boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_id, peer_id)
);

CREATE TABLE messages (
    id                 bigserial PRIMARY KEY,
    chat_id            bigint NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    tg_message_id      bigint NOT NULL,
    sent_at            timestamptz NOT NULL,
    kind               text NOT NULL DEFAULT 'message' CHECK (kind IN ('message', 'service')),
    sender_peer_id     bigint REFERENCES peers (id),
    sender_name        text,
    is_outgoing        boolean,
    text               text NOT NULL DEFAULT '',
    entities           jsonb,
    reply_to_tg_id     bigint,
    forwarded_from     text,
    edited_at          timestamptz,
    media_type         text,
    media_path         text,
    service_action     text,
    -- откуда сообщение известно: import, business, session
    sources            text[] NOT NULL,
    deleted_at         timestamptz,
    first_seen_at      timestamptz NOT NULL DEFAULT now(),
    fts                tsvector GENERATED ALWAYS AS (to_tsvector('russian', text)) STORED,
    UNIQUE (chat_id, tg_message_id)
);
CREATE INDEX messages_fts ON messages USING gin (fts);
CREATE INDEX messages_chat_time ON messages (chat_id, sent_at);
CREATE INDEX messages_sender_time ON messages (sender_peer_id, sent_at);

-- Прежние версии текста. Пополняется, когда приходит то же сообщение с другим текстом.
CREATE TABLE message_versions (
    id           bigserial PRIMARY KEY,
    message_id   bigint NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
    text         text NOT NULL,
    edited_at    timestamptz,
    replaced_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX message_versions_message ON message_versions (message_id);

CREATE TABLE imports (
    id           bigserial PRIMARY KEY,
    account_id   bigint NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    source_name  text NOT NULL,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    stats        jsonb
);
