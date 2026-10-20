-- «В чат»: текст ответа — в поле ввода чата владельца (решение Р-105, часть 2).
--
-- Нажатие «В чат» под «Черновиком ответа» кладёт его текст облачным черновиком Telegram в поле ввода этого
-- личного чата основного аккаунта (`replies/put.py`); владелец правит и отправляет сам. Черновик сервиса после
-- этого завершён: новый конечный статус `placed` (из `pending`). `put_started_at` — нажатие принято и запрос
-- к Telegram идёт: второе нажатие в это время ничего не делает. То же — у записи «Записано» (`dictations`).
-- Остановка ассистента (Р-87) проверяется только в коде, не в базе: при нажатии (`replies/put.check`) и ещё раз
-- прямо перед записью в Telegram (`replies/put.put`, `before_write`). Переход в `placed` лишь записывает уже
-- сделанное — триггера остановки на нём нет, и защиты на уровне базы здесь нет.
-- Применённые миграции не правятся: функция проверки черновика заменяется целиком (копия из 0045 с `placed`).

ALTER TABLE outbox_drafts DROP CONSTRAINT outbox_drafts_status_check;
ALTER TABLE outbox_drafts ADD CONSTRAINT outbox_drafts_status_check CHECK (status IN (
    'pending', 'approved', 'sending', 'sent', 'failed', 'outcome_unknown', 'rejected', 'expired', 'superseded',
    'placed'));
ALTER TABLE outbox_drafts ADD COLUMN put_started_at timestamptz;
-- «В чат» — только у черновика сервиса.
ALTER TABLE outbox_drafts ADD CONSTRAINT outbox_drafts_placed_suggest_only
    CHECK (status <> 'placed' OR origin = 'suggest');

CREATE OR REPLACE FUNCTION outbox_drafts_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    final CONSTANT text[] := ARRAY['sent', 'failed', 'outcome_unknown', 'rejected', 'expired', 'superseded', 'placed'];
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NOT (NEW.status = 'pending' OR (NEW.status = 'approved' AND NEW.origin = 'autoreply')) THEN
            RAISE EXCEPTION 'черновик создаётся только в состоянии pending (автоответ — approved)';
        END IF;
        IF NEW.text = '' OR NEW.text_purged_at IS NOT NULL THEN
            RAISE EXCEPTION 'черновик создаётся с текстом';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.chat_id <> OLD.chat_id OR NEW.account_id <> OLD.account_id OR NEW.channel <> OLD.channel
       OR NEW.origin <> OLD.origin OR NEW.nonce <> OLD.nonce
       OR NEW.reply_to_tg_id IS DISTINCT FROM OLD.reply_to_tg_id
       OR NEW.text_hash IS DISTINCT FROM OLD.text_hash
       OR NEW.trigger_message_id IS DISTINCT FROM OLD.trigger_message_id
       OR NEW.task_id IS DISTINCT FROM OLD.task_id
       OR NEW.sources IS DISTINCT FROM OLD.sources
       OR NEW.task_policy_revision IS DISTINCT FROM OLD.task_policy_revision
       OR NEW.topic_tg_id IS DISTINCT FROM OLD.topic_tg_id THEN
        RAISE EXCEPTION 'адресат и канал черновика после создания не меняются';
    END IF;
    IF NEW.text IS DISTINCT FROM OLD.text THEN
        -- Единственное допустимое изменение текста — стирание у завершённого черновика.
        IF NOT (NEW.text = '' AND NEW.text_purged_at IS NOT NULL
                AND OLD.status = ANY (final) AND NEW.status = OLD.status) THEN
            RAISE EXCEPTION 'текст черновика после создания не меняется; стереть можно только у завершённого';
        END IF;
    ELSIF NEW.text_purged_at IS DISTINCT FROM OLD.text_purged_at AND OLD.text <> '' THEN
        RAISE EXCEPTION 'отметка о стирании ставится только вместе со стиранием текста';
    END IF;
    IF NEW.status <> OLD.status AND NOT (
           -- pending → failed: карточка не дошла до владельца, согласовать некому;
           -- pending → placed: владелец нажал «В чат» и текст лёг в поле ввода (Р-105)
           (OLD.status = 'pending'  AND NEW.status IN ('approved', 'rejected', 'expired', 'superseded', 'failed', 'placed'))
        OR (OLD.status = 'approved' AND NEW.status IN ('sending', 'failed'))
        OR (OLD.status = 'sending'  AND NEW.status IN ('sent', 'failed', 'outcome_unknown'))
        -- поздно пришедшее подтверждение доставки; обратного пути и повторной отправки нет
        OR (OLD.status = 'outcome_unknown' AND NEW.status = 'sent')
    ) THEN
        RAISE EXCEPTION 'недопустимый переход черновика: % → %', OLD.status, NEW.status;
    END IF;
    IF NEW.status IN ('approved', 'sending', 'placed') AND NEW.text = '' THEN
        RAISE EXCEPTION 'черновик без текста не отправляется';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END $$;

ALTER TABLE dictations
    ADD COLUMN put_started_at timestamptz,
    ADD COLUMN placed_at      timestamptz;

ALTER TABLE rehearsal_items DROP CONSTRAINT rehearsal_items_rated_via_check;
ALTER TABLE rehearsal_items ADD CONSTRAINT rehearsal_items_rated_via_check
    CHECK (rated_via IS NULL OR rated_via IN ('button', 'send', 'voice', 'put'));
