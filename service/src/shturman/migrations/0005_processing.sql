-- Обработка архива: реестр людей и обязательства.
--
-- Источник истины для людей и обязательств — эти таблицы (docs/memory.md); страницы памяти
-- строятся из них. Всё, что выведено из сообщений, ссылается на строки архива (messages.id)
-- с ON DELETE CASCADE: исчезло сообщение — исчезло и выведенное из него.

-- ---------------------------------------------------------------------------
-- Люди
-- ---------------------------------------------------------------------------

-- Человек. peers — это учётные записи Telegram; у человека их может быть несколько.
CREATE TABLE people (
    id            bigserial PRIMARY KEY,
    display_name  text NOT NULL,
    first_name    text,
    middle_name   text,
    last_name     text,
    gender        text CHECK (gender IN ('m', 'f')),
    is_owner      boolean NOT NULL DEFAULT false,
    -- auto: заведён по имени из Telegram; owner: заведён или подтверждён владельцем
    origin        text NOT NULL DEFAULT 'auto' CHECK (origin IN ('auto', 'owner')),
    -- подтверждён владельцем (правил алиасы, сливал, подтвердил явно); страницу можно вести без вопроса
    confirmed     boolean NOT NULL DEFAULT false,
    -- запись влита в другую: идентификатор сохраняется, чтобы старые ссылки вели к новой записи
    merged_into   bigint REFERENCES people (id) ON DELETE SET NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX people_one_owner ON people (is_owner) WHERE is_owner;

-- Учётная запись Telegram принадлежит ровно одному человеку.
CREATE TABLE person_peers (
    peer_id    bigint PRIMARY KEY REFERENCES peers (id) ON DELETE CASCADE,
    person_id  bigint NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    origin     text NOT NULL DEFAULT 'auto' CHECK (origin IN ('auto', 'owner')),
    linked_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX person_peers_person ON person_peers (person_id);

-- Как человека называют. telegram: имя или адрес учётной записи (peer_id — чьё);
-- owner: подтвердил владелец; auto: выведено автоматически.
CREATE TABLE person_aliases (
    id          bigserial PRIMARY KEY,
    person_id   bigint NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    alias       text NOT NULL,
    -- ключ сравнения: нижний регистр, ё->е, й->и, латиница -> кириллица
    alias_norm  text NOT NULL,
    origin      text NOT NULL CHECK (origin IN ('telegram', 'owner', 'auto')),
    peer_id     bigint REFERENCES peers (id) ON DELETE CASCADE,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (person_id, alias_norm)
);
CREATE INDEX person_aliases_trgm ON person_aliases USING gin (alias_norm gin_trgm_ops);

-- Указатель словоформ: строится из реестра вперёд (все падежи имени, отчества, фамилии,
-- уменьшительные, алиасы). По нему упоминание «Ивану Иванычу» находит человека.
CREATE TABLE person_forms (
    form       text NOT NULL,
    person_id  bigint NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    slot       text NOT NULL CHECK (slot IN ('first', 'middle', 'last', 'alias', 'username')),
    PRIMARY KEY (form, person_id, slot)
);
CREATE INDEX person_forms_person ON person_forms (person_id);

-- Предложения владельцу. Слияние двух записей само не происходит никогда.
CREATE TABLE person_proposals (
    id               bigserial PRIMARY KEY,
    kind             text NOT NULL DEFAULT 'merge' CHECK (kind IN ('merge')),
    person_id        bigint NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    other_person_id  bigint NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    score            int NOT NULL,
    status           text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'rejected')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    decided_at       timestamptz,
    CHECK (person_id <> other_person_id)
);
-- одна пара предлагается один раз: отклонённое повторно не всплывает
CREATE UNIQUE INDEX person_proposals_pair
    ON person_proposals (kind, LEAST(person_id, other_person_id), GREATEST(person_id, other_person_id));

-- ---------------------------------------------------------------------------
-- Прогоны обработки
-- ---------------------------------------------------------------------------

CREATE TABLE processing_runs (
    id           bigserial PRIMARY KEY,
    trigger      text NOT NULL CHECK (trigger IN ('manual', 'nightly')),
    status       text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done')),
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    -- только счётчики, без текста переписки
    stats        jsonb NOT NULL DEFAULT '{}'
);
-- одновременно идёт не больше одного прогона
CREATE UNIQUE INDEX processing_runs_one_running ON processing_runs (status) WHERE status = 'running';

-- Запросы к модели, поставленные прогоном: по ним видно, когда прогон закончился.
CREATE TABLE processing_requests (
    job_id      bigint PRIMARY KEY REFERENCES jobs (id) ON DELETE CASCADE,
    run_id      bigint NOT NULL REFERENCES processing_runs (id) ON DELETE CASCADE,
    kind        text NOT NULL CHECK (kind IN ('extract', 'resolve')),
    chat_id     bigint NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    state       text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'done', 'failed')),
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX processing_requests_pending ON processing_requests (run_id) WHERE state = 'pending';

