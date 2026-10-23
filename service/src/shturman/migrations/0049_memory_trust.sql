-- Правила доверия к памяти и минимум истории (записка «держать, подкладывать, сигналить», шаг 1).
--
-- Значимое значение проекта (реквизиты, бюджет, цена, подрядчик, ответственный, срок) меняет только
-- владелец: сказанное другими ждёт его решения (status proposed), а если расходится с действующим —
-- помечено противоречием (conflicts_with). Значения проекта больше не сменяют друг друга сами по ключу:
-- у проекта несколько работ, и «бюджет» кровли закрывал «бюджет» электрики. Сменяет только «Принять»
-- владельца под карточкой противоречия.
--
-- У каждой версии — основание (basis), вид смены (change_kind), когда применено (applied_at) и когда
-- стало верно (effective_from; NULL — не определено). Когда ассистент узнал — created_at, как и раньше.
ALTER TABLE facts
    ADD COLUMN basis          text CHECK (basis IN ('owner', 'self', 'member', 'said', 'approved')),
    ADD COLUMN change_kind    text NOT NULL DEFAULT 'change' CHECK (change_kind IN ('change', 'correction')),
    ADD COLUMN effective_from date,
    ADD COLUMN applied_at     timestamptz,
    ADD COLUMN chained        boolean NOT NULL DEFAULT true,
    ADD COLUMN conflicts_with bigint REFERENCES facts (id) ON DELETE SET NULL;

-- Записанное раньше: применено тогда, когда записано или решено; основание не восстановить (NULL).
UPDATE facts SET applied_at = COALESCE(decided_at, created_at) WHERE status = 'active';
-- Значимые значения проекта, записанные раньше, тоже выходят из смены по ключу: иначе значение, которое
-- владелец заменил кнопкой «Принять», открыла бы снова следующая пересборка ключа. Что уже закрыто — остаётся
-- закрытым, что действует — действует.
UPDATE facts SET chained = false
 WHERE subject_type = 'project' AND slot IN ('реквизиты', 'бюджет', 'цена', 'подрядчик', 'ответственный', 'срок');

CREATE INDEX facts_project_proposed ON facts (project_id) WHERE subject_type = 'project' AND status = 'proposed';
