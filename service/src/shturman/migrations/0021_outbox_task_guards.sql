-- A generated reply binds immutable text, target, task policy and source receipts.
ALTER TABLE outbox_drafts
    ADD COLUMN task_id bigint REFERENCES reply_tasks (id) ON DELETE RESTRICT,
    ADD COLUMN sources jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(sources) = 'array'),
    ADD COLUMN task_policy_revision text,
    ADD COLUMN topic_tg_id bigint CHECK (topic_tg_id IS NULL OR topic_tg_id > 0),
    ADD COLUMN prepare_only boolean NOT NULL DEFAULT false;
ALTER TABLE outbox_drafts ADD CONSTRAINT outbox_drafts_prepare_never_sends
    CHECK (NOT prepare_only OR status NOT IN ('approved', 'sending', 'sent', 'outcome_unknown'));
CREATE INDEX outbox_drafts_task ON outbox_drafts(task_id) WHERE task_id IS NOT NULL;
ALTER TABLE outbox_drafts ADD CONSTRAINT outbox_drafts_task_bound
    CHECK ((task_id IS NULL AND task_policy_revision IS NULL AND sources = '[]'::jsonb)
           OR (task_id IS NOT NULL AND task_policy_revision IS NOT NULL));

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
       OR NEW.reply_to_tg_id IS DISTINCT FROM OLD.reply_to_tg_id
       OR NEW.text_hash IS DISTINCT FROM OLD.text_hash
       OR NEW.trigger_message_id IS DISTINCT FROM OLD.trigger_message_id
       OR NEW.task_id IS DISTINCT FROM OLD.task_id
       OR NEW.sources IS DISTINCT FROM OLD.sources
       OR NEW.task_policy_revision IS DISTINCT FROM OLD.task_policy_revision
       OR NEW.topic_tg_id IS DISTINCT FROM OLD.topic_tg_id
       OR NEW.prepare_only IS DISTINCT FROM OLD.prepare_only THEN
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

