FROM python:3.12-slim

# uv is what this project already uses locally for dependency management -
# copying the prebuilt binary from astral's official image is the
# recommended way to get it into a Docker build without a separate install step.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Install dependencies in their own layer first, so this layer is only
# rebuilt when pyproject.toml/uv.lock actually change, not on every code edit.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY app ./app
COPY scripts ./scripts

RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8080

# Cloud Run injects $PORT (defaults to 8080 locally); no brackets on CMD
# means Docker runs this via a shell, so the ${PORT:-8080} expansion works.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}
