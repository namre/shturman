-- Сферы: разделение работы и жизни владельца («Работа РАЗУМ», «Личное», «Преподавание», проекты по ЖК…).
-- Решение Р-82, docs/spheres.md. Шаг 1: сферы, чаты в сферах, проект → сфера, папки Telegram.
--
-- Что здесь НЕ делается (шаги 2–4, поверх этой схемы): своя обработка на сферу, своя модель, сводки по
-- сферам, строгая граница для ответов собеседникам, удаление и выгрузка сферы целиком.

-- ---------------------------------------------------------------------------
-- Сферы
-- ---------------------------------------------------------------------------

-- Сферу заводит только владелец (на странице настройки, в приложении «Штурман» или через ассистента —
-- карточкой в боте согласований). Название показывается ассистенту: по нему он сужает поиск.
CREATE TABLE spheres (
    id           bigserial PRIMARY KEY,
    title        text NOT NULL CHECK (length(title) BETWEEN 1 AND 60),
    -- ключ сравнения названий: нижний регистр, ё → е, пробелы схлопнуты; одно название — одна сфера
    title_norm   text NOT NULL UNIQUE CHECK (title_norm <> ''),
    description  text CHECK (description IS NULL OR length(description) <= 300),
    position     integer NOT NULL DEFAULT 0,     -- порядок в списках (меньше — выше)
    -- Место под настройки шагов 2–4: своя обработка (обязательства, люди, проекты, фото, голосовые),
    -- своя модель, строгая граница для ответов. В шаге 1 сервис этот столбец не читает и не пишет.
    settings     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- Чат в сфере. Чат может быть в нескольких сферах или ни в одной.
--
-- Ключ — диалог аккаунта Telegram (account_id, peer_class, tg_id): тот же, что у `tg_dialogs` и
-- `tg_sync_chats`. Не `chats.id`: запись в `chats` появляется, только когда чат выбран для чтения (или
-- пришёл из выгрузки), а сферу владелец назначает и чату, который ещё не читается, — например, всей
-- папке Telegram сразу. Заводить для этого запись в `chats` нельзя: такой чат стал бы виден ассистенту
-- (list_chats) с названием, хотя владелец его не выбирал. С архивом ключ сходится через
-- chats.account_id + peers (class, tg_id) — у любого чата архива, откуда бы он ни пришёл.
--
-- Внешнего ключа на `tg_dialogs` нет намеренно: строка списка диалогов исчезает, когда чата нет в
-- очередной полной загрузке (вышли из группы), а решение владельца «этот чат — в сфере» остаётся.
-- Удаление аккаунта из архива уносит и его чаты из сфер (каскад).
CREATE TABLE sphere_chats (
    sphere_id   bigint NOT NULL REFERENCES spheres (id) ON DELETE CASCADE,
    account_id  bigint NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    peer_class  text   NOT NULL CHECK (peer_class IN ('user', 'chat', 'channel')),
    tg_id       bigint NOT NULL,
    -- owner — отнёс владелец; folder — пришёл из папки Telegram («Сделать сферу из папки» или принятое
    -- предложение связанной папки)
    origin      text   NOT NULL DEFAULT 'owner' CHECK (origin IN ('owner', 'folder')),
    added_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (sphere_id, account_id, peer_class, tg_id)
);
CREATE INDEX sphere_chats_chat ON sphere_chats (account_id, peer_class, tg_id);

-- Проект — не больше чем в одной сфере (необязательно). Удаление сферы снимает её с проекта.
ALTER TABLE projects ADD COLUMN sphere_id bigint REFERENCES spheres (id) ON DELETE SET NULL;
CREATE INDEX projects_sphere ON projects (sphere_id) WHERE sphere_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Папки Telegram
-- ---------------------------------------------------------------------------

-- Для правил папок («контакты», «без звука», «прочитанные») — сведения из того же ответа
-- messages.getDialogs, что и остальная строка. Это снимок на момент загрузки списка.
ALTER TABLE tg_dialogs ADD COLUMN is_contact boolean;                    -- собеседник в контактах (только user)
ALTER TABLE tg_dialogs ADD COLUMN muted_until timestamptz;               -- уведомления чата выключены до
ALTER TABLE tg_dialogs ADD COLUMN unread boolean NOT NULL DEFAULT false; -- есть непрочитанное или метка «непрочитано»

-- Папки аккаунта (messages.getDialogFilters), загружаются той же фоновой загрузкой, что и список
-- диалогов (Р-81). Только описание папки: какие чаты в неё входят явно и по каким правилам. Состав
-- вычисляется сервисом по списку диалогов (`spheres.folder_members`).
CREATE TABLE tg_folders (
    account_id  bigint  NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    folder_id   integer NOT NULL,                 -- номер папки в Telegram (2…255)
    title       text    NOT NULL,
    emoticon    text,
    -- filter — обычная папка; chatlist — общая папка по ссылке (у неё только явный перечень чатов)
    kind        text    NOT NULL CHECK (kind IN ('filter', 'chatlist')),
    position    integer NOT NULL,                 -- место в списке папок Telegram
    -- явные перечни чатов: элементы вида 'user:2001', 'chat:3001', 'channel:4002'
    pinned      text[]  NOT NULL DEFAULT '{}',
    include     text[]  NOT NULL DEFAULT '{}',
    exclude     text[]  NOT NULL DEFAULT '{}',
    -- правила папки: contacts, non_contacts, groups, broadcasts, bots, exclude_muted, exclude_read,
    -- exclude_archived
    flags       text[]  NOT NULL DEFAULT '{}',
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, folder_id)
);

-- Сфера, сделанная из папки или связанная с ней. propose = true — «Связать папку со сферой»: чаты,
-- которые появятся в папке, сервис ПРЕДЛАГАЕТ добавить в сферу (на странице и в приложении), а не
-- добавляет молча. propose = false — сфера сделана из папки разовым действием; повторное «Сделать
-- сферу из папки» дополняет эту же сферу. Папка, исчезнувшая в Telegram, оставляет связь (видно
-- «папки больше нет»), её название на момент связи — в folder_title.
CREATE TABLE sphere_folders (
    sphere_id     bigint  NOT NULL REFERENCES spheres (id) ON DELETE CASCADE,
    account_id    bigint  NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    folder_id     integer NOT NULL,
    propose       boolean NOT NULL DEFAULT false,
    folder_title  text,
    linked_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (sphere_id, account_id, folder_id)
);

-- Чаты, которые владелец не хочет видеть в сфере: отказался от предложения связанной папки или сам
-- убрал чат из сферы. Такие чаты связанная папка больше не предлагает. Добавление чата в сферу вручную
-- снимает отметку.
CREATE TABLE sphere_declined (
    sphere_id    bigint NOT NULL REFERENCES spheres (id) ON DELETE CASCADE,
    account_id   bigint NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    peer_class   text   NOT NULL CHECK (peer_class IN ('user', 'chat', 'channel')),
    tg_id        bigint NOT NULL,
    declined_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (sphere_id, account_id, peer_class, tg_id)
);
