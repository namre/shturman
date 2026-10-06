# Сервис переписки «Штурмана». Собирается на сервере из этого каталога: ./ops/up.sh.
# Базовый образ назван по точной версии; обновление версии — отдельным изменением.
FROM python:3.12.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git — история страниц памяти (без него страницы собираются, но без истории).
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# Работает под пользователем, которого задаёт Compose (владелец каталога данных на сервере),
# поэтому домашний каталог и каталог данных должны быть доступны любому идентификатору.
ENV HOME=/tmp \
    SHTURMAN_DATA_DIR=/data \
    SHTURMAN_HOST=0.0.0.0 \
    SHTURMAN_PORT=8765
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=3).status == 200 else 1)"

ENTRYPOINT ["shturman"]
CMD ["serve"]
