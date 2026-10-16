-- Качество договорённостей (Р-96, docs/decisions.md; docs/memory.md, «Договорённости»).
--
-- «В команде»: обещание между другими людьми (direction = 'others') больше не ждёт решения владельца —
-- у него свой статус team: только для сведения, без кнопок «Верно / Не так», с «Скрыть».
ALTER TABLE commitments DROP CONSTRAINT commitments_status_check;
ALTER TABLE commitments ADD CONSTRAINT commitments_status_check
    CHECK (status IN ('proposed', 'open', 'done', 'cancelled', 'rejected', 'expired', 'team'));

-- Почему договорённость снята без владельца: done_before_shown / cancelled_before_shown — сделано
-- (отменено) ещё до показа; done_before_decision / cancelled_before_decision — до решения владельца;
-- recheck — заменена перепроверкой по новым правилам; unanswered — осталась без ответа;
-- team_done / team_cancelled / team_stale — «В команде»: сделано, отменено, давно. Только код причины,
-- без текста переписки; подробности (номер сообщения-основания) — в commitment_events.details.
ALTER TABLE commitments ADD COLUMN quiet_reason text;
-- Быстрое обещание («сейчас», «через 10 минут»): не показывается владельцу до этого времени — сутки
-- после обещания; к тому времени проверка по следующим сообщениям тихо закроет сделанное.
ALTER TABLE commitments ADD COLUMN hold_until timestamptz;
-- До какого сообщения чата уже проверено, не сделано ли обещанное (время и номер последнего
-- проверенного). NULL — ещё не проверялось: первая проверка идёт с момента обещания.
ALTER TABLE commitments ADD COLUMN checked_until_at timestamptz;
ALTER TABLE commitments ADD COLUMN checked_until_id bigint;
-- Просьба, на которую обещание — согласие («Ок» на «пришли смету»): сообщение и дословный фрагмент.
ALTER TABLE commitments ADD COLUMN request_message_id bigint REFERENCES messages (id) ON DELETE CASCADE;
ALTER TABLE commitments ADD COLUMN request_quote text;
-- Версия инструкции модели, по которой извлечено (PROMPT_VERSION): для «было / стало» и перепроверки.
ALTER TABLE commitments ADD COLUMN prompt_version text;

CREATE INDEX commitments_request_message ON commitments (request_message_id) WHERE request_message_id IS NOT NULL;
CREATE INDEX commitments_hold ON commitments (hold_until) WHERE status = 'proposed' AND hold_until IS NOT NULL;
CREATE INDEX commitments_team ON commitments (chat_id) WHERE status = 'team';

-- Уже записанные «между другими», ещё не решённые владельцем, переходят в «В команде».
INSERT INTO commitment_events (commitment_id, actor, action, from_status, to_status, details)
SELECT id, 'auto', 'moved_to_team', 'proposed', 'team', '{"reason":"others_not_for_decision"}'::jsonb
FROM commitments WHERE status = 'proposed' AND direction = 'others';
UPDATE commitments SET status = 'team', digest_batch = NULL, digest_pos = NULL, updated_at = now()
WHERE status = 'proposed' AND direction = 'others';

-- Прежние причины снятия без ответа.
UPDATE commitments SET quiet_reason = 'unanswered' WHERE status = 'expired' AND quiet_reason IS NULL;

-- Виды прогона: перепроверка (`shturman commitments recheck`) и дневной частый прогон (hourly).
ALTER TABLE processing_runs DROP CONSTRAINT processing_runs_trigger_check;
ALTER TABLE processing_runs ADD CONSTRAINT processing_runs_trigger_check
    CHECK (trigger IN ('manual', 'nightly', 'recheck', 'hourly'));
-- Дневные пределы запросов считаются по времени постановки запроса.
CREATE INDEX processing_requests_created ON processing_requests (created_at);
