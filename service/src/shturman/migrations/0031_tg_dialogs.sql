-- Список диалогов аккаунта Telegram в базе (решение Р-81, tg/dialogs.py).
--
-- Раньше список жил только в памяти сервиса и грузился, пока ждал запрос страницы: у большого
-- аккаунта это минуты, а при «подождите» от Telegram загруженное пропадало. Теперь загрузка идёт
-- в фоне и пишет каждую страницу сюда; экран и приложение читают отсюда, в том числе сразу после
-- перезапуска сервиса.
--
-- Это данные владельца, как таблица `chats`: названия и @имена всех диалогов аккаунта, а не только
-- выбранных. Наружу таблица не отдаётся (только страница настройки, приложение «Штурман» и
-- внутренний API — тем же списком, что раньше из памяти). При выходе из аккаунта строки удаляются,
-- при удалении аккаунта из архива — каскадом.
--
-- Строка — диалог, каким его видел Telegram при последней загрузке. Диалог, которого в полной
-- загрузке не оказалось, удаляется отсюда; архив сообщений (`chats`, `messages`) это не трогает.
-- Ключ (account_id, peer_class, tg_id) тот же, что у `tg_sync_chats`, — на него можно ссылаться
-- будущим группам чатов (папки Telegram, свои сферы).
CREATE TABLE tg_dialogs (
    account_id      bigint  NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    peer_class      text    NOT NULL CHECK (peer_class IN ('user', 'chat', 'channel')),
    tg_id           bigint  NOT NULL,
    chat_type       text    NOT NULL,          -- как в экспорте: personal_chat, private_group, public_channel…
    title           text,
    username        text,
    is_bot          boolean,
    -- название и @имя в нижнем регистре (casefold Python) — для поиска подстрокой, одинаково при любой
    -- локали базы
    search          text    NOT NULL DEFAULT '',
    top_message     bigint  NOT NULL DEFAULT 0,  -- номер последнего сообщения диалога
    last_message_at timestamptz,               -- время последнего сообщения
    folder_id       integer NOT NULL DEFAULT 0,  -- папка Telegram: 0 — основной список, 1 — архив
    pinned          boolean NOT NULL DEFAULT false,
    position        integer NOT NULL,          -- место в списке Telegram при загрузке (закреплённые — первые)
    load_id         bigint  NOT NULL,          -- номер загрузки, в которой диалог видели последний раз
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, peer_class, tg_id)
);
CREATE INDEX tg_dialogs_order ON tg_dialogs (account_id, position);

-- Сведения о загрузке списка аккаунта: когда он загружен целиком и сколько диалогов называл Telegram.
CREATE TABLE tg_dialog_lists (
    account_id    bigint PRIMARY KEY REFERENCES accounts (id) ON DELETE CASCADE,
    load_id       bigint NOT NULL DEFAULT 0,   -- номер последней начатой загрузки
    started_at    timestamptz,
    completed_at  timestamptz,                 -- последняя загрузка, дошедшая до конца
    expected      integer                      -- сколько диалогов назвал Telegram (count)
);
