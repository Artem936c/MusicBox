# =============================================================================
# MusicBox — образ «всё в одном»: сборка Mini App + backend с ботом и API.
# Сборка:  docker build -t musicbox .
# Запуск:  docker run --env-file .env -p 8000:8000 -v ./data:/app/data musicbox
# =============================================================================

# --- Этап 1: сборка фронтенда (React + Vite) ---------------------------------
FROM node:20-alpine AS frontend-build

WORKDIR /app/frontend

# Сначала манифесты — слой с зависимостями переиспользуется между сборками.
COPY frontend/package.json frontend/package-lock.json* ./
# npm ci требует package-lock.json; если его нет, ставим обычным npm install.
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund

COPY frontend/ ./
RUN npm run build


# --- Этап 2: рабочий образ (Python 3.12) -------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC

WORKDIR /app

# Зависимости Python отдельным слоем — не пересобираются при правке кода.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Код бота и API.
COPY backend/ ./backend/
COPY scripts/ ./scripts/
COPY pytest.ini ./

# Собранный Mini App из первого этапа.
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

# База данных живёт в томе, чтобы переживать пересоздание контейнера.
ENV DATABASE_PATH=/app/data/musicbox.db \
    FRONTEND_DIST=/app/frontend/dist \
    API_HOST=0.0.0.0 \
    API_PORT=8000

RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "backend.main"]
