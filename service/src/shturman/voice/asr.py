"""Клиент контейнера распознавания речи (`asr/server.py`).

Контейнер стоит в закрытой сети сервиса и в интернет не ходит. Запрос — файл голосового как
есть (Ogg/Opus, MP4 «кружка»); ответ — текст и длительность в секундах.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

TIMEOUT = 300.0      # минута речи распознаётся секунды; запас — на десятиминутное голосовое


class AsrUnavailable(Exception):
    """Контейнер не ответил или ответил не так. Сообщение остаётся в очереди."""


class AsrRejected(Exception):
    """Контейнер отказался: файл не разобрать, нет звука. Повторять бессмысленно."""


@dataclass
class Transcript:
    text: str
    seconds: float | None


class AsrClient:
    def __init__(self, url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # trust_env=False: никаких прокси из окружения — контейнер рядом, в закрытой сети.
        self._client = httpx.AsyncClient(base_url=url, transport=transport, trust_env=False,
                                         timeout=httpx.Timeout(TIMEOUT, connect=5.0))
        self.model: str | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def health(self) -> dict[str, Any]:
        try:
            response = await self._client.get("/health", timeout=10.0)
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AsrUnavailable(type(exc).__name__) from None
        if response.status_code != 200 or not isinstance(data, dict):
            raise AsrUnavailable(f"http_{response.status_code}")
        model = data.get("model")
        self.model = model if isinstance(model, str) else None
        return data

    async def transcribe(self, audio: bytes) -> Transcript:
        try:
            response = await self._client.post(
                "/transcribe", content=audio, headers={"Content-Type": "application/octet-stream"})
        except httpx.HTTPError as exc:
            raise AsrUnavailable(type(exc).__name__) from None
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code == 422:
            reason = data.get("error") if isinstance(data, dict) else None
            raise AsrRejected(str(reason or "bad_audio")[:200])
        if response.status_code != 200 or not isinstance(data, dict) or not isinstance(data.get("text"), str):
            raise AsrUnavailable(f"http_{response.status_code}")
        seconds = data.get("seconds")
        return Transcript(text=data["text"],
                          seconds=float(seconds) if isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
                          else None)
