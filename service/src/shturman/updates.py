"""Уведомления об обновлениях «Штурмана» (Р-101; решение 10.10 дополнения 3 к Charter).

Зачем. Экземпляр сам не узнавал о новых версиях, а владелец (CEO, который сопровождает экземпляр с Codex)
не следит за репозиторием. Теперь сервис раз в сутки читает список выпусков `releases.json` из ветки main
репозитория и пишет владельцу в бот согласований; после обновления — итог: «обновлено, проверки прошли»
или что не прошло.

Что и откуда.

  * Список выпусков — `releases.json` в корне репозитория (схема — `docs/releases.md`, «Список выпусков»).
    Его пишет разработчик при выпуске; проверка формы — `ops/release_manifest.py` в CI. Сервис читает его по
    raw-ссылке на main (`SHTURMAN_UPDATES_URL`, только https) одним GET без идентификатора экземпляра, без
    куки и без версии в заголовках; видно только адрес сервера, как у любого обращения наружу. Через
    `EGRESS_PROXY_URL`, если он задан. Выключатель — `SHTURMAN_UPDATES=off`.
  * Список — ДАННЫЕ, не указания. Принимается только целиком прошедший проверку полей (`parse`): теги
    вида vX.Y.Z[-rc.N], даты, виды, короткие строки без управляющих знаков; лишние поля отбрасываются.
    Принимается только в пределах срока годности (`expires`; не дольше `MAX_LIFETIME` дней от публикации):
    подменённый когда-то и застывший список не продолжает «сообщать» вечно. Просроченный, недоступный или
    битый список — строка `updates` в `./ops/doctor.sh` и одно сообщение владельцу (не каждый день).
  * Текст сообщений собирает этот модуль по шаблону из проверенных полей — с ограничением длины. Ничего
    исполняемого в сообщениях нет: «Как обновить» присылает текст для Codex со ссылкой на промпт и runbook
    репозитория, а настоящая проверка цели — у `./ops/update.sh`: тег есть в истории, `VERSION` совпадает,
    только вперёд (`docs/releases.md`). Тег, которого нет, — Codex услышит «выпуска нет», и только.
  * Ассистент в Hermes список не читает: во внутреннем API (`GET /api/status`) — только теги и коды, без
    текстов; тексты видит владелец в боте и в приложении «Штурман».

Кому и когда (`tell_news`).

  * Одна новость — одно сообщение на версию (`update_notices`). Обычный выпуск — не чаще раза в неделю и не
    в тихие часы владельца (те же, что у уведомлений, `notify/settings.py`). Исправление безопасности
    (`kind: security`) — сразу (в тихие часы — без звука) и повтором раз в сутки, пока экземпляр не на нём.
  * Кнопки: «Как обновить» (текст для Codex), «Напомнить через неделю», «Пропустить версию» — две
    последние не у исправлений безопасности: их напоминание — ежедневное.
  * Канал — тот же, что у `./ops/update.sh`: `release` видит только выпуски, `pilot` — и кандидаты.
  * Новая проверенная версия Hermes (`hermes` в списке) — отдельное сообщение, только в режиме с Hermes и
    только если она новее запущенной. Версию Hermes поднимает владелец (`AGENTS.md`).
  * Предупреждение «модель X у провайдера снимается ДД.ММ» (`warnings`) — за 30 и за 7 дней, если сервис
    сам обращается к этой модели (свой доступ к модели: `SHTURMAN_LLM_MODEL*`, подписка ChatGPT).
    О модели ассистента в Hermes сервис не знает.

После обновления (`tell_results`, `tell_changed`). `./ops/update.sh` в конце передаёт итог командой
`shturman update-result` в контейнере сервиса — прямо в базу (`update_runs`): у внутреннего API, а значит
и у ассистента, такого маршрута нет. Сервис пишет владельцу «Обновлено до X, проверки прошли» или что не
прошло. Если при запуске сервис видит, что версия сменилась, а итога 30 минут нет (обновляли не через
`./ops/update.sh`, откат, восстановление), — «версия сменилась, итога проверки нет».
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, AsyncIterator, Iterable

import asyncpg
import httpx

from . import __version__, bridge
from .sanitize import clean_line

logger = logging.getLogger("shturman.updates")

DEFAULT_URL = "https://raw.githubusercontent.com/namre/shturman/main/releases.json"
SCHEMA = 1
CHECK_EVERY = timedelta(hours=24)
CHECK_JITTER = 1800            # секунд: следующая проверка — через сутки и до получаса, а не ровно
BOOT_DELAY = timedelta(minutes=5)
TICK = 60.0
TIMEOUT = 20.0
MAX_BYTES = 64 * 1024
MAX_LIFETIME = 120             # дней от публикации до конца срока годности — не больше
WEEK = timedelta(days=7)
DAY = timedelta(days=1)
RESULT_WAIT = timedelta(minutes=30)   # смена версии ждёт итога ./ops/update.sh
UNREACHABLE_ALERT = 3          # столько неудачных проверок подряд (раз в сутки) — сообщение владельцу
TEXT_LIMIT = 200
MODULE = "up"                  # кнопки: sh:up:<how|later|skip>:<номер новости>
STAGES = (30, 7)               # за сколько дней предупреждать о снятии модели

_TAG = re.compile(r"^v(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?:-rc\.(\d{1,3}))?$")
_VERSION = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")
_HERMES_TAG = re.compile(r"^v(\d{4})\.(\d{1,2})\.(\d{1,2})$")
_HERMES_VERSION = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")
_WARN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,40}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,80}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RUN = re.compile(r"^\d{8}T\d{6}Z$")
_ROW = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
KINDS = ("security", "regular")
RESULTS = ("ok", "rolled-back", "failed-needs-human", "rollback-failed")
HERMES_IMAGE = "nousresearch/hermes-agent:"


class ManifestError(ValueError):
    """Список выпусков не принят. code — короткий код для doctor и сообщения: expired, invalid,
    unreachable, http, too_big, bad_url."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code, self.detail = code, detail


