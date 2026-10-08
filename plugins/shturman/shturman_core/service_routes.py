"""Какие маршруты сервиса переписки кому разрешены.

У внутреннего API сервиса один токен на всё, поэтому границы между ролями держит плагин:
каждый клиент создаётся со своим перечнем пар «метод + путь» и ничего другого отправить не может.

  BRIDGE — исполнитель заданий и обработчики Telegram в процессе шлюза;
  TOOLS  — инструменты агента: создать черновик, посмотреть и поправить обязательства и людей;
  UI     — страницы владельца в дашборде (проходят через plugin_api, уже за входом).

Агенту недоступно всё, что меняет правила отправки, список доверенных, автоответ, правила
наблюдателя и исключения чатов: это действия владельца в интерфейсе.

Вход в аккаунт Telegram, управление аккаунтами, выбор их чатов и импорт выгрузки не разрешены
НИ ОДНОЙ роли (перечень `SETUP_PAGE_ONLY` ниже). С версии 0.0.6 это делается только на странице
настройки переписки, которую сервис отдаёт сам, мимо Hermes: дашборд стоит на адресе, где
ассистент может исполнять свой код, и через его проход не должны идти ни ссылка для QR-кода
входа, ни облачный пароль Telegram, ни список всех диалогов владельца. У внутреннего API сервиса
эти маршруты остаются (ими пользуется оператор командой `shturman call`); плагин к ним не ходит.
Маршруты сверены с кодом сервиса (`service/src/shturman/**`) на коммите fb46275; маршруты
подтверждений и состояния своего исполнителя добавлены по коммиту 0d4a600.

Когда у сервиса свой бот согласований, часть действий владельца не применяется сразу: сервис
отвечает 202 с телом {"status": "pending_confirmation", "action_id", "summary", ...} и ждёт
нажатия в боте. Страницы владельца получают этот ответ как есть (см. `plugin_api.service_proxy`)
и могут посмотреть ждущие действия и отменить их (`/api/confirmations…`). Подтвердить действие
через плагин нельзя ни одной ролью: «да» принимает только бот сервиса.
"""

from __future__ import annotations

import re
from typing import Iterable

Routes = tuple[tuple[str, "re.Pattern[str]"], ...]

_INT = r"[0-9]{1,19}"
_HEX32 = r"[0-9a-f]{32}"
_TOKEN = r"[A-Za-z0-9_-]{1,64}"


def _compile(items: Iterable[tuple[str, str]]) -> Routes:
    return tuple((method, re.compile(pattern)) for method, pattern in items)


def allowed(routes: Routes, method: str, path: str) -> bool:
    """Разрешена ли пара. Путь сравнивается целиком: «..», двойные косые и хвосты не проходят."""
    if not isinstance(path, str) or not isinstance(method, str):
        return False
    method = method.upper()
    return any(m == method and pattern.fullmatch(path) for m, pattern in routes)


BRIDGE: Routes = _compile([
    ("GET", r"/api/status"),
    ("GET", r"/api/outbox/policy"),          # только чтение: включена ли отправка — для страницы состояния
    ("PUT", r"/api/owner"),
    ("DELETE", r"/api/owner"),
    ("POST", r"/api/jobs/claim"),
    ("POST", rf"/api/jobs/{_INT}/(complete|fail)"),
    ("POST", r"/api/callbacks/telegram"),
    ("POST", r"/api/ingest/business/(connection|message|deleted)"),
])

TOOLS: Routes = _compile([
    ("POST", r"/api/outbox/drafts"),
    ("GET", r"/api/commitments"),
    ("GET", rf"/api/commitments/{_INT}"),
    # accept и reject сюда не входят: предложение принимает или отклоняет владелец кнопкой.
    ("POST", rf"/api/commitments/{_INT}/(close|cancel|reopen|reschedule)"),
    ("GET", r"/api/people"),
    ("GET", rf"/api/people/{_INT}"),
    ("POST", rf"/api/people/{_INT}/aliases"),
])

