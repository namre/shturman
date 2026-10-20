-- Граница выдачи архива (Р-104): журнал выдачи без текста, состояние «притормозить / закрыть», тревоги и ловушки.
-- docs/service.md, «Граница выдачи архива». Ни текста сообщений, ни названий чатов здесь нет.
--
-- archive_disclosures — что, когда и сколько выдано по токенам, видимым ассистенту: инструмент MCP или
-- маршрут внутреннего API (шаблон без чисел), число сообщений и знаков, архивные номера чатов (не названия),
-- вид перебора (страницы, даты, буквы), выдана ли ловушка, отказ. Строка заводится ДО выдачи с запасом
-- (pending = true) и уточняется после: параллельные запросы не обходят пределы. Хранится 30 дней.
CREATE TABLE archive_disclosures (
    id        bigserial PRIMARY KEY,
    at        timestamptz NOT NULL DEFAULT now(),
    caller    text NOT NULL CHECK (caller IN ('mcp', 'remote', 'hermes')),
    tool      text NOT NULL,
    pending   boolean NOT NULL DEFAULT true,
    messages  integer NOT NULL DEFAULT 0,
    chars     integer NOT NULL DEFAULT 0,
    chats     bigint[] NOT NULL DEFAULT '{}',
    pattern   text CHECK (pattern IN ('page', 'dates', 'alpha')),
    canary    boolean NOT NULL DEFAULT false,
    refused   text
);
CREATE INDEX archive_disclosures_caller_at ON archive_disclosures (caller, at);
CREATE INDEX archive_disclosures_at ON archive_disclosures (at);

-- Одна строка: выдача открыта, приторможена до решения владельца или закрыта им (или ловушкой).
CREATE TABLE archive_guard (
    id           integer PRIMARY KEY CHECK (id = 1),
    state        text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'slowed', 'closed')),
    reason       text,                 -- volume | chats | walk | limit | canary | owner
    since        timestamptz,
    nonce        text,                 -- метка кнопок последнего сообщения владельцу
    quiet_until  timestamptz,          -- «Это я, продолжить»: сутки без новых тревог (пределы действуют)
    weekly_sent  text,                 -- неделя (ГГГГ-Ннн) последней недельной сводки
    updated_at   timestamptz NOT NULL DEFAULT now()
);
INSERT INTO archive_guard (id) VALUES (1);

-- Тревоги: причина, числа (без текста и названий) и решение владельца. Для недельной сводки и doctor.
CREATE TABLE archive_alarms (
    id          bigserial PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    caller      text NOT NULL,
    reason      text NOT NULL,
    detail      jsonb NOT NULL DEFAULT '{}',
    decision    text CHECK (decision IN ('continue', 'close', 'open')),
    decided_at  timestamptz
);

-- Ловушки: ненастоящие чаты, которые существуют только в выдаче архива по токену (MCP). В таблицах chats и
-- messages их нет, поэтому ни списки чатов владельца, ни сводки, ни поиск приложения, ни обработка их не видят.
CREATE TABLE archive_canaries (
    id               bigserial PRIMARY KEY,
    title            text NOT NULL,
    bait             text NOT NULL,          -- редкое слово: поиск по нему находит ловушку
    texts            text[] NOT NULL,
    dates            timestamptz[] NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    hits             integer NOT NULL DEFAULT 0,
    last_hit_at      timestamptz
);
