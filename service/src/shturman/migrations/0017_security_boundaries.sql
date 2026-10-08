-- Согласие не выводится из статуса, decided_at или прежнего actor='owner':
-- до этой миграции агент мог подделать их. Старая история остаётся непроверенной.
ALTER TABLE commitments ADD COLUMN approved_at timestamptz;
ALTER TABLE commitments ADD COLUMN approved_by text;
ALTER TABLE commitments ADD COLUMN approved_via text;
ALTER TABLE commitments ADD COLUMN approval_fingerprint text;
ALTER TABLE commitments ADD COLUMN legacy_unverified boolean NOT NULL DEFAULT false;
ALTER TABLE commitments ADD COLUMN digest_fingerprint text;
ALTER TABLE commitment_changes ADD COLUMN digest_fingerprint text;
ALTER TABLE commitment_events DROP CONSTRAINT commitment_events_actor_check;
ALTER TABLE commitment_events ADD CONSTRAINT commitment_events_actor_check
    CHECK (actor IN ('owner', 'agent', 'auto', 'model'));
INSERT INTO commitment_events (commitment_id, actor, action, from_status, to_status, details)
SELECT id, 'auto', 'legacy_approval_unverified', status, 'proposed',
       '{"reason":"approval_provenance_missing"}'::jsonb
FROM commitments WHERE status = 'open';
-- Не рассылаем архив заново по одному пункту: записи доступны для отдельного пересмотра.
UPDATE commitments SET legacy_unverified = true, status = 'proposed',
    digest_attempts = GREATEST(digest_attempts, 3), notified_at = NULL, digest_batch = NULL
WHERE status = 'open';
UPDATE commitments SET legacy_unverified = true WHERE status IN ('done', 'cancelled');

-- Постоянный реестр точных Telegram user IDs: смена токена/бота не открывает старый чат.
-- Ни имя отправителя, ни username не дают полномочий и не создают этот запрет.
CREATE TABLE control_peers (
    tg_id bigint PRIMARY KEY CHECK (tg_id > 0),
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE pages ADD COLUMN security_quarantined boolean NOT NULL DEFAULT false;

CREATE FUNCTION control_peer_chat_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_advisory_xact_lock_shared(hashtext('shturman.control_peers'));
    IF EXISTS (SELECT 1 FROM peers p JOIN control_peers b ON b.tg_id = p.tg_id
               WHERE p.id = NEW.peer_id AND p.class = 'user') THEN
        NEW.excluded := true;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER control_peer_chat_guard BEFORE INSERT OR UPDATE ON chats
    FOR EACH ROW EXECUTE FUNCTION control_peer_chat_guard();

CREATE FUNCTION control_peer_sync_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.peer_class = 'user' AND EXISTS (SELECT 1 FROM control_peers WHERE tg_id = NEW.tg_id) THEN
        NEW.enabled := false;
        NEW.auto_enabled := false;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER control_peer_sync_guard BEFORE INSERT OR UPDATE ON tg_sync_chats
    FOR EACH ROW EXECUTE FUNCTION control_peer_sync_guard();

INSERT INTO control_peers (tg_id, reason) VALUES
    (777000, 'telegram_service'), (93372553, 'telegram_service'), (178220800, 'telegram_service');
-- Идентификаторы прежней привязки и текущего бота могут различаться: сохраняем оба.
INSERT INTO control_peers (tg_id, reason)
SELECT DISTINCT (CASE WHEN key = 'bot' THEN value->>'id' ELSE value->>'bot_id' END)::bigint,
       'service_bot'
FROM executor_state
WHERE key IN ('bot', 'owner')
  AND (CASE WHEN key = 'bot' THEN value->>'id' ELSE value->>'bot_id' END) ~ '^[1-9][0-9]{0,17}$'
ON CONFLICT DO NOTHING;

UPDATE chats c SET excluded = true FROM peers p JOIN control_peers b ON b.tg_id = p.tg_id
WHERE c.peer_id = p.id AND p.class = 'user';
UPDATE tg_sync_chats SET enabled = false, auto_enabled = false
WHERE peer_class = 'user' AND tg_id IN (SELECT tg_id FROM control_peers);
-- До фоновой перерисовки старые файлы/индекс производной памяти не выдаются.
UPDATE pages SET security_quarantined = true, dirty = true
WHERE id IN (SELECT e.page_id FROM page_entries e JOIN page_entry_sources s ON s.entry_id = e.id
    JOIN messages m ON m.id = s.message_id JOIN chats c ON c.id = m.chat_id
    JOIN peers p ON p.id = c.peer_id JOIN control_peers b ON b.tg_id = p.tg_id WHERE p.class = 'user');
