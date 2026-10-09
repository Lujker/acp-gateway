FROM ghcr.io/astral-sh/uv:0.7.6 AS uv
FROM python:3.12-slim-bookworm AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm AS runtime
RUN groupadd --gid 10001 acpgw && useradd --uid 10001 --gid acpgw --create-home acpgw \
    && mkdir /data /config && chown acpgw:acpgw /data /config
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
WORKDIR /data
ENTRYPOINT ["acpgw", "--config", "/config/gateway.yaml", "--env-file", "/config/gateway.env"]
CMD ["serve"]
