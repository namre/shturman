-- Mini App бота согласований (setup_page/miniapp.py): сессии приложения.
--
-- Отдельно от сессий страницы настройки: у приложения своя, более узкая роль — только то, что
-- ждёт решения владельца, и те же кнопки, что в боте. Ключ сессии страницы здесь не действует,
-- и наоборот.
CREATE TABLE miniapp_sessions (
    id             bigserial PRIMARY KEY,
    token_hash     text NOT NULL UNIQUE,          -- SHA-256 ключа сессии; сам ключ — только у приложения
    -- SHA-256 подписи Telegram (поле hash данных входа): одни данные входа — одна сессия.
    -- Запись живёт дольше срока данных входа, поэтому повтор тех же данных не даёт второй сессии.
    init_hash      text NOT NULL UNIQUE,
    bot_id         bigint NOT NULL,                -- бот согласований, которым подписаны данные входа
    user_id        bigint NOT NULL,                -- владелец, привязанный к этому боту
    created_at     timestamptz NOT NULL DEFAULT now(),
    last_seen_at   timestamptz NOT NULL DEFAULT now(),
    expires_at     timestamptz NOT NULL,           -- предел от входа; продлением не сдвигается
    revoked_at     timestamptz
);
CREATE INDEX miniapp_sessions_live ON miniapp_sessions (expires_at) WHERE revoked_at IS NULL;
