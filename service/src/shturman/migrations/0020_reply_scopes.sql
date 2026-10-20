-- Только сессия Telegram даёт достоверную вложенную разметку и признаки сообщения.
-- NULL у прежних строк / экспорта / бизнес-бота — «неизвестно», а не «безопасно».
ALTER TABLE messages ADD COLUMN telegram_entities jsonb;
ALTER TABLE messages ADD COLUMN topic_tg_id bigint CHECK (topic_tg_id > 0);
ALTER TABLE messages ADD COLUMN is_forwarded boolean;
ALTER TABLE messages ADD COLUMN telegram_via_bot boolean;
ALTER TABLE messages ADD COLUMN telegram_sender_bot boolean;

-- Разрешение владельца строго на аккаунт и глобальную сущность Telegram.
-- 0 — весь чат; положительное число — только корень указанной темы форума.
CREATE TABLE outbox_reply_scopes (
    id bigserial PRIMARY KEY,
    account_id bigint NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    peer_id bigint NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    topic_tg_id bigint NOT NULL DEFAULT 0 CHECK (topic_tg_id >= 0),
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_id, peer_id, topic_tg_id)
);

-- Лимиты и паузы меняют updated_at той же строки. Отпечаток согласия не должен
-- меняться от обычной отправки, но отзыв и повторное включение дают новую ревизию.
ALTER TABLE outbox_accounts ADD COLUMN autoreply_revision bigint NOT NULL DEFAULT 0;
CREATE FUNCTION outbox_autoreply_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.autoreply_enabled IS DISTINCT FROM OLD.autoreply_enabled THEN
        NEW.autoreply_revision := OLD.autoreply_revision + 1;
    ELSE
        NEW.autoreply_revision := OLD.autoreply_revision;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER outbox_autoreply_revision BEFORE UPDATE ON outbox_accounts
    FOR EACH ROW EXECUTE FUNCTION outbox_autoreply_revision();
