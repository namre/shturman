-- Эмбеддинги сообщений для смыслового поиска. Проверено на pgvector 0.8.7 и Postgres 16.
--
-- Тип halfvec (половинная точность) вместо vector: вектор и индекс вдвое меньше — это важно
-- на сервере с 4 ГБ памяти; операторный класс индекса — halfvec_cosine_ops.
--
-- РАЗМЕРНОСТЬ ЗАШИТА В ТИП СТОЛБЦА: 384 — под intfloat/multilingual-e5-small.
--   * Сменить модель на другую с той же размерностью можно настройкой: строки, посчитанные
--     прежней моделью, сервис сам вернёт в очередь и пересчитает (см. embeddings.py).
--   * Модель с другой размерностью требует НОВОЙ миграции: удалить индекс
--     messages_embedding_hnsw, сменить тип столбца (ALTER COLUMN embedding TYPE halfvec(N)
--     USING NULL), обнулить embedding_model, создать индекс заново. Пока этого не сделано,
--     сервис с включёнными эмбеддингами откажется запускаться: он сверяет
--     SHTURMAN_EMBEDDINGS_DIM с размерностью столбца.
--
-- Состояние строки задаётся парой (embedding, embedding_model):
--   model IS NULL                       — в очереди: ещё не рассмотрена;
--   model = текущая, embedding есть     — посчитана;
--   model = текущая, embedding IS NULL  — рассмотрена и пропущена (слишком короткий текст,
--                                          исключённый чат);
--   model = другая                      — устарела: при запуске сервис вернёт её в очередь.

CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE messages
    ADD COLUMN embedding       halfvec(384),
    ADD COLUMN embedding_model text;

-- Параметры построения — значения pgvector по умолчанию, записаны явно. Индекс создаётся на
-- пустом столбце и растёт по мере работы фонового счётчика: большой разовой сборки
-- (и памяти под неё) не требуется.
CREATE INDEX messages_embedding_hnsw ON messages
    USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);

-- Очередь счётчика: что ещё не рассмотрено. Пустеет по мере работы, поэтому остаётся маленьким.
CREATE INDEX messages_embedding_todo ON messages (id)
    WHERE embedding_model IS NULL AND kind = 'message' AND deleted_at IS NULL;

-- Вектор — производное от текста. Текст изменился (правка) или сообщение помечено удалённым —
-- вектор снимается; исправленное сообщение возвращается в очередь. Запись в архив при этом
-- ничего не знает об эмбеддингах: правило живёт в схеме.
CREATE FUNCTION messages_embedding_invalidate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.embedding := NULL;
    NEW.embedding_model := NULL;
    RETURN NEW;
END
$$;

CREATE TRIGGER messages_embedding_invalidate
    BEFORE UPDATE OF text, deleted_at ON messages
    FOR EACH ROW
    WHEN (OLD.embedding_model IS NOT NULL
          AND (OLD.text IS DISTINCT FROM NEW.text
               OR (OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL)))
    EXECUTE FUNCTION messages_embedding_invalidate();