def tag_key(tag: str) -> tuple[int, int, int, int, int] | None:
    """Порядок выпусков: кандидат vX.Y.Z-rc.N идёт перед выпуском vX.Y.Z (как в ./ops/update.sh)."""
    m = _TAG.match(tag or "")
    if not m:
        return None
    x, y, z, rc = m.groups()
    return (int(x), int(y), int(z), 0 if rc is not None else 1, int(rc or 0))


def hermes_key(tag: str) -> tuple[int, int, int] | None:
    m = _HERMES_TAG.match(tag or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


# --- разбор списка выпусков -------------------------------------------------------------------------

@dataclass(frozen=True)
class Release:
    tag: str
    version: str
    date: date
    kind: str                       # security | regular
    changes: tuple[str, ...]        # что изменилось, 1–3 строки простыми словами
    downtime_minutes: int | None    # перерыв в работе, минут; None — не сказано
    owner_action: bool              # нужны ли действия владельца сверх нажатий в боте
    owner_steps: tuple[str, ...]
    hermes: tuple[str, ...]         # с какими образами Hermes проверен

    @property
    def rc(self) -> bool:
        return "-rc." in self.tag

    @property
    def key(self) -> tuple[int, ...]:
        key = tag_key(self.tag)
        assert key is not None
        return key


@dataclass(frozen=True)
class HermesRelease:
    tag: str
    version: str
    verified: date


@dataclass(frozen=True)
class ModelWarning:
    id: str
    model: str
    provider: str
    until: date
    text: str


@dataclass(frozen=True)
class Manifest:
    published: date
    expires: date
    releases: tuple[Release, ...]
    hermes: tuple[HermesRelease, ...] = ()
    warnings: tuple[ModelWarning, ...] = ()

    def to_json(self) -> dict[str, Any]:
        """Принятый список в той же схеме — только проверенные поля (так он и хранится в базе)."""
        return {
            "schema": SCHEMA, "published": self.published.isoformat(), "expires": self.expires.isoformat(),
            "releases": [{"tag": r.tag, "version": r.version, "date": r.date.isoformat(), "kind": r.kind,
                          "changes": list(r.changes), "downtime_minutes": r.downtime_minutes,
                          "owner_action": r.owner_action, "owner_steps": list(r.owner_steps),
                          "hermes": list(r.hermes)} for r in self.releases],
            "hermes": [{"tag": h.tag, "version": h.version, "verified": h.verified.isoformat()} for h in self.hermes],
            "warnings": [{"id": w.id, "model": w.model, "provider": w.provider, "until": w.until.isoformat(),
                          "text": w.text} for w in self.warnings],
        }

    def release(self, tag: str) -> Release | None:
        return next((r for r in self.releases if r.tag == tag), None)


def _bad(detail: str) -> ManifestError:
    return ManifestError("invalid", detail)


def _date(value: Any, name: str) -> date:
    if not isinstance(value, str) or not _DATE.match(value):
        raise _bad(f"{name}: нужна дата ГГГГ-ММ-ДД")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise _bad(f"{name}: такой даты нет") from None


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise _bad(f"{name}: нужна строка")
    out = clean_line(value, TEXT_LIMIT)
    if not out:
        raise _bad(f"{name}: пустая строка")
    return out


def _texts(value: Any, name: str, *, most: int, least: int = 0) -> tuple[str, ...]:
    if value is None and least == 0:
        return ()
    if not isinstance(value, list) or not least <= len(value) <= most:
        raise _bad(f"{name}: нужен список из {least}–{most} строк")
    return tuple(_text(v, f"{name}[{i}]") for i, v in enumerate(value))


def _list(data: dict, name: str, most: int) -> list:
    value = data.get(name, [])
    if not isinstance(value, list) or len(value) > most:
        raise _bad(f"{name}: нужен список, не длиннее {most}")
    return value


def _release(item: Any, i: int) -> Release:
    name = f"releases[{i}]"
    if not isinstance(item, dict):
        raise _bad(f"{name}: нужен объект")
    tag = item.get("tag")
    if not isinstance(tag, str) or tag_key(tag) is None:
        raise _bad(f"{name}.tag: нужен тег вида v0.0.9 или v0.0.9-rc.2")
    version = item.get("version")
    if not isinstance(version, str) or not _VERSION.match(version) or tag[1:].split("-rc.")[0] != version:
        raise _bad(f"{name}.version: должна совпадать с номером тега")
    kind = item.get("kind")
    if kind not in KINDS:
        raise _bad(f"{name}.kind: security или regular")
    downtime = item.get("downtime_minutes")
    if downtime is not None and (isinstance(downtime, bool) or not isinstance(downtime, int) or not 0 <= downtime <= 240):
        raise _bad(f"{name}.downtime_minutes: целое число минут от 0 до 240 или null")
    owner_action = item.get("owner_action")
    if not isinstance(owner_action, bool):
        raise _bad(f"{name}.owner_action: true или false")
    steps = _texts(item.get("owner_steps"), f"{name}.owner_steps", most=3, least=1 if owner_action else 0)
    hermes = item.get("hermes", [])
    if not isinstance(hermes, list) or len(hermes) > 5 or not all(isinstance(h, str) and hermes_key(h) for h in hermes):
        raise _bad(f"{name}.hermes: список тегов образа Hermes вида v2026.9.24")
    return Release(tag=tag, version=version, date=_date(item.get("date"), f"{name}.date"), kind=kind,
                   changes=_texts(item.get("changes"), f"{name}.changes", most=3, least=1),
                   downtime_minutes=downtime, owner_action=owner_action, owner_steps=steps if owner_action else (),
                   hermes=tuple(hermes))


def _hermes(item: Any, i: int) -> HermesRelease:
    name = f"hermes[{i}]"
    if not isinstance(item, dict):
        raise _bad(f"{name}: нужен объект")
    tag, version = item.get("tag"), item.get("version")
    if not isinstance(tag, str) or hermes_key(tag) is None:
        raise _bad(f"{name}.tag: тег образа Hermes вида v2026.9.24")
    if not isinstance(version, str) or not _HERMES_VERSION.match(version):
        raise _bad(f"{name}.version: номер версии Hermes вида 0.21.5")
    return HermesRelease(tag=tag, version=version, verified=_date(item.get("verified"), f"{name}.verified"))


def _warning(item: Any, i: int) -> ModelWarning:
    name = f"warnings[{i}]"
    if not isinstance(item, dict):
        raise _bad(f"{name}: нужен объект")
    wid, model = item.get("id"), item.get("model")
    if not isinstance(wid, str) or not _WARN_ID.match(wid):
        raise _bad(f"{name}.id: латиница в нижнем регистре, цифры, точка, дефис")
    if not isinstance(model, str) or not _MODEL.match(model):
        raise _bad(f"{name}.model: имя модели, как его пишут в настройках")
    return ModelWarning(id=wid, model=model, provider=_text(item.get("provider"), f"{name}.provider")[:40],
                        until=_date(item.get("until"), f"{name}.until"), text=_text(item.get("text"), f"{name}.text"))


def parse(data: Any, *, today: date) -> Manifest:
    """Проверяет список выпусков целиком. Бросает ManifestError: invalid — не та форма, expired — срок вышел."""
    if not isinstance(data, dict):
        raise _bad("нужен объект JSON")
    if data.get("schema") != SCHEMA:
        raise _bad(f"schema: ожидается {SCHEMA}")
    published, expires = _date(data.get("published"), "published"), _date(data.get("expires"), "expires")
    if expires < published or (expires - published).days > MAX_LIFETIME:
        raise _bad(f"expires: от published до expires — от 0 до {MAX_LIFETIME} дней")
    if published > today + timedelta(days=2):
        raise _bad("published: дата из будущего")
    releases = tuple(_release(item, i) for i, item in enumerate(_list(data, "releases", 200)))
    if not releases:
        raise _bad("releases: пустой список")
    if len({r.tag for r in releases}) != len(releases):
        raise _bad("releases: тег повторяется")
    hermes = tuple(_hermes(item, i) for i, item in enumerate(_list(data, "hermes", 30)))
    if len({h.tag for h in hermes}) != len(hermes):
        raise _bad("hermes: тег повторяется")
    warnings = tuple(_warning(item, i) for i, item in enumerate(_list(data, "warnings", 30)))
    if len({w.id for w in warnings}) != len(warnings):
        raise _bad("warnings: id повторяется")
    if expires < today:
        raise ManifestError("expired", f"срок годности вышел {expires.isoformat()}")
    return Manifest(published=published, expires=expires, releases=releases, hermes=hermes, warnings=warnings)


def parse_bytes(raw: bytes, *, today: date) -> Manifest:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _bad("не JSON") from None
    return parse(data, today=today)


# --- что запущено -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Running:
    version: str
    tag: str = ""          # тег выпуска на запущенном коммите (пусто — коммит вне выпуска или неизвестно)
    commit: str = ""
    hermes: str = ""       # тег образа Hermes (пусто — режим без Hermes или неизвестно)

    @property
    def key(self) -> tuple[int, ...]:
        if self.tag:
            key = tag_key(self.tag)
            if key is not None:
                return key
        m = _VERSION.match(self.version)
        # Коммит вне выпуска с VERSION X.Y.Z — не раньше выпуска vX.Y.Z (VERSION поднимается выпуском).
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)), 1, 0) if m else (0, 0, 0, 0, 0)

    @property
    def name(self) -> str:
        if self.tag:
            return self.tag
        return self.version + (f" (коммит {self.commit[:7]})" if self.commit else "")

    def as_json(self) -> dict[str, str]:
        return {"version": self.version, "tag": self.tag, "commit": self.commit, "hermes": self.hermes}

    def same_code(self, other: dict[str, Any]) -> bool:
        return (other.get("version"), other.get("tag"), other.get("commit")) == (self.version, self.tag, self.commit)


