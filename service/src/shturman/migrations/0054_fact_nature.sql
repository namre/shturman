-- Вид сведения и уточнение ключа (Р-111, разбор памяти пилота 26.10.2026).
--
-- nature — что это за сведение:
--   state       — держится, пока не сменится (должность, правило, бюджет): как было до сих пор;
--   event       — однажды произошло («подключил», «торги закрыты»): только в хронологию, действующим не бывает;
--   observation — положение дел на момент («сейчас», «пока», «на 29.09», статус): действует, пока свежее
--                 (facts.OBSERVATION_DAYS), потом остаётся только в хронологии.
-- slot_detail — к чему именно относится значение ключа («статус: закупка электрики»). Факты сменяют друг
-- друга только при совпадении ключа и уточнения (slot_detail_norm — его ключ сравнения; '' — без уточнения).
ALTER TABLE facts
    ADD COLUMN nature           text NOT NULL DEFAULT 'state' CHECK (nature IN ('state', 'event', 'observation')),
    ADD COLUMN slot_detail      text CHECK (slot_detail IS NULL OR length(slot_detail) BETWEEN 1 AND 60),
    ADD COLUMN slot_detail_norm text NOT NULL DEFAULT '';

-- Статус проекта без уточнения — положение дел на момент. Раньше такие факты сменяли друг друга по ключу,
-- даже если речь шла о разных работах; теперь каждый стоит сам по себе и со временем уходит в хронологию.
-- Закрытое только пересборкой ключа (chained) открывается; то, что сменило «Принять» владельца, не трогается.
UPDATE facts SET nature = 'observation'
 WHERE subject_type = 'project' AND slot = 'статус' AND kind = 'fact';
UPDATE facts SET chained = false, valid_to = NULL, superseded_by = NULL
 WHERE subject_type = 'project' AND slot = 'статус' AND kind = 'fact' AND chained AND status = 'active';

-- Записанное раньше с приметами момента («сейчас», «пока», «снова», «на 29.09») — тоже положение дел.
-- Профиль владельца не трогается: его факты он одобрил сам, а лишнее отметит «Неверно».
-- Буквы перечислены явно: классы символов и регистр кириллицы зависят от локали базы.
UPDATE facts SET nature = 'observation'
 WHERE nature = 'state' AND subject_type <> 'owner' AND kind = 'fact'
   AND text ~ '(^|[^А-Яа-яЁёA-Za-z0-9])([Сс]ейчас|[Пп]ока|[Сс]нова|[Оо]пять|[Вв]ременно|[Нн]а сегодня|[Нн]а данный момент|[Нн]а текущий момент|[Вв] данный момент|[Нн]а \d{1,2}\.\d{1,2})([^А-Яа-яЁёA-Za-z0-9]|$)';

DROP INDEX IF EXISTS facts_slot_active;
CREATE INDEX facts_slot_active ON facts (subject_type, person_id, project_id, slot, slot_detail_norm)
    WHERE status = 'active' AND valid_to IS NULL;

UPDATE pages SET dirty = true
 WHERE (entity_type = 'project' AND project_id IN (SELECT project_id FROM facts WHERE nature <> 'state'))
    OR (entity_type = 'person' AND person_id IN (SELECT person_id FROM facts WHERE nature <> 'state'));
