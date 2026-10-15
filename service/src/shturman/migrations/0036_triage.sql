-- Разбор входящих (решение Р-90): вместо пересылки «💬 Вам написали» на каждое сообщение модель
-- разбирает новые входящие и сервис пишет владельцу только о срочном и важном, напоминает о вопросе
-- без ответа и коротко пересказывает обращения в группах.
--
-- Включает только владелец: на странице «Настройки Штурмана» он просит включить, а включает нажатие
-- в боте согласований (подтверждение `triage.enable`). Настройки и правила владельца (важные и тихие
-- люди и группы, важные сферы, «реже беспокоить», срок напоминания) — строка `triage.settings`
-- в `settings` (её нет — разбор выключен).
--
-- Здесь — одна строка на пачку сообщений подряд от одного человека в одном чате (её последнее
-- сообщение — `message_id`). **Текста сообщений нет:** только номера, выводы модели одной строкой
-- («что от вас хотят», срок, суть ветки) и состояние. Строки хранятся 14 дней.
--
--   state:
--     queued    — ждёт, не допишет ли человек ещё (пачка — один разбор);
--     preparing — модель разбирает (job_id — задание llm.structured);
--     done      — разобрано: needs_reply, urgency, want, due, thread;
--     skipped   — не разбиралось: reason — newer (пачку закрыло сообщение новее), answered (вы уже
--                 ответили), stopped («Остановить всё»), disabled (разбор выключили), gone (удалено,
--                 скрыто защитой, чат исключён), no_bot;
--     failed    — не получилось: reason — model_unavailable, invalid_model_result. Сообщения пачки
--                 уходят обычным уведомлением «Вам написали» (notify/).
--   show_state (после разбора): silent — не беспокоить; shown — карточка ушла; deferred — ждёт конца
--     тихих часов (show_at); capped — не ушла сразу из-за предела карточек в час.
--   wait_state (нужен ответ владельца): waiting — ждёт (remind_at — когда напомнить); reminded —
--     напоминание ушло; answered — вы ответили в этом чате; dismissed — «Не важно»; off — разбор
--     выключили.
CREATE TABLE triage_items (
    id               bigserial PRIMARY KEY,
    account_id       bigint NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    chat_id          bigint NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    message_id       bigint NOT NULL UNIQUE REFERENCES messages (id) ON DELETE CASCADE,
    -- personal — личное сообщение; mention — прямое обращение к владельцу в группе
    kind             text   NOT NULL CHECK (kind IN ('personal', 'mention')),
    reason           text   CHECK (reason IS NULL OR reason IN ('mention', 'reply', 'alias')),
    -- кто написал: числовой ID Telegram (для правил «важные/тихие люди»)
    person_tg_id     bigint,
    topic_tg_id      bigint,
    state            text   NOT NULL DEFAULT 'queued'
                     CHECK (state IN ('queued', 'preparing', 'done', 'skipped', 'failed')),
    skip_reason      text,
    -- номера сообщений пачки (без текста): для обычного уведомления, если разбор не состоялся
    batch_ids        bigint[] NOT NULL DEFAULT '{}',
    -- строка, разбор которой закрыл эту (skipped / newer): пачка — один разбор
    covered_by       bigint,
    job_id           bigint,
    asked_at         timestamptz,
    -- выводы модели: одна строка каждый, без цитат сообщения
    needs_reply      boolean,
    urgency          text   CHECK (urgency IS NULL OR urgency IN ('normal', 'important', 'urgent')),
    want             text   CHECK (want IS NULL OR char_length(want) <= 300),
    due              text   CHECK (due IS NULL OR char_length(due) <= 100),
    thread           text   CHECK (thread IS NULL OR char_length(thread) <= 400),
    -- уровень правил владельца в момент разбора: important, normal, lower, quiet
    level            text,
    show_state       text   CHECK (show_state IS NULL OR show_state IN ('silent', 'shown', 'deferred', 'capped')),
    show_at          timestamptz,
    shown_at         timestamptz,
    wait_state       text   CHECK (wait_state IS NULL OR wait_state IN ('waiting', 'reminded', 'answered', 'dismissed', 'off')),
    remind_at        timestamptz,
    reminded_at      timestamptz,
    answered_at      timestamptz,
    -- «Не важно» под карточкой: обратная связь владельца
    feedback         text   CHECK (feedback IS NULL OR feedback = 'not_important'),
    nonce            text   NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX triage_items_created ON triage_items (created_at);
CREATE INDEX triage_items_queued ON triage_items (chat_id) WHERE state IN ('queued', 'preparing');
CREATE INDEX triage_items_waiting ON triage_items (remind_at) WHERE wait_state IN ('waiting', 'reminded');
CREATE INDEX triage_items_deferred ON triage_items (show_at) WHERE show_state = 'deferred';

-- «Остановить всё» (Р-87) останавливает и разбор: пока флаг стоит, новые строки разбора не заводятся
-- (вторая линия после проверки в коде). Сообщения в это время приходят обычными уведомлениями.
CREATE TRIGGER triage_items_halt BEFORE INSERT ON triage_items
    FOR EACH ROW EXECUTE FUNCTION shturman_halt_guard();
