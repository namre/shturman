-- Память, этап 2: проекты, датированные факты и решения, профиль владельца (docs/memory.md).
--
-- Кто чему хозяин — как и раньше: таблицы — источник истины, файлы страниц — производное.
-- Всё, что выведено из сообщений, ссылается на messages.id с ON DELETE CASCADE.

-- ---------------------------------------------------------------------------
-- Проекты
-- ---------------------------------------------------------------------------

-- Проект, объект, сделка. Заводит владелец или предлагает модель — тогда владелец решает кнопкой.
-- Отклонённое предложение остаётся строкой: по нему же видно, что предлагать это снова не нужно.
CREATE TABLE projects (
    id               bigserial PRIMARY KEY,
    title            text NOT NULL,
    -- ключ сравнения названий: нижний регистр, ё->е, только буквы и цифры через пробел
    title_norm       text NOT NULL,
    -- пояснение владельца (необязательно); из переписки сюда ничего не пишется
    description      text,
    status           text NOT NULL DEFAULT 'proposed'
                     CHECK (status IN ('proposed', 'active', 'archived', 'rejected')),
    origin           text NOT NULL CHECK (origin IN ('owner', 'model')),
    -- почему предложен; только числа и идентификаторы: {"chats": [id], "mentions": n, "episodes": n, "messages": n}
    reason           jsonb NOT NULL DEFAULT '{}',
    -- в каком сообщении с предложениями, под каким номером и когда показан владельцу
    batch            text,
    pos              int,
    notified_at      timestamptz,
    digest_attempts  int NOT NULL DEFAULT 0,
    -- кто и каким каналом решил (только по проверенному владельцу: telegram, setup)
    approved_by      text,
    approved_via     text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    decided_at       timestamptz,
    CHECK (title_norm <> '')
);
-- одно название — один действующий или предложенный проект
CREATE UNIQUE INDEX projects_title_norm ON projects (title_norm) WHERE status <> 'rejected';
CREATE INDEX projects_batch ON projects (batch) WHERE batch IS NOT NULL;

-- Чаты проекта. owner — добавил владелец; model — чат, по которому проект предложен.
CREATE TABLE project_chats (
    project_id  bigint NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    chat_id     bigint NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    origin      text NOT NULL DEFAULT 'owner' CHECK (origin IN ('owner', 'model')),
    added_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, chat_id)
);
CREATE INDEX project_chats_chat ON project_chats (chat_id);

-- Другие названия проекта («ЖК Северный», «Северный», «объект на Ленина»).
CREATE TABLE project_aliases (
    id          bigserial PRIMARY KEY,
    project_id  bigint NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    alias       text NOT NULL,
    alias_norm  text NOT NULL,
    origin      text NOT NULL DEFAULT 'owner' CHECK (origin IN ('owner', 'model')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, alias_norm)
);
CREATE INDEX project_aliases_norm ON project_aliases (alias_norm);

-- Упоминания проекта, которого ещё нет: из них складывается предложение владельцу
-- (не меньше трёх упоминаний в двух эпизодах за 30 дней). Источник — сообщение эпизода:
-- удалено сообщение — уходит и упоминание.
CREATE TABLE project_mentions (
    id          bigserial PRIMARY KEY,
    title       text NOT NULL,
    title_norm  text NOT NULL,
    chat_id     bigint NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    message_id  bigint NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
    -- эпизод: <chat>:<первое>-<последнее сообщение>
    episode     text NOT NULL,
    run_id      bigint REFERENCES processing_runs (id) ON DELETE SET NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (title_norm, message_id)
);
CREATE INDEX project_mentions_norm ON project_mentions (title_norm);
CREATE INDEX project_mentions_message ON project_mentions (message_id);

ALTER TABLE commitments ADD COLUMN project_id bigint REFERENCES projects (id) ON DELETE SET NULL;
CREATE INDEX commitments_project ON commitments (project_id) WHERE project_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Датированные факты и решения
-- ---------------------------------------------------------------------------