def running(config: Any) -> Running:
    """Из окружения, которое пишет ./ops/up.sh; чего нет или что записано не так — не берётся."""
    version = config.code_version if _VERSION.match(config.code_version or "") else __version__
    tag = config.code_tag if tag_key(config.code_tag or "") and config.code_tag[1:].split("-rc.")[0] == version else ""
    commit = config.code_commit if _COMMIT.match(config.code_commit or "") else ""
    image = config.hermes_image or ""
    hermes = image[len(HERMES_IMAGE):] if image.startswith(HERMES_IMAGE) else ""
    return Running(version=version, tag=tag, commit=commit, hermes=hermes if hermes_key(hermes) else "")


def ahead(manifest: Manifest, run: Running, channel: str) -> list[Release]:
    """Выпуски впереди запущенного кода в канале, по возрастанию. В канале pilot выпуск vX.Y.Z не
    считается новостью для кандидата той же версии: по порядку выпуска (docs/releases.md) это тот же
    коммит, и ./ops/update.sh скажет «обновлений нет»."""
    mine = run.key
    pilot_rc = channel == "pilot" and mine[3] == 0
    out = []
    for r in manifest.releases:
        if r.rc and channel != "pilot":
            continue
        if r.key <= mine:
            continue
        if pilot_rc and not r.rc and r.key[:3] == mine[:3]:
            continue
        out.append(r)
    return sorted(out, key=lambda r: r.key)


def newer_hermes(manifest: Manifest, run: Running) -> HermesRelease | None:
    mine = hermes_key(run.hermes)
    if mine is None:
        return None
    newer = [h for h in manifest.hermes if (hermes_key(h.tag) or (0,)) > mine]
    return max(newer, key=lambda h: hermes_key(h.tag) or (0,)) if newer else None


def models_in_use(config: Any, env: Any = None) -> set[str]:
    """Модели, к которым сервис обращается сам (свой доступ к модели). Имена — в нижнем регистре."""
    import os

    from .executor.llm import task_models

    if not getattr(config, "own_llm", False):
        return set()
    main = config.chatgpt_model if config.chatgpt else config.llm_model
    names = {main} | set(task_models(os.environ if env is None else env).values())
    return {n.strip().lower() for n in names if n and n.strip()}


# --- состояние в базе -------------------------------------------------------------------------------

async def _get(conn: asyncpg.Connection, key: str) -> dict[str, Any]:
    raw = await conn.fetchval("SELECT value FROM update_state WHERE key = $1", key)
    if raw is None:
        return {}
    value = json.loads(raw) if isinstance(raw, str) else raw
    return value if isinstance(value, dict) else {}


