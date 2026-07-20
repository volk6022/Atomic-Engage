FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

COPY pyproject.toml .
RUN uv pip install --system "kurigram>=2.2.19" TgCrypto "fastapi>=0.115.0" "uvicorn[standard]>=0.30.0" "pydantic>=2.10.0" "pydantic-settings>=2.7.0" "arq>=0.23.0" "sqlalchemy[asyncio]>=2.0.36" "asyncpg>=0.30.0" "redis[asyncio]>=5.2.0" "geoip2>=4.2.0" "phonenumbers>=8.13.0" "alembic>=1.14.0" "prometheus-client>=0.22.0" "httpx>=0.28.0" "python-multipart>=0.0.20" "structlog>=24.4.0"

RUN uv pip install --system -e ".[dev]"

COPY app/ app/
COPY data/ data/
COPY migrations/ migrations/
# seed_accounts.py imports the app's models and must run inside the instance
# network, since postgres is never published to the host. It is therefore
# invoked with `compose run` against this image and has to ship inside it.
COPY scripts/ scripts/

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]