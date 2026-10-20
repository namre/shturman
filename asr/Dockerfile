# Контейнер распознавания речи «Штурмана» (asr/server.py). Собирается на сервере, когда владелец
# включает расшифровку голосовых: ./ops/asr.sh on. Базовый образ — по точной версии, зависимости —
# по списку с контрольными суммами (requirements.lock). Модель в образ не входит: её скачивает
# и сверяет ./ops/asr.sh в data/asr/model, контейнер получает её только на чтение.
FROM python:3.12.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MALLOC_ARENA_MAX=2

# ffmpeg — отдельной программой: разбирает Ogg/Opus голосовых и звук «кружков» (решение Р-63).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libsndfile1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.lock ./
# torch и torchaudio для процессора лежат в индексе PyTorch; суммы всех пакетов — в списке.
RUN pip install --require-hashes --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.lock
COPY server.py ./

ENV HOME=/tmp \
    ASR_MODEL_DIR=/data/model \
    ASR_PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

CMD ["python", "server.py"]
