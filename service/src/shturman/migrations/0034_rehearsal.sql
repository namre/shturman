-- Репетиция ответов на основном аккаунте владельца (решение Р-85).
--
-- На новое личное сообщение от выбранного владельцем человека сервис готовит ответ так же, как
-- автоответ доверенным (уровень 2а), и присылает владельцу в бот согласований карточку: кто написал,
-- его сообщение, предложенный ответ и три кнопки оценки. Отправки у репетиции нет и быть не может:
-- основной аккаунт подключён только на чтение, а репетиция не создаёт ни черновика (`outbox_drafts`),
-- ни задачи ответа (`reply_tasks`), ни задания отправки. Настройки — строка `rehearsal.settings`
-- в `settings` (её нет — репетиция выключена).
--
-- Здесь — одна строка на сообщение, которое репетиция рассмотрела. **Текста нет**: ни сообщения
-- собеседника, ни предложенного ответа — только номера, исход, оценка владельца и время. Предложенный
-- ответ живёт только в карточке, ушедшей владельцу (и в задании модели — до его разбора).
--
--   queued    — ждёт, не допишет ли собеседник ещё (пачка сообщений — одна карточка);
--   preparing — модель готовит ответ (job_id — задание llm.structured);
--   shown     — карточка ушла владельцу; outcome — что предложила модель; rating — оценка владельца;
--   skipped   — не готовилось: reason — newer (есть сообщение новее), answered (вы уже ответили),
--               daily_cap (дневной предел), pause (пауза между репетициями), stale (сообщение
--               устарело), gone (удалено, изменено, скрыто защитой, человека убрали из списка);
--   failed    — не получилось: reason — model_unavailable, invalid_model_result, card_not_delivered.
CREATE TABLE rehearsal_items (
    id                  bigserial PRIMARY KEY,
    account_id          bigint NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    chat_id             bigint NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    trigger_message_id  bigint NOT NULL UNIQUE REFERENCES messages (id) ON DELETE CASCADE,
    peer_tg_id          bigint NOT NULL,
    state               text   NOT NULL DEFAULT 'queued'
                        CHECK (state IN ('queued', 'preparing', 'shown', 'skipped', 'failed')),
    reason              text,
    outcome             text   CHECK (outcome IS NULL OR outcome IN ('reply', 'decline', 'ask_owner', 'need_source')),
    -- сколько сообщений собеседника подряд покрывает карточка (пачка)
    covered             integer NOT NULL DEFAULT 1,
    -- номера сообщений, которые модель получила справкой (для проверки ссылок в её ответе); без текста
    offered             bigint[] NOT NULL DEFAULT '{}',
    job_id              bigint,
    asked_at            timestamptz,
    nonce               text   NOT NULL,
    rating              text   CHECK (rating IS NULL OR rating IN ('good', 'almost', 'bad')),
    rated_at            timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CHECK (rating IS NULL OR state = 'shown')
);
CREATE INDEX rehearsal_items_created ON rehearsal_items (created_at);
CREATE INDEX rehearsal_items_chat_queued ON rehearsal_items (chat_id) WHERE state = 'queued';

-- Уведомление (Р-84) о сообщении, на которое готовится репетиция, придерживается: одна карточка
-- «вам написали + предложенный ответ» вместо двух сообщений. Не вышло (предел, сбой модели) —
-- уведомление уходит как обычно; не дождалось (сервис остановился) — уходит само в свой срок.
ALTER TABLE notify_items ADD COLUMN held_for_rehearsal boolean NOT NULL DEFAULT false;