-- ---------------------------------------------------------------------------
-- Обязательства
-- ---------------------------------------------------------------------------

CREATE TABLE commitments (
    id                 bigserial PRIMARY KEY,
    chat_id            bigint NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    -- сообщение, в котором дано обещание
    source_message_id  bigint NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
    -- сообщение, из которого взята формулировка срока (может быть другим: «пришлите до пятницы» — «хорошо»)
    due_message_id     bigint REFERENCES messages (id) ON DELETE CASCADE,
    -- кто обещал и кому; учётные записи Telegram, человек находится через person_peers
    debtor_peer_id     bigint REFERENCES peers (id) ON DELETE SET NULL,
    creditor_peer_id   bigint REFERENCES peers (id) ON DELETE SET NULL,
    -- относительно владельца: он должен / ему должны / между другими людьми
    direction          text NOT NULL CHECK (direction IN ('owner_owes', 'owed_to_owner', 'others')),
    what               text NOT NULL,
    -- дословный фрагмент исходного сообщения
    source_quote       text NOT NULL,
    -- формулировка срока дословно; дату считает код (processing/dates.py)
    due_expression     text,
    due_date           date,
    due_time           time,
    due_part           text,
    -- код причины из dates.Reason: ok, no_deadline, ambiguous_period, ...
    due_reason         text NOT NULL DEFAULT 'no_deadline',
    -- proposed: ждёт решения владельца; open: принято; done: выполнено; cancelled: отменено;
    -- rejected: владелец отклонил предложение; expired: предложение осталось без ответа
    status             text NOT NULL DEFAULT 'proposed'
                       CHECK (status IN ('proposed', 'open', 'done', 'cancelled', 'rejected', 'expired')),
    run_id             bigint REFERENCES processing_runs (id) ON DELETE SET NULL,
    -- в каком сообщении-сводке, под каким номером и когда показано владельцу
    digest_batch       text,
    digest_pos         int,
    notified_at        timestamptz,
    model              text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    decided_at         timestamptz,
    closed_at          timestamptz
);
CREATE INDEX commitments_status_due ON commitments (status, due_date);
CREATE INDEX commitments_chat ON commitments (chat_id);
CREATE INDEX commitments_source ON commitments (source_message_id);
CREATE INDEX commitments_due_message ON commitments (due_message_id) WHERE due_message_id IS NOT NULL;
CREATE INDEX commitments_debtor ON commitments (debtor_peer_id);
CREATE INDEX commitments_creditor ON commitments (creditor_peer_id);
CREATE INDEX commitments_batch ON commitments (digest_batch) WHERE digest_batch IS NOT NULL;

-- Предложенные изменения статуса: модель нашла в новых сообщениях признак того, что
-- обязательство выполнено, отменено или перенесено. Применяется только после нажатия владельца.
CREATE TABLE commitment_changes (
    id                   bigserial PRIMARY KEY,
    commitment_id        bigint NOT NULL REFERENCES commitments (id) ON DELETE CASCADE,
    kind                 text NOT NULL CHECK (kind IN ('fulfilled', 'cancelled', 'rescheduled')),
    evidence_message_id  bigint NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
    evidence_quote       text NOT NULL,
    new_due_expression   text,
    new_due_date         date,
    new_due_time         time,
    new_due_part         text,
    status               text NOT NULL DEFAULT 'proposed'
                         CHECK (status IN ('proposed', 'accepted', 'rejected', 'expired')),
    run_id               bigint REFERENCES processing_runs (id) ON DELETE SET NULL,
    digest_batch         text,
    digest_pos           int,
    notified_at          timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    decided_at           timestamptz,
    CHECK (kind <> 'rescheduled' OR new_due_date IS NOT NULL)
);
CREATE INDEX commitment_changes_commitment ON commitment_changes (commitment_id);
CREATE INDEX commitment_changes_evidence ON commitment_changes (evidence_message_id);
CREATE INDEX commitment_changes_batch ON commitment_changes (digest_batch) WHERE digest_batch IS NOT NULL;

-- Журнал изменений статуса. Текста сообщений в нём нет: только кто, что и когда.
--   actor: owner — владелец (кнопка или команда); auto — правило сервиса; model — предложено моделью
CREATE TABLE commitment_events (
    id             bigserial PRIMARY KEY,
    commitment_id  bigint NOT NULL REFERENCES commitments (id) ON DELETE CASCADE,
    at             timestamptz NOT NULL DEFAULT now(),
    actor          text NOT NULL CHECK (actor IN ('owner', 'auto', 'model')),
    action         text NOT NULL,
    from_status    text,
    to_status      text,
    details        jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX commitment_events_commitment ON commitment_events (commitment_id, at);
