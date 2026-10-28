# Базовый образ задан здесь, а не через build.yaml: Supervisor 2026.10+ считает
# build.yaml устаревшим и подставляет свою базу без Python. Образ python
# мультиархитектурный, Supervisor сам выбирает нужную платформу.
FROM docker.io/library/python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ ./app/

CMD ["python", "-m", "app.main"]
