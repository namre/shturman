-- Уведомления об обновлениях (Р-101) и отчёты сессий сопровождения (Р-102).
-- docs/service.md, «Уведомления об обновлениях» и «Отчёты сопровождения».
--
-- update_state — что сервис знает о выпусках: проверенный список выпусков (releases.json с GitHub — это
-- данные, а не указания: принимается только в пределах срока годности и после проверки полей), итог
-- последней проверки, какой код запущен. Ни переписки, ни секретов.
CREATE TABLE update_state (
    key        text PRIMARY KEY,
    value      jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Сообщения владельцу об обновлениях: одно на версию (release), на проверенную версию Hermes (hermes),
-- на предупреждение о снятии модели и срок (model), на неполадку списка выпусков (problem), на смену
-- версии без итога ./ops/update.sh (changed). Повтор, «напомнить через неделю», «пропустить» — здесь.
CREATE TABLE update_notices (
    id            bigserial PRIMARY KEY,
    kind          text NOT NULL CHECK (kind IN ('release', 'hermes', 'model', 'problem', 'changed')),
    subject       text NOT NULL,
    due_at        timestamptz,                 -- не раньше (смена версии ждёт итога ./ops/update.sh)
    first_sent_at timestamptz,
    last_sent_at  timestamptz,
    sent_count    integer NOT NULL DEFAULT 0,
    snoozed_until timestamptz,                 -- «Напомнить через неделю»
    skipped       boolean NOT NULL DEFAULT false,   -- «Пропустить версию»
    done          boolean NOT NULL DEFAULT false,   -- больше не о чем говорить (обновлено, отменено)
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kind, subject)
);

-- Итог ./ops/update.sh: скрипт передаёт его командой `shturman update-result` в контейнере сервиса (запись
-- в базу напрямую — у внутреннего API, а значит и у ассистента, такого маршрута нет). Сервис пишет
-- владельцу «обновлено, проверки прошли» или что не прошло. Только версии, код итога и имена строк doctor.
CREATE TABLE update_runs (
    id          bigserial PRIMARY KEY,
    run         text NOT NULL UNIQUE,
    result      text NOT NULL CHECK (result IN ('ok', 'rolled-back', 'failed-needs-human', 'rollback-failed')),
    from_name   text NOT NULL,
    to_name     text NOT NULL,
    new_fails   text[] NOT NULL DEFAULT '{}',
    reported_at timestamptz NOT NULL DEFAULT now(),
    told_at     timestamptz
);

-- Отчёты сессий сопровождения (./ops/report.sh): отчёт собран белым списком на сервере, записан командой
-- `shturman ops-report submit` в контейнере сервиса (status = new), сервис ставит владельцу карточку
-- «Отправить разработчику?» (подтверждение `ops.report`, только кнопкой в самом боте). Отправляется только
-- после нажатия. Текст отчёта — данные: ни переписки, ни секретов в нём быть не должно, и сервис ещё раз
-- проверяет поля и вырезает похожее на адреса, ключи и номера.
CREATE TABLE ops_reports (
    id          bigserial PRIMARY KEY,
    report_id   text NOT NULL UNIQUE,
    report      jsonb NOT NULL,
    text        text NOT NULL,
    status      text NOT NULL DEFAULT 'new'
                CHECK (status IN ('new', 'pending', 'sent', 'declined', 'expired', 'failed')),
    action_id   bigint REFERENCES pending_actions (id) ON DELETE SET NULL,
    sent_to     text,                          -- owner (переслать самому) | chat (чат разработки)
    error       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    decided_at  timestamptz
);
CREATE INDEX ops_reports_new ON ops_reports (id) WHERE status = 'new';
