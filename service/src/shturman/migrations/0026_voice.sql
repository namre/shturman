-- Голосовые сообщения и «кружки»: расшифровка на сервере (voice/, docs/voice.md).
--
-- Расшифровка дописывается в messages.text — так её видят поиск, эмбеддинги, защита от
-- внедрённых инструкций и всё, что отдаёт переписку ассистенту, без отдельных правок в каждом
-- читателе. Сама расшифровка хранится ещё и отдельно (transcript), а текст собирается функцией
-- voice_text из подписи и расшифровки. Повторный приход того же сообщения (догрузка, сверка,
-- правка подписи) пишет подпись — запись в архив (store.py) до сравнения пересобирает её той же
-- функцией, поэтому неизменная подпись правкой не считается, а изменённая — считается.
--
-- Состояние расшифровки:
--   NULL     — не нужна или ещё не поставлена в очередь (старое сообщение, не голосовое);
--   pending  — ждёт: скачать файл и распознать;
--   done     — готово, текст собран;
--   failed   — не получилось за несколько попыток (причина в transcript_error);
--   skipped  — не будет: длиннее предела, файл недоступен, источника нет.

ALTER TABLE messages
    ADD COLUMN media_duration      integer,   -- длительность вложения, секунд (если известна)
    ADD COLUMN media_ref           text,      -- file_id Bot API для бизнес-режима; сессии он не нужен
    ADD COLUMN transcript          text,
    ADD COLUMN transcript_state    text CHECK (transcript_state IN ('pending', 'done', 'failed', 'skipped')),
    ADD COLUMN transcript_error    text,
    ADD COLUMN transcript_attempts integer NOT NULL DEFAULT 0,
    ADD COLUMN transcript_at       timestamptz;

CREATE INDEX messages_transcript_queue ON messages (id) WHERE transcript_state = 'pending';

-- Текст голосового в архиве: «[голосовое, 0:42] расшифровка», под ней — подпись, если была.
CREATE FUNCTION voice_text(caption text, transcript text, media_type text, duration integer)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT '[' || CASE WHEN media_type = 'video_message' THEN 'кружок' ELSE 'голосовое' END
           || CASE WHEN duration IS NOT NULL AND duration >= 0
                   THEN ', ' || (duration / 60)::text || ':' || lpad((duration % 60)::text, 2, '0')
                   ELSE '' END
           || '] ' || COALESCE(NULLIF(btrim(transcript), ''), '(без слов)')
           || CASE WHEN COALESCE(btrim(caption), '') <> '' THEN E'\n' || caption ELSE '' END
$$;
