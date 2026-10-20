-- «История» и «Вернуть как было» (решение Р-95, service/src/shturman/history.py).
--
-- Одна строка — одно изменение правил, которые решают, что ассистент может: доверенные, автоответ,
-- черновики по чатам, группы и обращения, репетиция, уведомления, разбор входящих, «Остановить всё»,
-- утренняя сводка. Пишет её функция, которая меняет настройку, в той же транзакции.
--
--   area    — что менялось (trusted, autoreply, drafting, groups, addressing, policy, rehearsal, notify,
--             triage, halt, summaries);
--   target  — что именно: «trusted:2044», «drafting:15», «rehearsal». «Вернуть как было» есть только у
--             последней строки каждого target: более ранняя уже перекрыта следующим изменением;
--   title   — что стало, словами сервиса (имена — как в архиве, без текста переписки);
--   via     — откуда: страница, приложение, бот (команда, кнопка, «Да» под карточкой), внутренний API;
--   undo    — как вернуть: {"kind": вид действия, "payload": …} — тем же путём, что любое изменение
--             (`confirm`), а ослабление — только нажатием в боте; NULL — вернуть нельзя;
--   undone_by — строка, которой это изменение вернули.
--
-- Хранится 90 дней и не больше 2000 строк (`history.trim`).
CREATE TABLE settings_history (
    id         bigserial PRIMARY KEY,
    at         timestamptz NOT NULL DEFAULT now(),
    area       text        NOT NULL,
    target     text        NOT NULL,
    title      text        NOT NULL CHECK (char_length(title) <= 600),
    via        text        NOT NULL,
    undo       jsonb,
    undone_by  bigint
);
CREATE INDEX settings_history_target ON settings_history (target, id DESC);
CREATE INDEX settings_history_at ON settings_history (at DESC);
