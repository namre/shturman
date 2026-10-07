-- Свой исполнитель сервиса и подтверждения владельца.

-- Кто выполняет задание: plugin — плагин в Hermes (забирает через API), builtin — сам сервис
-- (свой бот согласований, свой ключ модели). Задания builtin плагину не выдаются.
ALTER TABLE jobs ADD COLUMN executor text NOT NULL DEFAULT 'plugin'
    CHECK (executor IN ('plugin', 'builtin'));

-- Через какого бота пришло бизнес-подключение: через него же идёт отправка от имени владельца.
ALTER TABLE business_connections ADD COLUMN via text NOT NULL DEFAULT 'plugin'
    CHECK (via IN ('plugin', 'service'));

-- Действия, которые расширяют права ассистента или стирают данные, применяются только после
-- нажатия владельца в боте согласований. До нажатия они лежат здесь.
CREATE TABLE pending_actions (
    id          bigserial PRIMARY KEY,
    kind        text NOT NULL,
    summary     text NOT NULL,
    payload     jsonb NOT NULL DEFAULT '{}',
    nonce       text NOT NULL,
    status      text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'applied', 'rejected', 'expired', 'failed')),
    error       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    decided_at  timestamptz,
    -- сообщение с кнопками в боте согласований: чтобы погасить кнопки, когда срок вышел
    card_message_id bigint
);
CREATE INDEX pending_actions_open ON pending_actions (expires_at) WHERE status = 'pending';

-- Виден ли текст сообщения ассистенту и модели. Единственное, что проверяют модули, отдающие
-- переписку наружу. Значение ведёт защита от внедрённых инструкций (см. guard/); пока она
-- не включена, всё видно.
ALTER TABLE messages ADD COLUMN agent_visible boolean NOT NULL DEFAULT true;