UI: Routes = _compile([
    ("GET", r"/api/status"),
    ("GET", r"/api/embeddings/status"),
    # защита от внедрённых инструкций: только числа и состояние; выключателя в API нет
    ("GET", r"/api/guard/status"),
    # расшифровка голосовых: только числа и состояние, текстов нет
    ("GET", r"/api/voice/status"),
    ("GET", r"/api/media/status"),
    ("GET", r"/api/processing/status"),
    ("GET", r"/api/executor/status"),        # свой бот и своя модель сервиса: только признаки и счётчики
    # действия, которые ждут подтверждения владельца в боте: посмотреть и отменить (не подтвердить)
    ("GET", r"/api/confirmations"),
    ("GET", rf"/api/confirmations/{_INT}"),
    ("POST", rf"/api/confirmations/{_INT}/cancel"),
    # чаты и исключения
    ("GET", r"/api/chats"),
    ("PUT", rf"/api/chats/{_INT}/excluded"),
    # аккаунты Telegram: только перечень и состояние. Вход, пауза, выход, выбор чатов и импорт
    # выгрузки — на странице настройки переписки, не здесь (SETUP_PAGE_ONLY).
    ("GET", r"/api/tg/accounts"),
    # шлюз отправки
    ("GET", r"/api/outbox/drafts"),
    ("POST", rf"/api/outbox/drafts/{_INT}/cancel"),
    ("GET", r"/api/outbox/policy"),
    ("PUT", r"/api/outbox/policy"),
    ("PUT", rf"/api/outbox/chats/{_INT}"),
    ("GET", r"/api/outbox/autoreply"),
    ("PUT", r"/api/outbox/autoreply"),
    ("GET", r"/api/outbox/trusted"),
    ("POST", r"/api/outbox/trusted"),
    ("DELETE", r"/api/outbox/trusted"),
    # наблюдатель
    ("GET", r"/api/watch/rules"),
    ("POST", r"/api/watch/rules"),
    ("PUT", rf"/api/watch/rules/{_INT}"),
    ("DELETE", rf"/api/watch/rules/{_INT}"),
    ("GET", r"/api/watch/hits"),
    # обязательства
    ("GET", r"/api/commitments"),
    ("GET", rf"/api/commitments/{_INT}"),
    ("POST", rf"/api/commitments/{_INT}/(close|cancel|reopen|reschedule|accept|reject)"),
    # люди
    ("GET", r"/api/people"),
    ("GET", r"/api/people/proposals"),
    ("POST", rf"/api/people/proposals/{_INT}/reject"),
    ("POST", r"/api/people/merge"),
    ("GET", rf"/api/people/{_INT}"),
    ("POST", rf"/api/people/{_INT}/(aliases|split)"),
    ("DELETE", rf"/api/people/{_INT}/aliases"),
    # страницы памяти
    ("GET", r"/api/pages"),
    ("GET", r"/api/pages/(lint|search|proposals)"),
    ("POST", r"/api/pages/build"),
    ("POST", rf"/api/pages/proposals/{_INT}"),
    ("GET", rf"/api/pages/{_INT}"),
    ("PUT", rf"/api/pages/{_INT}/owner-block"),
])

# Маршруты внутреннего API, которые плагин не вызывает ни одной ролью: то же самое владелец делает
# на странице настройки переписки (`/shturman-setup/`), мимо Hermes. Перечень нужен тестам — чтобы
# эти маршруты не вернулись в проход дашборда незаметно — и сверке с кодом сервиса.
SETUP_PAGE_ONLY: Routes = _compile([
    # вход в аккаунт Telegram: в ответах — ссылка для QR-кода, в запросе — облачный пароль
    ("POST", r"/api/tg/login"),
    ("GET", rf"/api/tg/login/{_TOKEN}"),
    ("POST", rf"/api/tg/login/{_TOKEN}/(password|cancel)"),
    # управление аккаунтом и выбор его чатов
    ("POST", rf"/api/tg/accounts/{_INT}/(logout|pause|resume|sync)"),
    ("PUT", rf"/api/tg/accounts/{_INT}/options"),
    ("GET", rf"/api/tg/accounts/{_INT}/(dialogs|sync)"),
    # импорт выгрузки Telegram Desktop
    ("POST", r"/api/imports"),
    ("GET", r"/api/imports"),
    ("GET", rf"/api/imports/{_HEX32}"),
    ("DELETE", rf"/api/imports/{_HEX32}"),
    ("GET", rf"/api/imports/{_HEX32}/scan"),
    ("POST", rf"/api/imports/{_HEX32}/run"),
])
