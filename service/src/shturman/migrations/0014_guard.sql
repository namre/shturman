-- Защита от внедрённых инструкций во входящих сообщениях (см. guard/, docs/guard.md).
-- Столбец messages.agent_visible появился в 0013_executor.sql; здесь — то, что ведёт его значение.

-- Итог проверки сообщения:
--   NULL      — не проверено (защита выключена, модель недоступна или очередь ещё не дошла);
--   ok        — проверено, обычное сообщение;
--   suspect   — похоже на попытку управлять ассистентом: скрыто, ждёт решения владельца;
--   released  — владелец сказал «обычное сообщение»: видно, повторно не проверяется;
--   confirmed — владелец оставил скрытым.
-- Правка текста сбрасывает итог в NULL: новый текст проверяется заново (store.py).
ALTER TABLE messages
    ADD COLUMN guard_label      text CHECK (guard_label IN ('ok', 'suspect', 'released', 'confirmed')),
    ADD COLUMN guard_score      real,          -- оценка 0…1 того оценщика, который решил
    -- чем проверено: имя модели, «rules-N», оба через «+»; owner — по прежнему решению
    -- владельца о точно таком же тексте
    ADD COLUMN guard_model      text,
    ADD COLUMN guard_checked_at timestamptz;

-- Очередь проверки — сами строки архива: входящие текстовые сообщения без итога.
CREATE INDEX messages_guard_queue ON messages (id)
    WHERE guard_label IS NULL AND is_outgoing IS NOT TRUE AND kind = 'message' AND text <> '';

-- Скрытых сообщений мало; по этому индексу их находят уведомления и решения владельца.
CREATE INDEX messages_guard_hidden ON messages (id) WHERE NOT agent_visible;

-- Уведомление владельцу о скрытом тексте. Одно на текст: тот же текст в другом чате или пришедший
-- повторно нового уведомления не создаёт, а решение владельца действует на все такие сообщения
-- и запоминается (released — такой текст больше не скрывается, confirmed — скрывается молча).
-- Самого текста сообщения здесь нет: только его md5 и строка «кто, где, когда».
CREATE TABLE guard_alerts (
    id              bigserial PRIMARY KEY,
    text_hash       text NOT NULL UNIQUE,      -- md5(messages.text)
    -- первое сообщение с этим текстом; стирается вместе с ним (исключение чата с очисткой)
    message_id      bigint NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
    summary         text NOT NULL,             -- шапка карточки без цитаты
    nonce           text NOT NULL,
    status          text NOT NULL DEFAULT 'sent' CHECK (status IN ('sent', 'released', 'confirmed')),
    card_message_id bigint,                    -- сообщение с кнопками в управляющем чате
    notified_at     timestamptz NOT NULL DEFAULT now(),
    decided_at      timestamptz
);
CREATE INDEX guard_alerts_recent ON guard_alerts (notified_at);
