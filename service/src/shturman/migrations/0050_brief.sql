-- Справка при контакте и сигнал о просрочке (записка «держать, подкладывать, сигналить», шаги 2–3).
--
-- briefs — справки владельцу: о ком (person:<id> или peer:<id>), когда отправлена и до какого времени
-- отложена. Текста переписки здесь нет: справка собирается заново при нажатии «Показать».
CREATE TABLE briefs (
    id             bigserial PRIMARY KEY,
    subject_key    text NOT NULL CHECK (subject_key ~ '^(person|peer):[0-9]{1,18}$'),
    peer_ids       bigint[] NOT NULL,
    message_id     bigint REFERENCES messages (id) ON DELETE SET NULL,   -- входящее, на которое справка
    kind           text NOT NULL DEFAULT 'contact' CHECK (kind IN ('contact', 'overdue')),
    sent_at        timestamptz NOT NULL DEFAULT now(),
    snoozed_until  timestamptz
);
CREATE INDEX briefs_subject ON briefs (subject_key, sent_at DESC);

-- Сигнал о просрочке уходит по договорённости один раз на срок: перенесли срок — сигнал снова возможен.
ALTER TABLE commitments ADD COLUMN overdue_signaled_due date;
