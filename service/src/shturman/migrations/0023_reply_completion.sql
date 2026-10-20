-- Successful delivery is terminal; later task sweeps must not mark it expired.
ALTER TABLE reply_tasks DROP CONSTRAINT reply_tasks_status_check;
ALTER TABLE reply_tasks ADD CONSTRAINT reply_tasks_status_check CHECK (status IN
 ('queued','generating','waiting_source','waiting_owner','drafting','draft_ready',
  'completed','declined','cancelled','expired','failed'));