async def _put(conn: asyncpg.Connection, key: str, value: dict[str, Any]) -> None:
    await conn.execute(
        """INSERT INTO update_state (key, value, updated_at) VALUES ($1, $2::jsonb, now())
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        key, json.dumps(value, ensure_ascii=False))


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        out = datetime.fromisoformat(value)
    except ValueError:
        return None
    return out if out.tzinfo else out.replace(tzinfo=timezone.utc)


async def current(conn: asyncpg.Connection, *, today: date) -> tuple[Manifest | None, str | None]:
    """Принятый список выпусков на сегодня и код неполадки. Список, принятый раньше, у которого с тех
    пор вышел срок годности, — уже не список: просроченный манифест уведомлений не даёт."""
    stored = await _get(conn, "manifest")
    check = await _get(conn, "check")
    problem = check.get("problem") if isinstance(check.get("problem"), str) else None
    if not stored.get("data"):
        return None, problem
    try:
        return parse(stored["data"], today=today), problem
    except ManifestError as exc:
        # Принятый раньше список устарел, потому что свежий не скачивается, — причина в связи, а не в списке.
        return None, problem if problem in ("unreachable", "http", "bad_url") else exc.code


async def ensure_notice(conn: asyncpg.Connection, kind: str, subject: str, *,
                        due_at: datetime | None = None) -> asyncpg.Record:
    await conn.execute(
        "INSERT INTO update_notices (kind, subject, due_at) VALUES ($1, $2, $3) ON CONFLICT (kind, subject) DO NOTHING",
        kind, subject, due_at)
    row = await conn.fetchrow("SELECT * FROM update_notices WHERE kind = $1 AND subject = $2", kind, subject)
    assert row is not None
    return row


async def _sent(conn: asyncpg.Connection, notice_id: int, now: datetime) -> None:
    await conn.execute(
        """UPDATE update_notices SET last_sent_at = $2, first_sent_at = COALESCE(first_sent_at, $2),
                  sent_count = sent_count + 1, snoozed_until = NULL WHERE id = $1""", notice_id, now)


# --- проверка списка выпусков -----------------------------------------------------------------------

def manifest_url(config: Any) -> str | None:
    """Адрес списка: только https, без имени пользователя. Иначе None (проверка не идёт: bad_url)."""
    from urllib.parse import urlsplit

    raw = (config.updates_url or DEFAULT_URL).strip()
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
        return None
    return raw


def make_client(config: Any, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    if transport is None:
        transport = httpx.AsyncHTTPTransport(proxy=getattr(config, "proxy_url", "") or None, trust_env=False,
                                             retries=1)
    # Ни версии, ни имени экземпляра в заголовках: один и тот же запрос с любого сервера.
    return httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                             timeout=httpx.Timeout(TIMEOUT),
                             headers={"User-Agent": "shturman-updates", "Accept": "application/json"})


async def fetch(client: httpx.AsyncClient, url: str) -> bytes:
    try:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise ManifestError("http", f"HTTP {response.status_code}")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    raise ManifestError("too_big", f"больше {MAX_BYTES} байт")
            return bytes(body)
    except httpx.HTTPError as exc:
        raise ManifestError("unreachable", type(exc).__name__) from None


async def check_now(state: Any, client: httpx.AsyncClient | None, *, now: datetime) -> dict[str, Any]:
    """Одна проверка: скачать, разобрать, записать принятый список или неполадку. Возвращает итог проверки."""
    async with state.pool.acquire() as conn:
        before = await _get(conn, "check")
    out: dict[str, Any] = {"at": now.isoformat(), "ok_at": before.get("ok_at"), "problem": None,
                           "detail": None, "fails": 0,
                           "next_at": (now + CHECK_EVERY + timedelta(seconds=random.randint(0, CHECK_JITTER))).isoformat()}
    manifest: Manifest | None = None
    url = manifest_url(state.config)
    try:
        if url is None:
            raise ManifestError("bad_url", "адрес списка выпусков не https")
        if client is None:
            raise ManifestError("unreachable", "клиент не создан")
        manifest = parse_bytes(await fetch(client, url), today=now.date())
    except ManifestError as exc:
        out.update(problem=exc.code, detail=clean_line(exc.detail, 120) or None,
                   fails=int(before.get("fails") or 0) + 1)
        logger.warning("список выпусков не принят: %s (%s)", exc.code, clean_line(exc.detail, 120))
    async with state.pool.acquire() as conn:
        async with conn.transaction():
            if manifest is not None:
                out["ok_at"] = now.isoformat()
                await _put(conn, "manifest", {"data": manifest.to_json(), "fetched_at": now.isoformat()})
                # Неполадки больше нет: следующая такая же — снова сообщение владельцу.
                await conn.execute("DELETE FROM update_notices WHERE kind = 'problem'")
            await _put(conn, "check", out)
    return out


# --- что запущено: запомнить и заметить смену -------------------------------------------------------

async def remember_running(conn: asyncpg.Connection, run: Running, *, now: datetime) -> None:
    """При запуске сервиса: запущенный код сравнивается с прежним. Сменился — новость `changed`, которая
    ждёт итога ./ops/update.sh RESULT_WAIT; пришёл итог — она снимается (`tell_results`)."""
    before = await _get(conn, "running")
    if before and run.same_code(before):
        if before.get("hermes") != run.hermes:
            await _put(conn, "running", {**run.as_json(), "since": before.get("since") or now.isoformat()})
        return
    if before:
        old = Running(version=str(before.get("version") or "?"), tag=str(before.get("tag") or ""),
                      commit=str(before.get("commit") or ""))
        await ensure_notice(conn, "changed", f"{old.name} → {run.name}|{now.isoformat()}", due_at=now + RESULT_WAIT)
    elif await _updated_into_this(conn, now):
        # Первый запуск с этим модулем на экземпляре, где уже была база: обновились с версии без него.
        await ensure_notice(conn, "changed", f"→ {run.name}|{now.isoformat()}", due_at=now + RESULT_WAIT)
    await _put(conn, "running", {**run.as_json(), "since": now.isoformat()})


async def _updated_into_this(conn: asyncpg.Connection, now: datetime) -> bool:
    row = await conn.fetchrow(
        """SELECT (SELECT applied_at FROM schema_migrations WHERE name = '0042_updates_reports.sql') AS mine,
                  (SELECT min(applied_at) FROM schema_migrations) AS first""")
    if row is None or row["mine"] is None or row["first"] is None:
        return False
    return row["mine"] - row["first"] > timedelta(hours=1) and now - row["mine"] < timedelta(hours=6)


# --- тексты -----------------------------------------------------------------------------------------

def _days(n: int) -> str:
    return f"{n} {'день' if n % 10 == 1 and n % 100 != 11 else 'дня' if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else 'дней'}"


def release_text(rels: list[Release], run: Running, *, security: bool) -> str:
    newest = rels[-1]
    head = "Вышло исправление безопасности «Штурмана»" if security else "Вышла новая версия «Штурмана»"
    lines = [f"{head}: {newest.tag}" + (" — кандидат для пилота" if newest.rc else "") + ".",
             f"У вас сейчас: {run.name}."]
    if len(rels) > 1:
        lines.append(f"Выпусков впереди: {len(rels)} — обновление пройдёт по очереди, с проверкой после каждого.")
    lines.append("")
    lines.append("Что изменилось:")
    lines += [f"• {c}" for c in newest.changes]
    for r in rels[:-1]:
        if r.kind == "security":
            lines.append(f"• В {r.tag} — исправление безопасности: {r.changes[0]}")
    minutes = [r.downtime_minutes for r in rels if r.downtime_minutes is not None]
    lines.append("")
    if minutes:
        total = sum(minutes)
        lines.append("Перерыв в работе: без перерыва." if total == 0 else f"Перерыв в работе: около {total} мин.")
    steps = [s for r in rels for s in r.owner_steps]
    if steps:
        lines.append("Понадобятся ваши действия: " + "; ".join(steps[:3]) + ".")
    else:
        lines.append("Ваших действий не нужно — кроме разрешений кнопкой в этом боте, если обновление их попросит.")
    lines.append("Обновляет Codex по инструкции из репозитория — нажмите «Как обновить», там готовый текст для него.")
    if security:
        lines.append("Напомню завтра, если эта версия ещё не будет установлена.")
    return "\n".join(lines)


def codex_text(tag: str) -> str:
    return ("Текст для Codex — откройте новую сессию с промптом prompts/operate-codex.md и вставьте в конце:\n\n"
            f"Обнови мой «Штурман» до версии {tag}: задание «Обновить экземпляр» в prompts/operate-codex.md, "
            "runbook docs/runbooks/update.md. Если до неё несколько выпусков — по очереди, с проверкой после "
            "каждого (./ops/update.sh --all). Разрешения я даю кнопкой в боте согласований — в терминал меня не "
            "отправляй. В конце сессии — ./ops/report.sh.")


def hermes_text(h: HermesRelease, run: Running) -> str:
    return (f"Вышла версия Hermes {h.version} ({h.tag}) — с вашим «Штурманом» она проверена "
            f"{h.verified.strftime('%d.%m.%Y')}. У вас сейчас Hermes {run.hermes}.\n\n"
            "Обновлять ли Hermes — решаете вы: ассистент перезапустится, а вернуть прежнюю версию можно только "
            "из резервной копии. Если решили — нажмите «Как обновить».")


def hermes_codex_text(tag: str) -> str:
    return ("Текст для Codex — откройте новую сессию с промптом prompts/operate-codex.md и вставьте в конце:\n\n"
            f"Обнови Hermes на моём «Штурмане» до {tag}: docs/runbooks/update.md, раздел «Новая версия Hermes», "
            "сначала резервная копия. Версия проверена — она есть в config/hermes-versions.txt. Строку "
            "HERMES_VERSION в .env записываю я — скажи, как. В конце сессии — ./ops/report.sh.")


def model_text(w: ModelWarning, days_left: int) -> str:
    when = "сегодня" if days_left <= 0 else f"через {_days(days_left)}"
    return (f"Провайдер {w.provider} снимает модель {w.model} {w.until.strftime('%d.%m.%Y')} — {when}. "
            f"«Штурман» сейчас обращается к ней сам (разбор переписки, договорённости).\n\n{w.text}\n\n"
            "Сменить модель — в «Настройках Штурмана», раздел «Дополнительно», или попросите об этом того, "
            "кто сопровождает сервер.")


PROBLEM_TEXT = {
    "expired": ("Не могу проверить обновления «Штурмана»: список выпусков на GitHub устарел — его срок годности "
                "вышел. Это забота разработчика; пока он не обновит список, сообщений о новых версиях не будет. "
                "Проверить вручную может Codex: ./ops/update.sh --check."),
    "invalid": ("Не могу проверить обновления «Штурмана»: список выпусков на GitHub не прочитался — он повреждён. "
                "Это забота разработчика; пока список не исправят, сообщений о новых версиях не будет. Проверить "
                "вручную может Codex: ./ops/update.sh --check."),
    "unreachable": ("Не могу проверить обновления «Штурмана»: GitHub не отвечает с сервера уже несколько дней "
                    "подряд. Сам «Штурман» от этого не ломается, но о новых версиях я не узнаю. Если так и "
                    "останется — попросите Codex посмотреть строку updates в ./ops/doctor.sh."),
    "bad_url": ("Проверка обновлений «Штурмана» выключена: адрес списка выпусков в настройках сервера записан "
                "неверно (нужен https). Попросите Codex посмотреть строку updates в ./ops/doctor.sh."),
}
PROBLEM_TEXT["http"] = PROBLEM_TEXT["unreachable"]
PROBLEM_TEXT["too_big"] = PROBLEM_TEXT["invalid"]


def _fails(names: Iterable[str]) -> str:
    names = [n for n in names if _ROW.match(n or "")]
    return "строки проверки: " + ", ".join(names[:10]) if names else "проверка не дала итога"


def result_text(row: Any, manifest: Manifest | None) -> str:
    to, frm = row["to_name"], row["from_name"]
    if row["result"] == "ok":
        lines = [f"«Штурман» обновлён: {frm} → {to}. Проверки после обновления прошли."]
        rel = manifest.release(to) if manifest else None
        if rel is not None:
            lines += ["", "Что изменилось:"] + [f"• {c}" for c in rel.changes]
        return "\n".join(lines)
    if row["result"] == "rolled-back":
        return (f"Обновление «Штурмана» до {to} не встало: проверка после него не прошла ({_fails(row['new_fails'])}). "
                f"Всё вернули как было — работает {frm}, данные на месте. Тот, кто обновлял, запишет дефект; "
                "повторять обновление не нужно, пока его не исправят.")
    if row["result"] == "failed-needs-human":
        return (f"Обновление «Штурмана» до {to} прошло с ошибками ({_fails(row['new_fails'])}), а разрешения на "
                f"автоматический откат не было: сейчас работает {to} и может работать неверно. Попросите Codex "
                "«откатить последнее обновление» — запрос на откат придёт вам сюда кнопкой.")
    return (f"Обновление «Штурмана» до {to} не встало, и автоматический откат тоже не удался "
            f"({_fails(row['new_fails'])}). Нужен тот, кто сопровождает сервер: копия до обновления сохранена, "
            "порядок — docs/runbooks/restore.md.")


def changed_text(subject: str) -> str:
    names = subject.split("|", 1)[0]
    old, _, new = names.partition("→ ")
    old = old.strip()
    if not old:
        return (f"«Штурман» обновлён до {new}. С этой версии он сам сообщает о новых версиях и об итоге каждого "
                "обновления. Итога проверки этого обновления у меня нет — попросите Codex показать ./ops/doctor.sh.")
    older = (tag_key(new) or (0,)) < (tag_key(old) or (0,)) if tag_key(new) and tag_key(old) else False
    if older:
        return (f"«Штурман» вернулся на версию {new} (была {old}) — откат или восстановление из копии. "
                "Итога проверки у меня нет — попросите Codex показать ./ops/doctor.sh.")
    return (f"Версия «Штурмана» сменилась: {old} → {new}. Обновляли не через ./ops/update.sh (или его итог до "
            "меня не дошёл), поэтому итога проверки у меня нет — попросите Codex показать ./ops/doctor.sh.")


# --- кому и когда -----------------------------------------------------------------------------------

async def _quiet(conn: asyncpg.Connection, config: Any, now: datetime) -> bool:
    from .notify import core as notify_core, settings as notify_settings

    s = await notify_settings.load(conn)
    return notify_core.in_quiet(s, now.astimezone(notify_core.zone(config.timezone)))


def _buttons(notice_id: int, *, how: bool = True, later: bool = False, skip: bool = False) -> list[list[dict[str, str]]]:
    row = []
    if how:
        row.append(bridge.button("Как обновить", MODULE, f"how:{notice_id}"))
    second = []
    if later:
        second.append(bridge.button("Напомнить через неделю", MODULE, f"later:{notice_id}"))
    if skip:
        second.append(bridge.button("Пропустить версию", MODULE, f"skip:{notice_id}"))
    return [r for r in (row, second) if r]


async def tell_results(conn: asyncpg.Connection, state: Any, now: datetime) -> int:
    rows = await conn.fetch("SELECT * FROM update_runs WHERE told_at IS NULL ORDER BY id")
    if not rows or await bridge.get_owner(conn) is None:
        return 0
    manifest, _ = await current(conn, today=now.date())
    for row in rows:
        async with conn.transaction():
            await bridge.notify_owner(conn, result_text(row, manifest))
            await conn.execute("UPDATE update_runs SET told_at = $2 WHERE id = $1", row["id"], now)
            # Итог объясняет смену версии, замеченную при перезапуске сервиса (и вперёд, и откат).
            await conn.execute(
                """UPDATE update_notices SET done = true
                   WHERE kind = 'changed' AND NOT done AND created_at > $1""", row["reported_at"] - timedelta(hours=6))
    return len(rows)


async def tell_changed(conn: asyncpg.Connection, state: Any, now: datetime) -> int:
    rows = await conn.fetch(
        """SELECT * FROM update_notices WHERE kind = 'changed' AND NOT done AND sent_count = 0
             AND due_at <= $1 ORDER BY id""", now)
    if not rows or await bridge.get_owner(conn) is None:
        return 0
    for row in rows:
        async with conn.transaction():
            await bridge.notify_owner(conn, changed_text(row["subject"]))
            await _sent(conn, row["id"], now)
            await conn.execute("UPDATE update_notices SET done = true WHERE id = $1", row["id"])
    return len(rows)


async def tell_news(conn: asyncpg.Connection, state: Any, now: datetime) -> list[str]:
    """Новости из списка выпусков: выпуски, Hermes, снятие моделей, неполадка списка. Возвращает виды
    отправленного (для тестов)."""
    config = state.config
    sent: list[str] = []
    if await bridge.get_owner(conn) is None:
        return sent
    quiet = await _quiet(conn, config, now)
    manifest, problem = await current(conn, today=now.date())
    check = await _get(conn, "check")
    await _tell_problem(conn, problem, check, quiet=quiet, now=now, sent=sent)
    if manifest is None:
        return sent
    run = running(config)
    rels = ahead(manifest, run, config.update_channel)
    # Новости о выпусках, которые уже не впереди, закрыты.
    for row in await conn.fetch("SELECT id, subject FROM update_notices WHERE kind = 'release' AND NOT done"):
        key = tag_key(row["subject"])
        if key is None or key <= run.key:
            await conn.execute("UPDATE update_notices SET done = true WHERE id = $1", row["id"])
    if rels:
        await _tell_release(conn, rels, run, quiet=quiet, now=now, sent=sent)
    h = newer_hermes(manifest, run)
    if h is not None and not quiet:
        row = await ensure_notice(conn, "hermes", h.tag)
        if row["sent_count"] == 0 and not row["done"]:
            async with conn.transaction():
                await bridge.notify_owner(conn, hermes_text(h, run), buttons=_buttons(row["id"]))
                await _sent(conn, row["id"], now)
            sent.append("hermes")
    using = models_in_use(config)
    for w in manifest.warnings:
        if w.model.lower() not in using or quiet:
            continue
        left = (w.until - now.astimezone(_zone(config)).date()).days
        stage = next((s for s in sorted(STAGES) if 0 <= left <= s), None)
        if stage is None:
            continue
        row = await ensure_notice(conn, "model", f"{w.id}:{stage}")
        if row["sent_count"] == 0:
            async with conn.transaction():
                await bridge.notify_owner(conn, model_text(w, left))
                await _sent(conn, row["id"], now)
                # Предупреждение за 7 дней заменяет то, что за 30 ещё не ушло.
                for s in STAGES:
                    if s > stage:
                        n = await ensure_notice(conn, "model", f"{w.id}:{s}")
                        await conn.execute("UPDATE update_notices SET done = true WHERE id = $1", n["id"])
            sent.append("model")
    return sent


def _zone(config: Any):
    from .notify import core as notify_core

    return notify_core.zone(config.timezone)


async def _tell_problem(conn: asyncpg.Connection, problem: str | None, check: dict[str, Any], *, quiet: bool,
                        now: datetime, sent: list[str]) -> None:
    if problem is None or quiet or problem not in PROBLEM_TEXT:
        return
    if problem in ("unreachable", "http") and int(check.get("fails") or 0) < UNREACHABLE_ALERT:
        return
    code = "unreachable" if problem == "http" else "invalid" if problem == "too_big" else problem
    row = await ensure_notice(conn, "problem", code)
    if row["sent_count"]:
        return
    async with conn.transaction():
        await bridge.notify_owner(conn, PROBLEM_TEXT[code])
        await _sent(conn, row["id"], now)
    sent.append("problem")


async def _tell_release(conn: asyncpg.Connection, rels: list[Release], run: Running, *, quiet: bool,
                        now: datetime, sent: list[str]) -> None:
    newest = rels[-1]
    security = any(r.kind == "security" for r in rels)
    row = await ensure_notice(conn, "release", newest.tag)
    if row["done"]:
        return
    last = row["last_sent_at"]
    if security:
        # Сразу и раз в сутки, пока не обновлено; в тихие часы — без звука. Пропустить нельзя.
        if last is not None and now - last < DAY:
            return
        async with conn.transaction():
            await bridge.notify_owner(conn, release_text(rels, run, security=True), buttons=_buttons(row["id"]),
                                      silent=quiet)
            await _sent(conn, row["id"], now)
        sent.append("security")
        return
    if row["skipped"] or quiet:
        return
    snoozed = row["snoozed_until"]
    if snoozed is not None and now < snoozed:
        return
    if last is not None and snoozed is None:
        return                       # об этой версии уже сказано
    notified = await _get(conn, "notified")
    regular_at = _when(notified.get("regular_at"))
    if regular_at is not None and now - regular_at < WEEK:
        return                       # обычные новости — не чаще раза в неделю
    async with conn.transaction():
        await bridge.notify_owner(conn, release_text(rels, run, security=False),
                                  buttons=_buttons(row["id"], later=True, skip=True))
        await _sent(conn, row["id"], now)
        await _put(conn, "notified", {**notified, "regular_at": now.isoformat()})
    sent.append("release")


# --- кнопки ---------------------------------------------------------------------------------------

GONE = {"answer": "Это сообщение уже неактуально.", "edit_text": None, "remove_buttons": True}


@bridge.on_callback(MODULE)
async def _pressed(conn: asyncpg.Connection, rest: str, user_id: int) -> dict[str, Any]:
    act, _, raw = rest.partition(":")
    if not raw.isdigit():
        return GONE
    row = await conn.fetchrow("SELECT * FROM update_notices WHERE id = $1 FOR UPDATE", int(raw))
    if row is None or row["kind"] not in ("release", "hermes"):
        return GONE
    if act == "how":
        text = codex_text(row["subject"]) if row["kind"] == "release" else hermes_codex_text(row["subject"])
        # Отдельным сообщением — его удобно скопировать или переслать; работает и через плагин Hermes.
        await bridge.notify_owner(conn, text)
        return {"answer": "Текст для Codex — следующим сообщением.", "edit_text": None, "remove_buttons": False}
    if row["kind"] != "release" or row["done"]:
        return GONE
    if act == "later":
        await conn.execute("UPDATE update_notices SET snoozed_until = now() + interval '7 days' WHERE id = $1", row["id"])
        return {"answer": "Напомню через неделю.", "edit_text": None, "remove_buttons": True}
    if act == "skip":
        manifest, _ = await current(conn, today=datetime.now(timezone.utc).date())
        rel = manifest.release(row["subject"]) if manifest is not None else None
        if rel is not None and rel.kind == "security":
            return {"answer": "Исправление безопасности пропустить нельзя.", "edit_text": None, "remove_buttons": False}
        await conn.execute("UPDATE update_notices SET skipped = true WHERE id = $1", row["id"])
        return {"answer": f"Версия {row['subject']} пропущена — о ней больше не напомню; о следующих сообщу.",
                "edit_text": None, "remove_buttons": True}
    return GONE


# --- итог ./ops/update.sh (команда `shturman update-result`) -----------------------------------------

def check_result(run: str, result: str, from_name: str, to_name: str, new_fails: Iterable[str]) -> dict[str, Any]:
    """Проверка полей итога. Бросает ValueError с текстом для человека."""
    if not _RUN.match(run or ""):
        raise ValueError("--run: метка запуска вида 20261017T101500Z")
    if result not in RESULTS:
        raise ValueError("--result: " + ", ".join(RESULTS))
    frm, to = clean_line(from_name, 80), clean_line(to_name, 80)
    if not frm or not to:
        raise ValueError("--from и --to: версии до и после")
    fails = [f for f in new_fails if f]
    if any(not _ROW.match(f) for f in fails) or len(fails) > 20:
        raise ValueError("--new-fails: имена строк doctor через пробел")
    return {"run": run, "result": result, "from_name": frm, "to_name": to, "new_fails": fails}


async def record_result(conn: asyncpg.Connection, fields: dict[str, Any]) -> bool:
    """Записывает итог (один раз на запуск). True — записан впервые."""
    got = await conn.fetchval(
        """INSERT INTO update_runs (run, result, from_name, to_name, new_fails) VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (run) DO NOTHING RETURNING id""",
        fields["run"], fields["result"], fields["from_name"], fields["to_name"], fields["new_fails"])
    return got is not None


def cli_result(args: Any) -> None:
    """`shturman update-result` — в контейнере сервиса, из ./ops/update.sh."""
    import sys

    from . import db
    from .config import Config, ConfigError

    try:
        fields = check_result(args.run, args.result, args.from_name, args.to_name, (args.new_fails or "").split())
        config = Config.from_env()
    except (ValueError, ConfigError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)

    async def go() -> bool:
        conn = await db.connect(config.dsn)
        try:
            return await record_result(conn, fields)
        finally:
            await conn.close()

    try:
        fresh = asyncio.run(go())
    except Exception as exc:  # noqa: BLE001 — база недоступна: обновлению это не мешает
        print(f"Итог обновления сервису не передан ({type(exc).__name__}).", file=sys.stderr)
        sys.exit(1)
    print("Итог обновления передан сервису: владелец получит сообщение в боте согласований." if fresh
          else "Этот итог уже был передан.")


def add_parser(sub: Any) -> None:
    p = sub.add_parser("update-result", help="итог ./ops/update.sh — сервису, для сообщения владельцу (Р-101)")
    p.add_argument("--run", required=True)
    p.add_argument("--result", required=True, choices=RESULTS)
    p.add_argument("--from", dest="from_name", required=True)
    p.add_argument("--to", dest="to_name", required=True)
    p.add_argument("--new-fails", default="")


# --- что видно снаружи ------------------------------------------------------------------------------

@dataclass
class Updates:
    """Состояние модуля в `state.extras["updates"]`."""
    booted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


async def overview(conn: asyncpg.Connection, state: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Для `GET /api/status` и строки `updates` в ./ops/doctor.sh: только теги, коды и время — без текстов
    из списка выпусков (его видит владелец, а не ассистент)."""
    now = now or datetime.now(timezone.utc)
    config = state.config
    run = running(config)
    manifest, problem = await current(conn, today=now.date())
    check = await _get(conn, "check")
    at = _when(check.get("at"))
    rels = ahead(manifest, run, config.update_channel) if manifest else []
    h = newer_hermes(manifest, run) if manifest else None
    return {
        "enabled": bool(config.updates), "channel": config.update_channel,
        "running": {"version": run.version, "tag": run.tag or None, "commit": run.commit[:12] or None,
                    "hermes": run.hermes or None},
        "checked_age_s": int((now - at).total_seconds()) if at else None,
        "problem": problem, "fails": int(check.get("fails") or 0),
        "expires": manifest.expires.isoformat() if manifest else None,
        "available": rels[-1].tag if rels else None,
        "available_kind": ("security" if any(r.kind == "security" for r in rels) else "regular") if rels else None,
        "behind": len(rels),
        "hermes_new": h.tag if h else None,
    }


async def app_view(state: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Плашка в приложении «Штурман» (блок «Сейчас»): что доступно и как обновить; итог последнего
    обновления за трое суток. Тексты — из проверенного списка выпусков, показываются как текст."""
    now = now or datetime.now(timezone.utc)
    config = state.config
    run = running(config)
    async with state.ro_pool.acquire() as conn:
        manifest, problem = await current(conn, today=now.date()) if config.updates else (None, None)
        last = await conn.fetchrow(
            """SELECT result, from_name, to_name, reported_at FROM update_runs
               WHERE reported_at > $1 ORDER BY id DESC LIMIT 1""", now - timedelta(days=3))
    rels = ahead(manifest, run, config.update_channel) if manifest else []
    available = None
    if rels:
        newest = rels[-1]
        minutes = [r.downtime_minutes for r in rels if r.downtime_minutes is not None]
        available = {"tag": newest.tag, "rc": newest.rc, "count": len(rels),
                     "security": any(r.kind == "security" for r in rels), "changes": list(newest.changes),
                     "downtime_minutes": sum(minutes) if minutes else None,
                     "owner_steps": [s for r in rels for s in r.owner_steps][:3],
                     "codex_text": codex_text(newest.tag)}
    return {"running": run.name, "available": available, "problem": problem if config.updates else None,
            "enabled": bool(config.updates),
            "last": {"result": last["result"], "from": last["from_name"], "to": last["to_name"],
                     "at": last["reported_at"].isoformat()} if last else None}


# --- жизненный цикл ---------------------------------------------------------------------------------

# Подмена сети и времени в тестах: {"transport": httpx.MockTransport(...), "start_delay": 0}.
TEST_OVERRIDES: dict[str, Any] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def tick(state: Any, client: httpx.AsyncClient | None, *, now: datetime) -> None:
    """Раз в минуту: проверка списка (раз в сутки), итоги обновлений, смена версии, новости."""
    config = state.config
    info = state.extras.get("updates")
    booted = info.booted_at if isinstance(info, Updates) else now
    if config.updates:
        async with state.pool.acquire() as conn:
            check = await _get(conn, "check")
        next_at = _when(check.get("next_at"))
        if now - booted >= BOOT_DELAY and (next_at is None or now >= next_at):
            await check_now(state, client, now=now)
    async with state.pool.acquire() as conn:
        await tell_results(conn, state, now)
        await tell_changed(conn, state, now)
        if config.updates:
            await tell_news(conn, state, now)


@contextlib.asynccontextmanager
async def lifespan(state: Any) -> AsyncIterator[None]:
    now = _now()
    state.extras["updates"] = Updates(booted_at=now)
    try:
        async with state.pool.acquire() as conn:
            await remember_running(conn, running(state.config), now=now)
    except Exception as exc:  # noqa: BLE001 — уведомления не должны мешать запуску сервиса
        logger.warning("обновления: запущенная версия не записана (%s)", type(exc).__name__)
    client: httpx.AsyncClient | None = None
    if state.config.updates:
        try:
            client = make_client(state.config, TEST_OVERRIDES.get("transport"))
        except Exception as exc:  # noqa: BLE001 — например, прокси SOCKS без пакета socksio
            logger.warning("обновления: клиент не создан (%s) — проверка списка выпусков не идёт", type(exc).__name__)

    async def loop() -> None:
        await asyncio.sleep(TEST_OVERRIDES.get("start_delay", 10.0))
        while True:
            try:
                await tick(state, client, now=_now())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — следующая попытка через минуту
                logger.warning("обновления: сбой (%s)", type(exc).__name__)
            await asyncio.sleep(TICK)

    task = state.spawn(loop(), name="updates")
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if client is not None:
            await client.aclose()
