-- Сообщения, у которых текст появился позже, чем сообщение пришло: расшифровка голосового,
-- разбор вложения. Разбор обязательств идёт по номерам сообщений (отметка в settings
-- 'processing.state'), и такое сообщение он мог уже пройти, пока текста не было. Пометка
-- late_content говорит следующему прогону: посмотри его ещё раз. Прогон снимает пометку с
-- того, что рассмотрел; скрытое защитой остаётся помеченным, пока его не откроют.

ALTER TABLE messages ADD COLUMN late_content boolean NOT NULL DEFAULT false;

CREATE INDEX messages_late_content ON messages (id) WHERE late_content;
