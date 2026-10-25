-- Напоминание исполнителю (записка «держать, подкладывать, сигналить»; решение владельца 25.10.2026).
--
-- Под договорённостью «вам обещали» в справке и в сигнале о просрочке — кнопка «Напомнить». Нажатие
-- владельца собирает текст напоминания по шаблону (без модели) и показывает его в боте согласований; куда
-- его положить — решает код: личный чат основного аккаунта с исполнителем («В чат», поле ввода), иначе —
-- «Скопировать» (чат аккаунта помощника или группа, где договорились). Сервис ничего не отправляет.
--
-- Строка живёт сутки (удаляется в часы сводки): за это время владелец кладёт текст или копирует его.
CREATE TABLE reminders (
    id             bigserial   PRIMARY KEY,
    commitment_id  bigint      NOT NULL REFERENCES commitments (id) ON DELETE CASCADE,
    chat_id        bigint      REFERENCES chats (id) ON DELETE CASCADE,
    route          text        NOT NULL CHECK (route IN ('put', 'helper', 'group', 'none')),
    nonce          text        NOT NULL,
    text           text        NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    put_started_at timestamptz,
    placed_at      timestamptz
);
CREATE INDEX reminders_created ON reminders (created_at);
CREATE INDEX reminders_commitment ON reminders (commitment_id);
CREATE INDEX reminders_put ON reminders (put_started_at) WHERE put_started_at IS NOT NULL;
