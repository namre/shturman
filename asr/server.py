"""Контейнер распознавания речи «Штурмана»: голосовое → текст.

Модель — GigaAM-Multilingual (ai-sage, MIT), вариант ctc, как её выложил производитель:
веса и код модели (modeling_gigaam.py) лежат в /data/model; их скачивает и сверяет по
контрольным суммам ./ops/asr.sh. Контейнер в интернет не ходит (сеть backend закрытая) и
ничего не хранит: файл пишется во временный каталог в памяти и удаляется сразу после разбора.

  GET  /health      {"model": "...", "ready": true}
  POST /transcribe  тело — файл голосового как есть (Ogg/Opus, MP4 «кружка» и т. п.)
                    → 200 {"text": "...", "seconds": 41.6}
                    → 422 {"error": "bad_audio" | "too_long" | "too_big"}

Модель принимает до 25 секунд звука за раз, поэтому длинное голосовое режется на куски
не длиннее CHUNK секунд — по самому тихому месту в конце куска, чтобы не резать слово.
Распознаётся по одному запросу за раз: параллельные запросы ждут очереди.
"""

from __future__ import annotations

import ctypes
import gc
import importlib.util
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import torch

MODEL_DIR = Path(os.environ.get("ASR_MODEL_DIR", "/data/model"))
MODEL_NAME = os.environ.get("ASR_MODEL_NAME", "ai-sage/GigaAM-Multilingual@ctc")
PORT = int(os.environ.get("ASR_PORT", "8000"))
THREADS = int(os.environ.get("ASR_THREADS", "2"))
MAX_BYTES = int(os.environ.get("ASR_MAX_BYTES", str(25 * 1024 * 1024)))
MAX_SECONDS = int(os.environ.get("ASR_MAX_SECONDS", "900"))
RATE = 16000
CHUNK = 22.0          # секунд в куске; модель берёт до 25
SEARCH = 6.0          # в последних SEARCH секундах куска ищется самое тихое место
FRAME = 0.1           # шаг поиска тишины, секунд

logging.basicConfig(level=logging.INFO, format="%(asctime)s asr %(levelname)s %(message)s")
log = logging.getLogger("asr")


def load_model():
    """Код модели производителя загружается из файла рядом с весами — без обращения к сети."""
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    spec = importlib.util.spec_from_file_location("modeling_gigaam", MODEL_DIR / "modeling_gigaam.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["modeling_gigaam"] = module
    spec.loader.exec_module(module)
    model = module.GigaAMModel.from_pretrained(str(MODEL_DIR)).eval()
    gc.collect()
    _trim()
    return model


def _trim() -> None:
    """Возвращает системе память, освобождённую после разбора: иначе процесс держит пик."""
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except OSError:
        pass


class BadAudio(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def decode(path: str) -> np.ndarray:
    """Любой формат голосового → 16 кГц, моно, int16. ffmpeg — отдельной программой."""
    done = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-t", str(MAX_SECONDS + 1), "-i", path,
         "-vn", "-f", "s16le", "-ac", "1", "-ar", str(RATE), "-"],
        capture_output=True, timeout=120)
    if done.returncode != 0:
        raise BadAudio("bad_audio")
    audio = np.frombuffer(done.stdout, dtype=np.int16)
    if len(audio) > MAX_SECONDS * RATE:
        raise BadAudio("too_long")
    return audio


def cut_points(audio: np.ndarray) -> list[tuple[int, int]]:
    """Границы кусков не длиннее CHUNK секунд, по самым тихим местам."""
    size, n = int(CHUNK * RATE), len(audio)
    frame, search = int(FRAME * RATE), int(SEARCH * RATE)
    out, start = [], 0
    while n - start > size:
        lo, hi = start + size - search, start + size
        window = audio[lo:hi].astype(np.float32)
        energy = [float(np.mean(window[i:i + frame] ** 2)) for i in range(0, len(window) - frame + 1, frame)]
        cut = lo + int(np.argmin(energy)) * frame + frame // 2 if energy else hi
        out.append((start, cut))
        start = cut
    if n - start > RATE // 10:      # хвост короче 0,1 с — не речь
        out.append((start, n))
    return out


def write_wav(path: str, audio: np.ndarray) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(audio.tobytes())


class Recognizer:
    def __init__(self) -> None:
        torch.set_num_threads(THREADS)
        self.model = load_model()
        self.lock = threading.Lock()
        log.info("модель загружена: %s", MODEL_NAME)

    def transcribe(self, data: bytes) -> dict:
        with tempfile.TemporaryDirectory(dir="/dev/shm" if os.path.isdir("/dev/shm") else None) as tmp:
            source = os.path.join(tmp, "in")
            Path(source).write_bytes(data)
            audio = decode(source)
            parts = []
            with self.lock, torch.inference_mode():
                for i, (a, b) in enumerate(cut_points(audio)):
                    piece = os.path.join(tmp, f"{i}.wav")
                    write_wav(piece, audio[a:b])
                    text = str(self.model.transcribe(piece)).strip()
                    if text:
                        parts.append(text)
            _trim()
        return {"text": " ".join(parts), "seconds": round(len(audio) / RATE, 2)}


class Handler(BaseHTTPRequestHandler):
    recognizer: Recognizer

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send(200, {"model": MODEL_NAME, "ready": True})
        else:
            self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/transcribe":
            self._send(404, {"error": "not_found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BYTES:
            self._send(422, {"error": "too_big" if length > MAX_BYTES else "bad_audio"})
            return
        data = self.rfile.read(length)
        try:
            self._send(200, self.recognizer.transcribe(data))
        except BadAudio as exc:
            self._send(422, {"error": exc.code})
        except Exception as exc:  # noqa: BLE001
            log.error("сбой распознавания: %s", type(exc).__name__)
            self._send(500, {"error": "failed"})

    def log_message(self, fmt: str, *args) -> None:
        # В журнал — только метод, путь и код ответа; содержимого и текста там нет.
        log.info("%s", fmt % args)


def main() -> None:
    Handler.recognizer = Recognizer()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info("слушаю порт %d", PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
