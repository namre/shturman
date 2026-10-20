-- Реакция на сообщения (решение Р-84): уведомления владельцу «вам написали» и «вас упомянули»,
-- наблюдатель по сферам, повтор того, что не получилось из-за сбоя.
--
-- Уведомления (notify/). Настройки — строка `notify.settings` в `settings` (её нет — действуют
-- умолчания из notify/settings.py). Здесь — очередь: одно живое сообщение, которое прошло отбор
-- (личное входящее или прямое обращение в группе), — одна строка. Текст сообщения сюда не
-- копируется: при отправке он читается из `messages` заново (успела ли расшифровка, не скрыто ли
-- защитой, не удалено ли, не ответил ли владелец). Строка удаляется вместе с сообщением и чатом.
--
--   waiting — ждёт своего времени (due_at): частота «не чаще раза на чат», пачка, тихие часы;
--   sent    — ушло владельцу (job_id — задание notify.owner);
--   dropped — не ушло и не уйдёт: dropped_reason — answered (владелец уже ответил в этом чате),
--             gone (удалено, чат исключён), disabled (уведомления выключили, пока ждало),
--             stale (слишком старое), no_bot (нет бота согласований).
CREATE TABLE notify_items (
    id              bigserial PRIMARY KEY,
    message_id      bigint NOT NULL UNIQUE REFERENCES messages (id) ON DELETE CASCADE,
    chat_id         bigint NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    -- personal — входящее в личном чате; mention — прямое обращение в группе
    kind            text   NOT NULL CHECK (kind IN ('personal', 'mention')),
    -- для mention: mention (разметка упоминания), reply (ответ на ваше сообщение), alias (обращение)
    reason          text   CHECK (reason IS NULL OR reason IN ('mention', 'reply', 'alias')),
    -- пришло в тихие часы: уйдёт утром одной пачкой
    quiet           boolean NOT NULL DEFAULT false,
    -- пачка (режим «раз в N минут»): уйдёт одним сообщением вместе с другими
    digest          boolean NOT NULL DEFAULT false,
    state           text   NOT NULL DEFAULT 'waiting' CHECK (state IN ('waiting', 'sent', 'dropped')),
    dropped_reason  text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    due_at          timestamptz NOT NULL,
    sent_at         timestamptz,
    job_id          bigint
);
CREATE INDEX notify_items_due ON notify_items (due_at) WHERE state = 'waiting';
CREATE INDEX notify_items_chat_sent ON notify_items (chat_id, sent_at) WHERE state = 'sent';

-- Наблюдатель по сферам (Р-82): правило смотрит группы и каналы из выбранных чатов И из выбранных
-- сфер. Пустой перечень сфер — как раньше, только чаты. Хотя бы одно из двух не пусто — проверяет код.
ALTER TABLE watch_rules ALTER COLUMN chat_ids SET DEFAULT '{}';
ALTER TABLE watch_rules ADD COLUMN sphere_ids bigint[] NOT NULL DEFAULT '{}';
