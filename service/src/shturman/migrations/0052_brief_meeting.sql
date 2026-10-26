-- Справка перед встречей (записка «держать, подкладывать, сигналить», шаг 7).
--
-- Принятая договорённость о созвоне или встрече со временем на сегодня — за полчаса до неё владельцу
-- приходит справка о человеке, как при контакте. По договорённости — одна справка на её время
-- (`commitment_id`, `meeting_at`): перенесли время — справка снова возможна.
ALTER TABLE briefs DROP CONSTRAINT briefs_kind_check;
ALTER TABLE briefs ADD CONSTRAINT briefs_kind_check CHECK (kind IN ('contact', 'overdue', 'meeting'));
ALTER TABLE briefs
    ADD COLUMN commitment_id bigint REFERENCES commitments (id) ON DELETE CASCADE,
    ADD COLUMN meeting_at    timestamptz;
CREATE UNIQUE INDEX briefs_meeting ON briefs (commitment_id, meeting_at) WHERE kind = 'meeting';
CREATE INDEX briefs_sent ON briefs (sent_at);
