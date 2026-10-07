-- Страница настройки сервиса (/shturman-setup/): вход, сессии, журнал действий владельца.
-- Отдельные таблицы, а не общая settings: ни один маршрут внутреннего API (/api/*) их не читает
-- и не меняет. Держатель токена API не может ни выдать себе ссылку входа, ни завести сессию,
-- ни подчистить журнал.

-- Одноразовая ссылка входа. Сама ссылка не хранится — только SHA-256 её значения.
-- Действующая ссылка всегда одна: новая стирает прежние (команда `shturman setup-link`).
CREATE TABLE setup_links (
    id          bigserial PRIMARY KEY,
    token_hash  text NOT NULL UNIQUE,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    used_at     timestamptz
);

-- Сессия браузера владельца. Хранится хеш случайного идентификатора; сам он — только в cookie.
CREATE TABLE setup_sessions (
    id            bigserial PRIMARY KEY,
    token_hash    text NOT NULL UNIQUE,
    -- как вошли: по одноразовой ссылке или по коду от бота согласований
    via           text NOT NULL CHECK (via IN ('link', 'code')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    -- предел жизни сессии от входа: продлением не сдвигается
    expires_at    timestamptz NOT NULL,
    revoked_at    timestamptz
);
CREATE INDEX setup_sessions_live ON setup_sessions (expires_at) WHERE revoked_at IS NULL;

-- Состояние входа по коду от бота: хеш кода, срок, счётчик неверных попыток, блокировка.
-- Ключ один — 'login_code'; таблица, а не столбцы, чтобы следующая настройка не требовала миграции.
CREATE TABLE setup_state (
    key         text PRIMARY KEY,
    value       jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Журнал действий со страницы настройки: когда, что, итог. Без секретов и без текстов переписки:
-- в detail — только то, что собрал код (числа, роли, признаки), не значения из запроса.
CREATE TABLE setup_audit (
    id       bigserial PRIMARY KEY,
    at       timestamptz NOT NULL DEFAULT now(),
    action   text NOT NULL,
    outcome  text NOT NULL DEFAULT 'ok' CHECK (outcome IN ('ok', 'refused', 'failed')),
    detail   text NOT NULL DEFAULT ''
);
CREATE INDEX setup_audit_at ON setup_audit (at DESC);
