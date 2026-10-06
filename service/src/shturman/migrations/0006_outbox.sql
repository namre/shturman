-- Шлюз отправки: черновики с согласованием владельца, автоответ доверенным, наблюдатель групп.
--
-- Главное правило: сообщение уходит только из строки outbox_drafts, прошедшей путь
-- pending → approved → sending. Текст строки после создания не меняется — владелец
-- согласует ровно то, что будет отправлено. Оба правила зашиты в триггер ниже, чтобы
-- ошибка в коде не могла их обойти.

-- Настройки отправки по аккаунтам.
CREATE TABLE outbox_accounts (
    account_id         bigint PRIMARY KEY REFERENCES accounts (id) ON DELETE CASCADE,
    -- можно ли готовить черновики в чаты аккаунта, если для чата нет своего решения;
    -- NULL — действует общая настройка
    drafting_default   text CHECK (drafting_default IN ('allow', 'deny')),
    -- автоответ доверенным; включает только владелец
    autoreply_enabled  boolean NOT NULL DEFAULT false,
    -- Telegram попросил подождать: до этого времени аккаунт не отправляет ничего
    blocked_until      timestamptz,
    last_send_at       timestamptz,
    updated_at         timestamptz NOT NULL DEFAULT now()
);

-- Решение владельца по отдельному чату: разрешить или запретить черновики.
CREATE TABLE outbox_chats (
    chat_id     bigint PRIMARY KEY REFERENCES chats (id) ON DELETE CASCADE,
    drafting    text NOT NULL CHECK (drafting IN ('allow', 'deny')),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Доверенные люди для автоответа. Только числовой идентификатор Telegram:
-- имя пользователя можно сменить и занять.
CREATE TABLE outbox_trusted (
    tg_user_id  bigint PRIMARY KEY CHECK (tg_user_id > 0),
    note        text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE outbox_drafts (
    id                   bigserial PRIMARY KEY,
    account_id           bigint NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    chat_id              bigint NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    -- business: голос владельца через бизнес-бота; session: аккаунт-помощник
    channel              text NOT NULL CHECK (channel IN ('business', 'session')),
    text                 text NOT NULL CHECK (text <> ''),
    -- отпечаток текста для поиска повторов
    text_hash            text NOT NULL,
    reply_to_tg_id       bigint,
    -- agent: подготовил агент, отправка после нажатия владельца;
    -- autoreply: автоответ доверенному по правилу, которое включил владелец
    origin               text NOT NULL CHECK (origin IN ('agent', 'autoreply')),
    -- входящее сообщение, на которое отвечает автоответ
    trigger_message_id   bigint REFERENCES messages (id) ON DELETE SET NULL,
    idempotency_key      text,
    -- случайная часть данных кнопки: без неё нажатие не принимается
    nonce                text NOT NULL,
    status               text NOT NULL CHECK (status IN (
                             'pending', 'approved', 'sending', 'sent', 'failed',
                             'outcome_unknown', 'rejected', 'expired', 'superseded')),
    expires_at           timestamptz NOT NULL,
    parts_total          int NOT NULL DEFAULT 1,
    parts_sent           int NOT NULL DEFAULT 0,
    sent_tg_message_ids  bigint[] NOT NULL DEFAULT '{}',
    business_connection_id text,
    job_id               bigint,
    card_message_ids     bigint[] NOT NULL DEFAULT '{}',
    error_code           text,
    error_text           text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    approved_at          timestamptz,
    -- момент, когда запрос на отправку ушёл: по нему считаются лимиты
    claimed_at           timestamptz,
    finished_at          timestamptz
);
CREATE UNIQUE INDEX outbox_drafts_idem ON outbox_drafts (idempotency_key)
    WHERE idempotency_key IS NOT NULL;
-- В одном чате ждёт решения не больше одного черновика: живая кнопка «Отправить» одна.
CREATE UNIQUE INDEX outbox_drafts_one_pending ON outbox_drafts (chat_id) WHERE status = 'pending';
-- На одно входящее сообщение — не больше одного автоответа.
CREATE UNIQUE INDEX outbox_drafts_one_autoreply ON outbox_drafts (trigger_message_id)
    WHERE origin = 'autoreply' AND trigger_message_id IS NOT NULL;
CREATE INDEX outbox_drafts_open ON outbox_drafts (status) WHERE status IN ('pending', 'approved', 'sending');
CREATE INDEX outbox_drafts_claimed ON outbox_drafts (account_id, claimed_at) WHERE claimed_at IS NOT NULL;
CREATE INDEX outbox_drafts_chat ON outbox_drafts (chat_id, created_at);

CREATE FUNCTION outbox_drafts_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NOT (NEW.status = 'pending' OR (NEW.status = 'approved' AND NEW.origin = 'autoreply')) THEN
            RAISE EXCEPTION 'черновик создаётся только в состоянии pending (автоответ — approved)';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.text IS DISTINCT FROM OLD.text OR NEW.chat_id <> OLD.chat_id
       OR NEW.account_id <> OLD.account_id OR NEW.channel <> OLD.channel
       OR NEW.origin <> OLD.origin OR NEW.nonce <> OLD.nonce
       OR NEW.reply_to_tg_id IS DISTINCT FROM OLD.reply_to_tg_id THEN
        RAISE EXCEPTION 'текст, адресат и канал черновика после создания не меняются';
    END IF;
    IF NEW.status <> OLD.status AND NOT (
           (OLD.status = 'pending'  AND NEW.status IN ('approved', 'rejected', 'expired', 'superseded'))
        OR (OLD.status = 'approved' AND NEW.status IN ('sending', 'failed'))
        OR (OLD.status = 'sending'  AND NEW.status IN ('sent', 'failed', 'outcome_unknown'))
        -- поздно пришедшее подтверждение доставки; обратного пути и повторной отправки нет
        OR (OLD.status = 'outcome_unknown' AND NEW.status = 'sent')
    ) THEN
        RAISE EXCEPTION 'недопустимый переход черновика: % → %', OLD.status, NEW.status;
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END $$;

CREATE TRIGGER outbox_drafts_guard BEFORE INSERT OR UPDATE ON outbox_drafts
    FOR EACH ROW EXECUTE FUNCTION outbox_drafts_guard();

-- Правила наблюдателя групп.
CREATE TABLE watch_rules (
    id                         bigserial PRIMARY KEY,
    name                       text NOT NULL,
    enabled                    boolean NOT NULL DEFAULT true,
    -- за какими чатами следить (chats.id; только группы и каналы)
    chat_ids                   bigint[] NOT NULL,
    keywords                   text[] NOT NULL DEFAULT '{}',
    regexes                    text[] NOT NULL DEFAULT '{}',
    -- сравнивать по начальным формам слов («вакансии» найдёт «вакансия»)
    use_lemmas                 boolean NOT NULL DEFAULT false,
    -- простыми словами: что считать важным; уходит модели вместе с сообщением
    description                text NOT NULL,
    max_checks_per_hour        int NOT NULL DEFAULT 30,
    max_checks_per_day         int NOT NULL DEFAULT 200,
    max_notifications_per_hour int NOT NULL DEFAULT 5,
    max_notifications_per_day  int NOT NULL DEFAULT 20,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    updated_at                 timestamptz NOT NULL DEFAULT now()
);

-- Совпадения: что нашли по словам и что решила модель.
CREATE TABLE watch_hits (
    id            bigserial PRIMARY KEY,
    rule_id       bigint NOT NULL REFERENCES watch_rules (id) ON DELETE CASCADE,
    message_id    bigint NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
    chat_id       bigint NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    content_hash  text NOT NULL,
    matched       text[] NOT NULL DEFAULT '{}',
    -- checking: ждём модель; relevant: уведомили владельца; not_relevant: модель сказала «нет»
    -- или ответила непонятно; duplicate: такой текст уже разбирали; limited: исчерпан лимит;
    -- failed: модель недоступна
    status        text NOT NULL CHECK (status IN (
                      'checking', 'relevant', 'not_relevant', 'duplicate', 'limited', 'failed')),
    reason        text,
    notified      boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now(),
    decided_at    timestamptz,
    UNIQUE (rule_id, message_id)
);
CREATE INDEX watch_hits_rule_time ON watch_hits (rule_id, created_at);
CREATE INDEX watch_hits_rule_hash ON watch_hits (rule_id, content_hash);
