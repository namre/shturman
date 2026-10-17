-- Разрешения владельца на действия сопровождения (Р-100, docs/service.md, «Разрешения на сопровождение»).
--
-- Скрипт ops/ (обновление с откатом базы, восстановление из копии, откат обновления, перезапуск без свежей
-- копии, порог защиты, удаление старых копий) просит разрешение: сервис ставит карточку в бот согласований
-- (подтверждение вида `ops.consent`, только кнопкой в самом боте). Нажатие «Разрешить» переводит строку
-- в granted; скрипт расходует разрешение один раз (used). Маршрута, который ставит granted без нажатия,
-- нет: это делает только функция применения `ops.consent` под нажатием владельца.
--
-- В строке только вид действия, его цель (коммит, метка копии, число) и короткие подробности для карточки
-- (версии, число копий) — ни переписки, ни секретов.
CREATE TABLE ops_consents (
    id          bigserial PRIMARY KEY,
    action      text NOT NULL,
    target      text NOT NULL,
    details     jsonb NOT NULL DEFAULT '{}',
    summary     text NOT NULL,
    status      text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'granted', 'used')),
    action_id   bigint REFERENCES pending_actions (id) ON DELETE SET NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,          -- до какого времени ждёт нажатия
    granted_at  timestamptz,
    valid_until timestamptz,                   -- до какого времени разрешение можно израсходовать
    used_at     timestamptz
);
CREATE INDEX ops_consents_open ON ops_consents (action, target) WHERE status <> 'used';
