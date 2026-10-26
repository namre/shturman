-- «Отправить» под напоминанием исполнителю (решение владельца 26.10.2026, Р-110).
--
-- Нажатие владельца «Отправить» под напоминанием создаёт черновик шлюза отправки с происхождением
-- `reminder` и сразу согласует его тем же кодом, что «Отправить» под черновиком (`drafts.approve`): дальше —
-- тот же отправщик, те же проверки перед отправкой и выключатель `SHTURMAN_SENDING`. Задачи ответа у такого
-- черновика нет. У напоминания — номер черновика: второе «Отправить» того же напоминания не отправит дважды.
ALTER TABLE outbox_drafts DROP CONSTRAINT outbox_drafts_origin_check;
ALTER TABLE outbox_drafts ADD CONSTRAINT outbox_drafts_origin_check
    CHECK (origin IN ('agent', 'autoreply', 'suggest', 'reminder'));
ALTER TABLE outbox_drafts ADD CONSTRAINT outbox_drafts_reminder_shape
    CHECK (origin <> 'reminder' OR task_id IS NULL);
ALTER TABLE reminders ADD COLUMN draft_id bigint REFERENCES outbox_drafts (id) ON DELETE SET NULL;
