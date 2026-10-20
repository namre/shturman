-- Глубина загрузки истории. Включение большого чата не должно означать «вся история без предела».

-- Глубина по умолчанию для аккаунта, в месяцах: применяется к чату в момент его включения.
-- NULL — без предела (вся история).
ALTER TABLE tg_sessions
    ADD COLUMN backfill_months integer DEFAULT 12
        CHECK (backfill_months IS NULL OR backfill_months BETWEEN 1 AND 600);

-- Граница для конкретного чата: сообщения старше неё вглубь не загружаются. NULL — вся история.
-- У чатов, включённых до этой миграции, остаётся NULL: их поведение задним числом не меняется.
ALTER TABLE tg_sync_chats
    ADD COLUMN backfill_since timestamptz;
