-- Страницы памяти: файлы Markdown о людях и их указатель в базе (docs/memory.md).
--
-- Кто чему хозяин:
--   * обязательства и люди — таблицы обработки (0005); блок обязательств на странице — производное;
--   * блок владельца и строки хронологии — файл; база хранит только копию для поиска;
--   * утверждения сводки — таблица page_entries (вместе с сообщениями-источниками), файл — производное.
-- Всё выведенное из сообщений ссылается на messages.id с ON DELETE CASCADE.

CREATE TABLE pages (
    id             bigserial PRIMARY KEY,
    -- вид страницы; проекты и решения добавятся следующими миграциями
    entity_type    text NOT NULL CHECK (entity_type IN ('person')),
    -- как в шапке файла: person:<people.id>
    entity_id      text NOT NULL UNIQUE,
    person_id      bigint UNIQUE REFERENCES people (id) ON DELETE CASCADE,
    -- путь внутри каталога страниц; назначается один раз и при переименовании человека не меняется
    path           text NOT NULL UNIQUE,
    title          text NOT NULL,
    -- дата последнего изменения содержимого (поле updated в шапке)
    updated        date,
    -- sha256 файла в том виде, в каком его последний раз записал сервис
    file_hash      text,
    -- файл надо перерисовать из базы (новая страница, удалённый источник, пришла сводка)
    dirty          boolean NOT NULL DEFAULT true,
    -- почему файл не обновляется (нарушена разметка); NULL — всё в порядке
    problem        text,
    -- none: сводки нет; pending: запрос у модели; fresh: соответствует входам; failed: не обновлена
    summary_state  text NOT NULL DEFAULT 'none'
                   CHECK (summary_state IN ('none', 'pending', 'fresh', 'failed')),
    -- отпечаток входов (хронология, обязательства, выборка сообщений) последней принятой сводки
    inputs_hash    text,
    summary_job_id bigint REFERENCES jobs (id) ON DELETE SET NULL,
    summary_at     timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    built_at       timestamptz,
    CHECK (entity_type <> 'person' OR person_id IS NOT NULL)
);

-- Записи страницы, выведенные из сообщений.
--   timeline: строка хронологии; текст живёт в файле, здесь — ключ («уже дописано») и источники;
--   summary:  утверждение сводки целиком (текст, происхождение, порядок).
CREATE TABLE page_entries (
    id          bigserial PRIMARY KEY,
    page_id     bigint NOT NULL REFERENCES pages (id) ON DELETE CASCADE,
    block       text NOT NULL CHECK (block IN ('summary', 'timeline')),
    -- timeline: c<commitments.id> — принятое обязательство, e<commitment_events.id> — событие;
    -- summary: порядковый номер утверждения
    key         text NOT NULL,
    pos         int  NOT NULL DEFAULT 0,
    text        text,
    -- кто это сказал: владелец, собеседник или вывод модели
    origin      text CHECK (origin IN ('owner', 'other', 'model')),
    -- источники противоречат друг другу (помечается, а не разрешается)
    disputed    boolean NOT NULL DEFAULT false,
    -- сколько сообщений-источников было при записи: стало меньше — запись теряет основание
    n_sources   int  NOT NULL,
    -- строка хронологии убрана из файла, потому что удалено сообщение-источник; повторно не дописывается
    removed_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (page_id, block, key)
);

CREATE TABLE page_entry_sources (
    entry_id    bigint NOT NULL REFERENCES page_entries (id) ON DELETE CASCADE,
    message_id  bigint NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
    PRIMARY KEY (entry_id, message_id)
);
CREATE INDEX page_entry_sources_message ON page_entry_sources (message_id);

-- Копия блоков страницы для поиска. head — заголовок и алиасы; owner — заметки владельца.
CREATE TABLE page_blocks (
    page_id  bigint NOT NULL REFERENCES pages (id) ON DELETE CASCADE,
    block    text   NOT NULL CHECK (block IN ('head', 'summary', 'owner', 'commitments', 'timeline')),
    text     text   NOT NULL DEFAULT '',
    fts      tsvector GENERATED ALWAYS AS (to_tsvector('russian', text)) STORED,
    PRIMARY KEY (page_id, block)
);
CREATE INDEX page_blocks_fts ON page_blocks USING gin (fts);

-- Предложения владельцу завести страницу о человеке, которого он ещё не подтверждал.
-- Отклонённое запоминается и повторно не предлагается.
CREATE TABLE page_proposals (
    person_id    bigint PRIMARY KEY REFERENCES people (id) ON DELETE CASCADE,
    status       text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'rejected')),
    -- только счётчики: {"commitments": n, "messages": n}
    reason       jsonb NOT NULL DEFAULT '{}',
    -- в каком сообщении-сводке, под каким номером и когда показано владельцу
    batch        text,
    pos          int,
    notified_at  timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    decided_at   timestamptz
);
CREATE INDEX page_proposals_batch ON page_proposals (batch) WHERE batch IS NOT NULL;

-- Сборки страниц. Строка в статусе running — она же замок: одновременно идёт одна сборка.
CREATE TABLE page_builds (
    id           bigserial PRIMARY KEY,
    trigger      text NOT NULL CHECK (trigger IN ('manual', 'nightly', 'auto')),
    status       text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done')),
    -- после какого прогона обработки запущена
    run_id       bigint REFERENCES processing_runs (id) ON DELETE SET NULL,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    -- только счётчики и пути файлов, без текста страниц
    stats        jsonb NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX page_builds_one_running ON page_builds (status) WHERE status = 'running';
