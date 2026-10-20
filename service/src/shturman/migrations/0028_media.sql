-- Фото и документы: что в них — коротким текстом в архиве (media/, docs/media.md).
--
-- Как и расшифровка голосовых (0026), разбор вложения дописывается в messages.text: так его
-- видят поиск, эмбеддинги, защита, обработка обязательств и всё, что отдаёт переписку
-- ассистенту. Сам разбор хранится отдельно (media_summary — уже с меткой вида
-- «[документ «Договор.pdf», 12 стр.] …»), а текст собирается функцией media_text из подписи и
-- разбора. Повторный приход того же сообщения пишет подпись — запись в архив (store.py)
-- пересобирает её той же функцией до сравнения.
--
-- Сведения о файле приходят из источника: имя (документ), тип и размер. media_file — файл из
-- загруженной выгрузки Telegram Desktop, путь относительно каталога данных сервиса; он лежит
-- только до разбора (вложения или голосового) и потом удаляется.
--
-- Состояние разбора:
--   NULL     — не нужен или ещё не поставлен в очередь;
--   pending  — ждёт: скачать файл и разобрать;
--   asking   — текст или картинки отправлены модели заданием media_job, ждём ответ;
--   done     — готово, текст собран;
--   failed   — не получилось за несколько попыток (причина в media_error);
--   skipped  — не будет: вид файла не поддерживается, больше предела, источника нет.

ALTER TABLE messages
    ADD COLUMN media_name     text,      -- имя файла документа, как его прислали
    ADD COLUMN media_mime     text,
    ADD COLUMN media_size     bigint,    -- байт
    ADD COLUMN media_file     text,
    ADD COLUMN media_state    text CHECK (media_state IN ('pending', 'asking', 'done', 'failed', 'skipped')),
    ADD COLUMN media_error    text,
    ADD COLUMN media_attempts integer NOT NULL DEFAULT 0,
    ADD COLUMN media_at       timestamptz,   -- pending: время следующей попытки; иначе — последнего изменения
    ADD COLUMN media_job      bigint,
    ADD COLUMN media_summary  text;

CREATE INDEX messages_media_queue ON messages (id) WHERE media_state IN ('pending', 'asking');
CREATE INDEX messages_media_file ON messages (id) WHERE media_file IS NOT NULL;

-- Текст вложения в архиве: разбор (с меткой), под ним — подпись, если была.
CREATE FUNCTION media_text(caption text, summary text)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT summary || CASE WHEN COALESCE(btrim(caption), '') <> '' THEN E'\n' || caption ELSE '' END
$$;
