-- Ответ голосом, записанный грамотно (Р-103). docs/service.md, «Ответ голосом»; docs/voice.md.
--
-- Строка на одно «Ответить голосом»: в какой чат и на какое сообщение владелец отвечает, чего бот ждёт
-- и что вышло. Голоса здесь нет: файл скачивается в память, отдаётся контейнеру распознавания и
-- забывается. Расшифровка и записанный текст — только здесь и только сутки (`dictation.KEEP`), потом строка
-- удаляется целиком. Собеседнику отсюда не уходит ничего: ни черновика, ни задания отправки.
CREATE TABLE dictations (
    id            bigserial PRIMARY KEY,
    chat_id       bigint NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    message_id    bigint REFERENCES messages (id) ON DELETE SET NULL,   -- на что отвечает владелец
    source        text   NOT NULL CHECK (source IN ('rehearsal', 'notify', 'triage', 'draft')),
    nonce         text   NOT NULL,
    state         text   NOT NULL DEFAULT 'waiting'
                  CHECK (state IN ('waiting', 'transcribing', 'preparing', 'shown', 'stopped', 'failed', 'expired')),
    reason        text,
    wait_until    timestamptz NOT NULL,          -- до какого времени бот ждёт голосовое или набросок
    input_kind    text   CHECK (input_kind IS NULL OR input_kind IN ('voice', 'text')),
    transcript    text,                          -- что сказал (или набросал) владелец, как есть
    correction    text,                          -- поправка владельца («Поправить»)
    result        text,                          -- записанный текст
    flags         text[] NOT NULL DEFAULT '{}',  -- места «проверьте»
    job_id        bigint,
    asks          integer NOT NULL DEFAULT 0,    -- запросов к модели по этой строке
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX dictations_created ON dictations (created_at);
