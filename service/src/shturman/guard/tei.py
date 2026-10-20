"""Модель-классификатор за HTTP: клиент сервера HuggingFace Text Embeddings Inference (TEI).

Модель живёт в отдельном контейнере без выхода в интернет (профиль Compose `guard`), сервис
ходит к нему по HTTP. В образе сервиса нет ни torch, ни transformers.

Проверено по исходникам TEI 1.9.4 (`router/src/http/types.rs`, `server.rs`,
`backends/ort/src/lib.rs`):
  * `POST /predict` принимает `{"inputs": [["текст"], ["текст"]], "truncate": true}` — пачка
    одиночных текстов записывается именно так: список из двух строк `["a", "b"]` TEI понимает
    как ПАРУ текстов, а не как два текста;
  * ответ на пачку — список по текстам, в каждом — `[{"label": "...", "score": 0.97}, ...]`
    по всем классам (после softmax);
  * `GET /info` отдаёт `model_id` и `max_client_batch_size`; `GET /health` — «жив».
С настоящим контейнером TEI клиент не запускался: в среде разработки нет Docker.

Длинный текст. Модель видит только первые N токенов (512 у DeBERTa), остальное TEI отрезает.
Указание, спрятанное в конце длинного письма, так осталось бы непрочитанным, поэтому клиент сам
режет текст на перекрывающиеся окна и берёт наибольшую оценку.

Текст сообщений в журнал не пишется; тело ответа сервера при ошибке не читается (TEI может
повторить в нём входной текст).
"""

from __future__ import annotations

from typing import Sequence

import httpx

# Названия класса «обычный текст» у известных моделей. Всё остальное — «внедрённая инструкция».
BENIGN_LABELS = frozenset({
    "safe", "benign", "legit", "legitimate", "label_0", "0", "negative", "clean", "normal",
    "no_injection", "not_injection", "trusted", "ok", "harmless",
})

DEFAULT_WINDOW = 1000    # знаков в окне: с запасом меньше 512 токенов для русского текста
DEFAULT_OVERLAP = 200


class ScorerError(Exception):
    """Классификатор недоступен или ответил не тем. В тексте ошибки нет данных переписки.

    mismatch — сервер отдаёт не ту модель, что указана в настройках. rejected — сервер отказался
    принять именно эти тексты (коды 400, 413, 422): повтор той же пачки не поможет.
    """

    def __init__(self, reason: str, *, mismatch: bool = False, rejected: bool = False) -> None:
        super().__init__(reason)
        self.reason, self.mismatch, self.rejected = reason, mismatch, rejected


