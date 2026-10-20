-- Пакет 2 ревизии удобства «Ответить и остановить» (решения Р-87, Р-88, Р-89).
--
-- 1. «Остановить всё» (Р-87). Один флаг на сервер: строка `assistant_halt`. Пока он стоит, ассистент
--    ничего не делает сам: не заводит задач ответа и вопросов, не готовит черновиков и репетиций, не
--    разбирает срабатывания наблюдателя, не просит подтверждений действий и не отправляет. Флаг сильнее
--    любых настроек, а сами настройки не трогаются: после «Продолжить» возвращается ровно то, что было.
--    Проверяет его код в каждой точке действия (`halt.py`); триггеры ниже — вторая линия: запись,
--    которая начала бы действие, в базу не попадёт, даже если какую-то точку код пропустил.
--    Внутренний API флаг не меняет: остановка — со страницы, из приложения и в боте согласований,
--    продолжение — только нажатием владельца в боте.
CREATE TABLE assistant_halt (
    id               smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    stopped          boolean NOT NULL DEFAULT false,
    since            timestamptz,
    -- откуда остановили: page — «Настройки Штурмана», app — приложение «Штурман», bot — бот согласований
    via              text CHECK (via IS NULL OR via IN ('page', 'app', 'bot')),
    -- аккаунты Telegram, поставленные на паузу галочкой «И перестать читать переписку»: «Продолжить»
    -- снимает их паузу, только если владелец отметит это отдельно
    paused_accounts  bigint[] NOT NULL DEFAULT '{}',
    -- метка кнопок «Продолжить» в сообщении бота (новая при каждом вопросе «продолжить?»)
    resume_nonce     text,
    resumed_at       timestamptz,
    updated_at       timestamptz NOT NULL DEFAULT now()
);
INSERT INTO assistant_halt (id) VALUES (1);

CREATE FUNCTION shturman_halted() RETURNS boolean LANGUAGE sql STABLE AS
$$ SELECT COALESCE((SELECT stopped FROM assistant_halt WHERE id = 1), false) $$;

CREATE FUNCTION shturman_halt_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF shturman_halted() THEN
        RAISE EXCEPTION 'assistant_stopped' USING ERRCODE = '55000', DETAIL = TG_TABLE_NAME;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER reply_tasks_halt BEFORE INSERT ON reply_tasks
    FOR EACH ROW EXECUTE FUNCTION shturman_halt_guard();
CREATE TRIGGER outbox_drafts_halt BEFORE INSERT ON outbox_drafts
    FOR EACH ROW EXECUTE FUNCTION shturman_halt_guard();
-- согласование и начало отправки черновика
CREATE TRIGGER outbox_drafts_halt_send BEFORE UPDATE OF status ON outbox_drafts
    FOR EACH ROW WHEN (NEW.status IN ('approved', 'sending') AND OLD.status IS DISTINCT FROM NEW.status)
    EXECUTE FUNCTION shturman_halt_guard();
CREATE TRIGGER rehearsal_items_halt BEFORE INSERT ON rehearsal_items
    FOR EACH ROW EXECUTE FUNCTION shturman_halt_guard();
CREATE TRIGGER watch_hits_halt BEFORE INSERT ON watch_hits
    FOR EACH ROW EXECUTE FUNCTION shturman_halt_guard();
-- подтверждения действий; «Продолжить» и включение входа по коду просит сам владелец — их карточки нужны
CREATE TRIGGER pending_actions_halt BEFORE INSERT ON pending_actions
    FOR EACH ROW WHEN (NEW.kind NOT IN ('halt.resume', 'setup.code_login'))
    EXECUTE FUNCTION shturman_halt_guard();
CREATE TRIGGER jobs_halt BEFORE INSERT ON jobs
    FOR EACH ROW WHEN (NEW.kind = 'business.send')
    EXECUTE FUNCTION shturman_halt_guard();

-- 2. Вход на страницу настройки по коду из бота (Р-88). Строка — одна попытка: код запрошен из одного
--    браузера и годится только в нём. Код и идентификатор попытки не хранятся: только HMAC кода ключом
--    из каталога данных (ключа в базе нет) и SHA-256 идентификатора попытки, который лежит у браузера
--    в cookie `HttpOnly; Secure; SameSite=Strict` с узким путём. Адрес и браузер, из которых запросили
--    код, сюда не пишутся — они есть только в сообщении владельцу в боте.
--
--   delivered — код ушёл владельцу в бот. Попытка без доставки (бота нет, владелец не привязан, вход по
--               коду выключен, Telegram не принял сообщение) выглядит снаружи так же, но не сработает;
--   cancelled — reason: superseded (тот же браузер запросил новый), tries (5 неверных), not_me («Это не
--               я»), logout_all («Выйти на всех устройствах»), link (вход по ссылке), expired, disabled.
CREATE TABLE setup_codes (
    id             bigserial PRIMARY KEY,
    attempt_hash   text NOT NULL UNIQUE,
    code_digest    text NOT NULL,
    delivered      boolean NOT NULL DEFAULT false,
    tries          integer NOT NULL DEFAULT 0,
    nonce          text NOT NULL,
    message_id     bigint,
    created_at     timestamptz NOT NULL DEFAULT now(),
    expires_at     timestamptz NOT NULL,
    used_at        timestamptz,
    cancelled_at   timestamptz,
    cancel_reason  text
);
CREATE INDEX setup_codes_created ON setup_codes (created_at);

-- 3. Кнопки у уведомлений (Б14): метка кнопок и повтор через час. Повтор — та же строка очереди, снова
--    «ждёт» со сроком через час; уходит, только если владелец к тому времени не ответил в этом чате.
ALTER TABLE notify_items ADD COLUMN nonce text;
ALTER TABLE notify_items ADD COLUMN reminder boolean NOT NULL DEFAULT false;

-- 4. Репетиция (Б14, П4): свой вариант владельца после «Почти — поправил бы» — слова самого владельца,
--    хранятся у строки репетиции и удаляются вместе с ней (30 дней); разовая репетиция по кнопке
--    «Подготовить ответ» у уведомления (on_demand) — даже если человека нет в списке.
ALTER TABLE rehearsal_items ADD COLUMN owner_variant text;
ALTER TABLE rehearsal_items ADD COLUMN owner_variant_at timestamptz;
ALTER TABLE rehearsal_items ADD COLUMN on_demand boolean NOT NULL DEFAULT false;
ALTER TABLE rehearsal_items ADD CONSTRAINT rehearsal_items_variant_len
    CHECK (owner_variant IS NULL OR char_length(owner_variant) <= 2000);
