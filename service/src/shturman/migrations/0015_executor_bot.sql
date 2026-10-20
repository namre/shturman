-- Свой исполнитель сервиса: состояние бота согласований и одноразовые коды привязки владельца.
-- Отдельные таблицы, а не общая settings: ни один маршрут внутреннего API их не читает
-- и не меняет, поэтому владелец токена API не может ни подложить код привязки, ни сдвинуть
-- отметку «владелец привязан через бота».

-- Ключи:
--   bot    {id, username}        — каким ботом сервис работает (по ответу getMe);
--   offset {bot_id, next}        — с какого обновления Telegram продолжать после перезапуска;
--   owner  {user_id, bot_id}     — кто и в каком боте привязался по одноразовой ссылке.
CREATE TABLE executor_state (
    key         text PRIMARY KEY,
    value       jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Одноразовый код привязки владельца. Сам код не хранится — только его SHA-256.
-- Действующий код всегда один: новый стирает прежние.
CREATE TABLE executor_bind_codes (
    id          bigserial PRIMARY KEY,
    code_hash   text NOT NULL UNIQUE,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    used_at     timestamptz
);
