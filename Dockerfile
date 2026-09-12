# Production image for ECS Fargate.
#
# One image serves every role. The web dashboard and all eight workers are the
# same package selected at runtime by HAWKNETIC_SERVICE, exactly as they are on
# Railway today, so building nine images would mean nine identical layer sets
# and nine chances for them to drift out of step. The task definition picks the
# role; the image does not care which one it is.
#
# Layout note -- this is load-bearing, not a style choice. `config.repo_path()`
# resolves repository files as `Path(__file__).parents[2] / <parts>`, so
# `migrations/postgres` is found relative to the *source tree*, not the
# installed package. Under a `pip install` of the wheel that expression points
# at the interpreter's lib directory, `discover_migrations()` finds nothing,
# and `database-migrate` reports success having applied zero migrations. That
# failure is silent and would reach production as an empty schema.
#
# So the image keeps the source layout the application already assumes and runs
# it with PYTHONPATH=/app/src -- identical to the Procfile, nixpacks.toml and
# railway.json commands that run it today. Only third-party dependencies are
# installed as wheels.

# ---------------------------------------------------------------------------
# Stage 1: build dependency wheels
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

COPY requirements.txt ./

# psycopg[binary] ships prebuilt libpq wheels, so this resolves without a
# compiler and the runtime stage needs neither gcc nor libpq-dev.
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2: runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# PYTHONUNBUFFERED is not cosmetic here: CloudWatch reads stdout/stderr, and a
# buffered worker that dies mid-cycle takes its last and most useful log lines
# with it.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    APP_ENV=production \
    HAWKNETIC_SERVICE=web \
    PORT=8000

# Research-only posture, baked in as image defaults so a task definition that
# forgets to set them does not silently come up permissive. An environment
# override is then a visible, auditable line in a task definition.
ENV RESEARCH_ONLY=true \
    KALSHI_ORDER_UPLOAD_ENABLED=false \
    LIVE_EXECUTION_ENABLED=false \
    AUTO_UPLOAD_ENABLED=false \
    AUTO_TRADE_ENABLED=false \
    MODEL_PROMOTION_ENABLED=false \
    STALE_CACHE_AS_FRESH=false \
    DASHBOARD_REQUIRE_AUTH_WHEN_HOSTED=true

# curl is for the container healthcheck below and nothing else.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 hawknetic \
    && useradd --system --uid 10001 --gid hawknetic --no-create-home \
       --shell /usr/sbin/nologin hawknetic

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels \
      "psycopg[binary]>=3.2,<4" "psycopg_pool>=3.2,<4" \
    && rm -rf /wheels

WORKDIR /app

# src/ and migrations/ must sit as siblings under /app so that
# parents[2] of src/kalshi_research_bot/config.py is /app.
COPY --chown=hawknetic:hawknetic src ./src
COPY --chown=hawknetic:hawknetic migrations ./migrations

# Writable scratch for generated artifacts. On ECS the durable equivalent is
# S3, not this directory -- anything written here dies with the task, which is
# the intended behaviour for a Fargate task with no volume.
RUN install -d -o hawknetic -g hawknetic /app/data
ENV RESEARCH_DATA_DIR=/app/data

USER hawknetic

EXPOSE 8000

# Both the web role and the workers expose /healthz -- workers run a small
# health server alongside the cycle loop -- so one healthcheck covers every
# role the image can be started as.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

# Exec form, so PID 1 is the Python process and ECS's SIGTERM on task stop
# reaches the application instead of a shell that ignores it. The worker then
# gets to finish its transaction inside the stop timeout.
ENTRYPOINT ["python", "-m", "kalshi_research_bot"]
CMD ["service-start"]
