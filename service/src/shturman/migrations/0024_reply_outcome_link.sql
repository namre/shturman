-- Link each task to its one terminal metric, so late delivery corrects that metric.
ALTER TABLE reply_tasks ADD COLUMN outcome_log_id bigint
    REFERENCES outbox_autoreply_log(id) ON DELETE SET NULL;
ALTER TABLE reply_tasks ADD CONSTRAINT reply_tasks_outcome_log_unique UNIQUE (outcome_log_id);

-- Older unknown-delivery events can be linked only when the transaction timestamp
-- and account/chat identify exactly one task and exactly one event. Ambiguities stay unlinked.
WITH candidates AS (
    SELECT t.id AS task_id,l.id AS log_id,
           count(*) OVER (PARTITION BY t.id) AS task_matches,
           count(*) OVER (PARTITION BY l.id) AS log_matches
    FROM reply_tasks t JOIN outbox_autoreply_log l
      ON l.account_id=t.account_id AND l.chat_id=t.chat_id AND l.created_at=t.updated_at
    WHERE t.status='failed' AND t.error_code='outcome_unknown'
      AND l.outcome='failed' AND l.reason='outcome_unknown'
)
UPDATE reply_tasks t SET outcome_log_id=c.log_id FROM candidates c
WHERE t.id=c.task_id AND c.task_matches=1 AND c.log_matches=1;
