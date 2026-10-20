-- «Черновик ответа» вместо репетиции и черновиков «только для просмотра» (решение Р-105, часть 1).
--
-- 1. Режима «только для просмотра» (`/prepare`, `outbox.prepare_only`, `SHTURMAN_PREPARE_ONLY`) больше нет.
--    Ждущие такие черновики закрываются как истёкшие (код `prepare_removed`), их текст стирается сразу;
--    задачи ответа, которые готовились без права отправки, снимаются. Признак `prepare_only` удаляется из
--    обеих таблиц вместе с проверкой «никогда не отправляется»: нечего проверять.
-- 2. Черновик ответа, который готовит сам сервис (бывшая карточка репетиции), хранит предложенный текст
--    в `outbox_drafts` с происхождением `suggest`: ждёт решения не дольше суток, текст стирается через час
--    после завершения (`drafts.purge_texts`). Через внутренний API такой черновик не создаётся
--    (`drafts.create` ставит `agent` сам).
-- 3. Строка `rehearsal_items` остаётся строкой подготовки и статистики — по-прежнему без текста: номер
--    черновика (`draft_id`) и чем владелец оценил ответ (`rated_via`: кнопкой, отправкой, своим вариантом
--    голосом).
-- 4. Автоответ доверенным выключается у всех аккаунтов. Его можно было включить при выключенной отправке,
--    пока шла подготовка без доставки (`/prepare on` или `SHTURMAN_PREPARE_ONLY`), и тогда он готовил
--    только черновики для просмотра. Без этого режима такой автоответ начал бы отвечать сам, как только
--    включится отправка (или сразу, если она уже включена). Подготовку из `.env` из базы не видно — поэтому
--    выключается у всех; снова включить можно только при включённой отправке. Владельцу — одно сообщение
--    после обновления (`outbox/service.announce_upgrade`, ключ `upgrade.autoreply_disabled`).
-- Применённые миграции не правятся: функция проверки черновика заменяется целиком (копия из 0021 без
-- строки о `prepare_only` и с происхождением `suggest`).

-- 1. Только для просмотра — закрыть и убрать.
UPDATE reply_tasks SET status = 'cancelled', error_code = 'prepare_removed', updated_at = now()
WHERE prepare_only AND status IN ('queued', 'generating', 'waiting_source', 'waiting_owner', 'drafting');

UPDATE outbox_drafts SET status = 'expired', error_code = 'prepare_removed', finished_at = now()
WHERE status = 'pending' AND prepare_only;
UPDATE outbox_drafts SET text = '', text_purged_at = now()
WHERE prepare_only AND error_code = 'prepare_removed' AND text <> '';

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

ALTER TABLE outbox_drafts DROP CONSTRAINT outbox_drafts_prepare_never_sends;
ALTER TABLE outbox_drafts DROP COLUMN prepare_only;
ALTER TABLE reply_tasks DROP COLUMN prepare_only;
DELETE FROM settings WHERE key = 'outbox.prepare_only';

-- 4. Автоответ доверенным — выключить у всех; сколько выключено — для сообщения владельцу.
INSERT INTO settings (key, value)
SELECT 'upgrade.autoreply_disabled', jsonb_build_object('accounts', count(*))
FROM outbox_accounts WHERE autoreply_enabled
HAVING count(*) > 0
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();
UPDATE outbox_accounts SET autoreply_enabled = false WHERE autoreply_enabled;

-- 2. Черновик ответа сервиса.
ALTER TABLE outbox_drafts DROP CONSTRAINT outbox_drafts_origin_check;
ALTER TABLE outbox_drafts ADD CONSTRAINT outbox_drafts_origin_check
    CHECK (origin IN ('agent', 'autoreply', 'suggest'));
-- У черновика сервиса нет задачи ответа: источники и разрешения ему не выдаются.
ALTER TABLE outbox_drafts ADD CONSTRAINT outbox_drafts_suggest_shape
    CHECK (origin <> 'suggest' OR task_id IS NULL);

-- 3. Строка подготовки — номер черновика и как оценён ответ. Текста по-прежнему нет.
ALTER TABLE rehearsal_items
    ADD COLUMN draft_id  bigint REFERENCES outbox_drafts (id) ON DELETE SET NULL,
    ADD COLUMN rated_via text CHECK (rated_via IS NULL OR rated_via IN ('button', 'send', 'voice'));
UPDATE rehearsal_items SET rated_via = 'button' WHERE rating IS NOT NULL;
CREATE INDEX rehearsal_items_draft ON rehearsal_items (draft_id) WHERE draft_id IS NOT NULL;
