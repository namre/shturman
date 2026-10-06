"""Серверная часть мастера настройки. Hermes монтирует её в /api/plugins/shturman/.

Здесь только то, чего нет в самом дашборде Hermes. Ключи модели, токен бота, список разрешённых
пользователей, перезапуск шлюза и установку плагинов страница мастера отправляет в штатные
вызовы Hermes напрямую — этот файл их не видит и не хранит.

Все маршруты закрыты общим входом дашборда: без сессии Hermes до них не допускает.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from shturman_core import botapi, personas, wizard  # noqa: E402
from shturman_core.pairing import Pairing  # noqa: E402
from shturman_core.state import Store  # noqa: E402

router = APIRouter()

PROBE_TIMEOUT = 90


def _store() -> Store:
    return Store()


def _bot_token() -> str:
    from hermes_cli.config import get_env_value_prefer_dotenv

    return (get_env_value_prefer_dotenv("TELEGRAM_BOT_TOKEN") or "").strip()


# ------------------------------------------------------------------ состояние

@router.get("/state")
async def get_state() -> dict[str, Any]:
    return wizard.snapshot(_store())


class MarkBody(BaseModel):
    key: str


@router.post("/mark")
async def post_mark(body: MarkBody) -> dict[str, Any]:
    try:
        wizard.mark(_store(), body.key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return wizard.snapshot(_store())


# ------------------------------------------------------------- имя и характер

class PersonaBody(BaseModel):
    persona: str = ""
    custom_name: str = ""
    custom_voice: str = ""
    tone: str = ""
    owner_address: str = ""
    intro: str = ""
    custom_intro: str = ""
    owner_genitive: str = ""
    soul: str = ""          # текущий текст SOUL.md, прочитанный страницей штатным вызовом


@router.post("/persona")
async def post_persona(body: PersonaBody) -> dict[str, Any]:
    """Сохраняет выбор и возвращает новый текст SOUL.md; записывает его страница штатным вызовом."""
    raw = body.model_dump()
    soul = raw.pop("soul")
    store = _store()
    choice = personas.save_choice(store, raw)
    wizard.mark(store, "persona_saved")
    return {
        "persona": choice,
        "resolved": personas.resolved(choice),
        "soul": personas.apply_to_soul(soul, choice),
    }


# ------------------------------------------------------------------------ бот

class BotCheckBody(BaseModel):
    token: Optional[str] = None     # пусто — проверить уже сохранённый токен


@router.post("/bot/check")
async def post_bot_check(body: BotCheckBody) -> dict[str, Any]:
    """Спрашивает у Telegram, чей это токен. Токен никуда не записывается."""
    token = (body.token or "").strip() or _bot_token()
    if not token:
        return {"ok": False, "error": "Токен не задан."}
    if not botapi.TOKEN_RE.match(token):
        return {"ok": False, "error": "Это не похоже на токен бота: в нём цифры, двоеточие и длинная строка."}
    try:
        me = await asyncio.to_thread(botapi.get_me, token)
    except botapi.BotApiError as exc:
        return {"ok": False, "error": f"Telegram не принял токен: {exc}"}
    username = str(me.get("username") or "")
    name = str(me.get("first_name") or "")
    wizard.remember_bot(_store(), username, name)
    return {"ok": True, "username": username, "name": name}


# ------------------------------------------------------------------- привязка

@router.post("/pairing/start")
async def post_pairing_start() -> dict[str, Any]:
    store = _store()
    bot = store.read("wizard").get("bot") or {}
    username = str(bot.get("username") or "")
    if not username:
        check = await post_bot_check(BotCheckBody())
        if not check.get("ok"):
            raise HTTPException(status_code=409, detail="Сначала сохраните и проверьте токен бота.")
        username = check["username"]
    started = Pairing(store).start()
    return {
        "bot_username": username,
        "deep_link": wizard.deep_link(username, started["token"]),
        "code": started["code"],
        "expires_at": started["expires_at"],
    }


@router.get("/pairing")
async def get_pairing() -> dict[str, Any]:
    return Pairing(_store()).status()


@router.post("/pairing/confirm")
async def post_pairing_confirm() -> dict[str, Any]:
    """Владелец в мастере подтвердил, что аккаунт, написавший боту, — его."""
    pairing = Pairing(_store())
    owner = pairing.confirm()
    if owner is None:
        raise HTTPException(status_code=409, detail="Подтверждать нечего: время привязки вышло. Получите новую ссылку.")
    return pairing.status()


@router.post("/pairing/reject")
async def post_pairing_reject() -> dict[str, Any]:
    pairing = Pairing(_store())
    pairing.reject()
    return pairing.status()


# ------------------------------------------------------------ проверка модели

@router.post("/model/probe")
async def post_model_probe() -> dict[str, Any]:
    """Короткий настоящий диалог через Hermes: проверяет весь путь от настроек до ответа модели."""
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "hermes_cli.main", "chat", "-Q", "--max-turns", "1",
            "-q", wizard.PROBE_PROMPT,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return {"ok": False, "error": f"Не удалось запустить проверку ({type(exc).__name__})."}
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"ok": False, "error": "Модель не ответила за полторы минуты."}
    result = wizard.parse_probe_output(proc.returncode or 0, out.decode("utf-8", "replace"))
    if result["ok"]:
        wizard.mark(_store(), "model_ok")
    return result