def windows(text: str, size: int = DEFAULT_WINDOW, overlap: int = DEFAULT_OVERLAP) -> list[str]:
    """Режет текст на перекрывающиеся окна не длиннее `size` знаков. Короткий текст — одно окно.

    Граница окна сдвигается к ближайшему пробелу, чтобы не резать слово. size <= 0 — не резать.
    """
    if size <= 0 or len(text) <= size:
        return [text]
    overlap = max(0, min(overlap, size // 2))
    out: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            space = text.rfind(" ", start + size // 2, end)
            newline = text.rfind("\n", start + size // 2, end)
            cut = max(space, newline)
            if cut > start:
                end = cut
        out.append(text[start:end])
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return out


def positive_score(labels: Sequence[dict], benign: frozenset[str] = BENIGN_LABELS) -> float:
    """Оценка «это внедрённая инструкция» из ответа по классам: 1 − сумма «обычных» классов."""
    safe = 0.0
    known = False
    for item in labels:
        score = item.get("score")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            raise ScorerError("в ответе классификатора не числа")
        if str(item.get("label", "")).strip().lower() in benign:
            safe += float(score)
            known = True
    if not known:
        # Ни одного знакомого «обычного» класса: модель не та, под которую настроен сервис.
        raise ScorerError("в ответе классификатора нет класса «обычный текст»", mismatch=True)
    return min(1.0, max(0.0, 1.0 - safe))


class TeiScorer:
    """`Scorer` поверх TEI. Синхронный: вызывается из отдельного потока (см. `Scorer`)."""

    def __init__(
        self, url: str, model: str, *, window: int = DEFAULT_WINDOW, overlap: int = DEFAULT_OVERLAP,
        timeout: float = 30.0, transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not url.startswith(("http://", "https://")):
            raise ValueError("SHTURMAN_GUARD_URL: нужен адрес вида http://имя:порт")
        self.name = model
        self.window, self.overlap = window, overlap
        # trust_env=False: тексты переписки идут только на указанный адрес, а не через прокси
        # из переменных окружения.
        self._client = httpx.Client(base_url=url.rstrip("/"), transport=transport, trust_env=False,
                                    timeout=httpx.Timeout(timeout, connect=3.0))
        self._verified = False
        self.max_batch = 32

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, *, json: object = None, timeout: float | None = None) -> httpx.Response:
        try:
            response = self._client.request(method, path, json=json,
                                            **({"timeout": timeout} if timeout else {}))
        except httpx.HTTPError as exc:
            self._verified = False
            raise ScorerError(type(exc).__name__) from None
        if response.status_code != 200:
            if response.status_code >= 500:
                self._verified = False
            raise ScorerError(f"HTTP {response.status_code}", rejected=response.status_code in (400, 413, 422))
        return response

    def healthy(self) -> bool:
        try:
            self._request("GET", "/health", timeout=2.0)
        except ScorerError:
            return False
        return True

    def verify(self) -> None:
        """Сервер отдаёт именно ту модель, именем которой будут подписаны оценки."""
        response = self._request("GET", "/info", timeout=5.0)
        try:
            info = response.json()
            names = {info.get("model_id"), info.get("served_model_name")}
            limit = info.get("max_client_batch_size")
        except (ValueError, AttributeError):
            raise ScorerError("непонятный ответ /info") from None
        # Имя в настройках может нести ревизию после «@»: сервер её не знает.
        if self.name not in names and self.name.split("@", 1)[0] not in names:
            raise ScorerError("сервер классификатора отдаёт другую модель", mismatch=True)
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            self.max_batch = limit
        self._verified = True

    def _predict(self, pieces: list[str]) -> list[float]:
        response = self._request("POST", "/predict",
                                 json={"inputs": [[p] for p in pieces], "truncate": True})
        try:
            data = response.json()
        except ValueError:
            raise ScorerError("ответ не JSON") from None
        if not isinstance(data, list) or len(data) != len(pieces) or not all(isinstance(d, list) for d in data):
            raise ScorerError("число оценок не совпало с числом текстов")
        return [positive_score(item) for item in data]

    def _predict_each(self, pieces: list[str]) -> list[float]:
        """Пачка, которую сервер отверг из-за содержимого, оценивается по одному тексту. Текст,
        который сервер не принял и в одиночку, получает оценку 1: то, что классификатор не смог
        прочитать, ассистенту не показывается, пока не посмотрит владелец."""
        try:
            return self._predict(pieces)
        except ScorerError as exc:
            if not exc.rejected:
                raise
        out: list[float] = []
        for piece in pieces:
            try:
                out.extend(self._predict([piece]))
            except ScorerError as exc:
                if not exc.rejected:
                    raise
                out.append(1.0)
        return out

    def score(self, texts: Sequence[str]) -> list[float]:
        if not self._verified:
            self.verify()
        pieces: list[str] = []
        owner: list[int] = []
        for index, text in enumerate(texts):
            for piece in windows(text, self.window, self.overlap):
                if piece.strip():   # пустой текст сервер не принимает, да и оценивать в нём нечего
                    pieces.append(piece)
                    owner.append(index)
        scores = [0.0] * len(texts)
        for start in range(0, len(pieces), self.max_batch):
            chunk = pieces[start:start + self.max_batch]
            for offset, value in enumerate(self._predict_each(chunk)):
                index = owner[start + offset]
                scores[index] = max(scores[index], value)
        return scores