-- Факт о человеке, проекте или владельце; решение — только у проекта.
--   slot — «ключ» сменяющих друг друга фактов (должность, телефон, цена…): новый факт с тем же
--   ключом закрывает прежний (valid_to, superseded_by), прежний остаётся в хронологии;
--   NULL — факт, который ничего не сменяет.
--   status: proposed — ждёт владельца (факты о самом владельце); active — действует или закрыт
--   датой; rejected — владелец отклонил; retracted — владелец отметил как неверный.
--   origin — кто это сказал: владелец, собеседник; model — вывод модели.
-- Субъект «владелец» один на экземпляр, поэтому у его фактов нет ни person_id, ни project_id.
CREATE TABLE facts (
    id                    bigserial PRIMARY KEY,
    subject_type          text NOT NULL CHECK (subject_type IN ('person', 'project', 'owner')),
    person_id             bigint REFERENCES people (id) ON DELETE CASCADE,
    project_id            bigint REFERENCES projects (id) ON DELETE CASCADE,
    kind                  text NOT NULL DEFAULT 'fact' CHECK (kind IN ('fact', 'decision')),
    slot                  text,
    text                  text NOT NULL CHECK (length(text) BETWEEN 1 AND 240),
    -- ключ сравнения текста: тот же факт второй раз не пишется
    text_norm             text NOT NULL,
    valid_from            date NOT NULL,
    valid_to              date,
    superseded_by         bigint REFERENCES facts (id) ON DELETE SET NULL,
    status                text NOT NULL DEFAULT 'active'
                          CHECK (status IN ('proposed', 'active', 'rejected', 'retracted')),
    origin                text NOT NULL CHECK (origin IN ('owner', 'other', 'model')),
    source_message_id     bigint NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
    source_quote          text NOT NULL,
    run_id                bigint REFERENCES processing_runs (id) ON DELETE SET NULL,
    model                 text,
    -- показ владельцу (факты о владельце): в каком сообщении, под каким номером, сколько раз
    batch                 text,
    pos                   int,
    notified_at           timestamptz,
    digest_attempts       int NOT NULL DEFAULT 0,
    digest_fingerprint    text,
    -- согласие владельца — только по проверенному владельцу (authority), как у обязательств
    approved_at           timestamptz,
    approved_by           text,
    approved_via          text,
    approval_fingerprint  text,
    created_at            timestamptz NOT NULL DEFAULT now(),
    decided_at            timestamptz,
    CHECK (subject_type <> 'person' OR (person_id IS NOT NULL AND project_id IS NULL)),
    CHECK (subject_type <> 'project' OR (project_id IS NOT NULL AND person_id IS NULL)),
    CHECK (subject_type <> 'owner' OR (person_id IS NULL AND project_id IS NULL)),
    CHECK (kind <> 'decision' OR (subject_type = 'project' AND slot IS NULL)),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);
CREATE INDEX facts_slot_active ON facts (subject_type, person_id, project_id, slot)
    WHERE status = 'active' AND valid_to IS NULL;
CREATE INDEX facts_person ON facts (person_id) WHERE person_id IS NOT NULL;
CREATE INDEX facts_project ON facts (project_id) WHERE project_id IS NOT NULL;
CREATE INDEX facts_source ON facts (source_message_id);
CREATE INDEX facts_batch ON facts (batch) WHERE batch IS NOT NULL;
CREATE INDEX facts_superseded ON facts (superseded_by) WHERE superseded_by IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Страницы проектов и профиль владельца
-- ---------------------------------------------------------------------------

ALTER TABLE pages DROP CONSTRAINT pages_entity_type_check;
ALTER TABLE pages ADD CONSTRAINT pages_entity_type_check
    CHECK (entity_type IN ('person', 'project', 'owner'));
ALTER TABLE pages ADD COLUMN project_id bigint UNIQUE REFERENCES projects (id) ON DELETE CASCADE;
ALTER TABLE pages DROP CONSTRAINT pages_check;
ALTER TABLE pages ADD CONSTRAINT pages_subject_check CHECK (
    (entity_type = 'person' AND person_id IS NOT NULL AND project_id IS NULL)
    OR (entity_type = 'project' AND project_id IS NOT NULL AND person_id IS NULL)
    OR (entity_type = 'owner' AND person_id IS NULL AND project_id IS NULL));
-- профиль владельца — одна страница
CREATE UNIQUE INDEX pages_one_owner ON pages (entity_type) WHERE entity_type = 'owner';

ALTER TABLE page_entries DROP CONSTRAINT page_entries_block_check;
ALTER TABLE page_entries ADD CONSTRAINT page_entries_block_check
    CHECK (block IN ('summary', 'timeline', 'facts', 'decisions'));
ALTER TABLE page_blocks DROP CONSTRAINT page_blocks_block_check;
ALTER TABLE page_blocks ADD CONSTRAINT page_blocks_block_check
    CHECK (block IN ('head', 'summary', 'owner', 'commitments', 'timeline', 'facts', 'decisions'));
