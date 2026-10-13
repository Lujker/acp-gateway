FROM ghcr.io/astral-sh/uv:0.7.6 AS uv
FROM python:3.12-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-editable
COPY tests/fakes /app/tests/fakes
COPY tests/fixtures /app/tests/fixtures
COPY scripts/lab_node.py /app/lab_node.py
COPY scripts/lab_goose.py /app/lab_goose.py
ENV PATH=/app/.venv/bin:$PATH PYTHONPATH=/app/tests PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 XDG_CONFIG_HOME=/lab/config-home XDG_DATA_HOME=/lab/data
WORKDIR /lab
CMD ["python", "/app/lab_node.py"]
