-- Шлюз отправки: правки по итогам проверок.
--   * карточка черновика: какое сообщение владельцу какой части текста соответствует
--     (чтобы после завершения заменить текст карточки и убрать кнопки);
--   * текст завершённого черновика можно стереть (срок хранения, исключение чата) —
--     и только стереть, и только у завершённого;
--   * черновик, карточка которого не дошла до владельца, закрывается как неудачный;
--   * «модель не ответила» — отдельное состояние, а не «не важно»;
--   * журнал исходов автоответа без текста.

ALTER TABLE outbox_drafts
    ADD COLUMN card_messages    jsonb NOT NULL DEFAULT '{}',   -- {"номер части": идентификатор сообщения владельцу}
    ADD COLUMN text_purged_at   timestamptz;

ALTER TABLE outbox_drafts DROP CONSTRAINT outbox_drafts_text_check;
ALTER TABLE outbox_drafts ADD CONSTRAINT outbox_drafts_text_check
    CHECK (text <> '' OR text_purged_at IS NOT NULL);

CREATE OR REPLACE FUNCTION outbox_drafts_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    final CONSTANT text[] := ARRAY['sent', 'failed', 'outcome_unknown', 'rejected', 'expired', 'superseded'];
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
       OR NEW.reply_to_tg_id IS DISTINCT FROM OLD.reply_to_tg_id THEN
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
           -- pending → failed: карточка не дошла до владельца, согласовать некому
           (OLD.status = 'pending'  AND NEW.status IN ('approved', 'rejected', 'expired', 'superseded', 'failed'))
        OR (OLD.status = 'approved' AND NEW.status IN ('sending', 'failed'))
        OR (OLD.status = 'sending'  AND NEW.status IN ('sent', 'failed', 'outcome_unknown'))
        -- поздно пришедшее подтверждение доставки; обратного пути и повторной отправки нет
        OR (OLD.status = 'outcome_unknown' AND NEW.status = 'sent')
    ) THEN
        RAISE EXCEPTION 'недопустимый переход черновика: % → %', OLD.status, NEW.status;
    END IF;
    IF NEW.status IN ('approved', 'sending') AND NEW.text = '' THEN
        RAISE EXCEPTION 'черновик без текста не отправляется';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END $$;

ALTER TABLE watch_hits DROP CONSTRAINT watch_hits_status_check;
ALTER TABLE watch_hits ADD CONSTRAINT watch_hits_status_check CHECK (status IN (
    'checking', 'relevant', 'not_relevant', 'duplicate', 'limited', 'failed',
    -- модель вернула пустой ответ: это сбой, а не решение «не важно»
    'no_answer'));

-- Чем закончился каждый запрос автоответа. Текста здесь нет — только исход.
CREATE TABLE outbox_autoreply_log (
    id          bigserial PRIMARY KEY,
    account_id  bigint NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    chat_id     bigint NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    -- replied: ответ записан на отправку; declined: модель решила не отвечать;
    -- no_answer: модель вернула пустоту; dropped: к приходу ответа условия изменились;
    -- failed: модель или плагин недоступны
    outcome     text NOT NULL CHECK (outcome IN ('replied', 'declined', 'no_answer', 'dropped', 'failed')),
    reason      text,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX outbox_autoreply_log_time ON outbox_autoreply_log (created_at);
