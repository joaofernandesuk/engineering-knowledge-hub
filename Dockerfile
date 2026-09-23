FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HUB_RUNTIME=/data HUB_PROJECTS=/data/projects.json HUB_VAULT=/knowledge HUB_GRAPHS=/graphs HUB_STATIC=/app/static HUB_INDEX_DB=/tmp/hub-index.db
WORKDIR /app
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
COPY requirements.lock ./
COPY backend/ ./backend/
RUN python -m pip install --no-cache-dir --upgrade pip==26.2.1 \
    && python -m pip install --no-cache-dir -r requirements.lock \
    && python -m pip install --no-cache-dir --no-deps .
COPY --from=frontend /frontend/dist/ /app/static/
EXPOSE 8765
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8765"]
