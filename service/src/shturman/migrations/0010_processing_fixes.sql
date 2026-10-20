-- Обработка: повторное планирование неудавшихся запросов и учёт повторных отправок сводки.

-- Запрос к модели помнит свои сообщения сам: контекст задания в очереди стирается через
-- несколько минут после закрытия, а неудавшийся запрос нужно уметь поставить заново.
--   attempt — какой это по счёту повтор (0 — первый запрос);
--   retry   — запрос не удался, его сообщения разберёт следующий прогон;
--   given_up — повторы исчерпаны, эпизод пропущен (счётчик — в итогах прогона).
ALTER TABLE processing_requests
    ADD COLUMN message_ids bigint[] NOT NULL DEFAULT '{}',
    ADD COLUMN attempt int NOT NULL DEFAULT 0;
ALTER TABLE processing_requests DROP CONSTRAINT processing_requests_state_check;
ALTER TABLE processing_requests ADD CONSTRAINT processing_requests_state_check
    CHECK (state IN ('pending', 'done', 'failed', 'retry', 'given_up'));
CREATE INDEX processing_requests_retry ON processing_requests (job_id) WHERE state = 'retry';

-- Сколько раз пункт уже уходил владельцу в сводке: недоставленную сводку пересылаем
-- ограниченное число раз, а не бесконечно.
ALTER TABLE commitments ADD COLUMN digest_attempts int NOT NULL DEFAULT 0;
ALTER TABLE commitment_changes ADD COLUMN digest_attempts int NOT NULL DEFAULT 0;
