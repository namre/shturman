-- Общие служебные таблицы сервиса: настройки, очередь заданий, бизнес-подключения.

CREATE TABLE settings (
    key         text PRIMARY KEY,
    value       jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Очередь заданий для исполнителя вне сервиса (плагин в Hermes): обращение к модели,
-- уведомление владельцу, отправка через бизнес-бота. Сервис сам к модели и к боту не ходит.
--   kind    — что должен сделать исполнитель (llm.structured, llm.text, notify.owner, business.send);
--   handler — кто в сервисе разберёт результат.
CREATE TABLE jobs (
    id            bigserial PRIMARY KEY,
    kind          text NOT NULL,
    handler       text,
    payload       jsonb NOT NULL DEFAULT '{}',
    -- данные сервиса для разбора результата; исполнителю не передаются
    context       jsonb NOT NULL DEFAULT '{}',
    status        text NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued', 'running', 'done', 'failed')),
    -- одинаковое задание второй раз не ставится (защита от повторных уведомлений и запросов)
    dedup_key     text,
    attempts      int  NOT NULL DEFAULT 0,
    max_attempts  int  NOT NULL DEFAULT 3,
    run_after     timestamptz NOT NULL DEFAULT now(),
    locked_until  timestamptz,
    worker        text,
    result        jsonb,
    error         text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz
);
CREATE UNIQUE INDEX jobs_dedup ON jobs (kind, dedup_key) WHERE dedup_key IS NOT NULL;
CREATE INDEX jobs_ready ON jobs (run_after) WHERE status IN ('queued', 'running');

-- Подключения бота в бизнес-режиме к аккаунту владельца (приходят от плагина).
CREATE TABLE business_connections (
    id          text PRIMARY KEY,
    account_id  bigint NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    can_reply   boolean NOT NULL DEFAULT false,
    enabled     boolean NOT NULL DEFAULT true,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
