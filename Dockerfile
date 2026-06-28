FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY ostag ./ostag
COPY examples ./examples
COPY docs ./docs
COPY scripts ./scripts

RUN uv sync --frozen --extra feishu --no-dev

CMD ["uv", "run", "python", "-m", "ostag.main", "--platform", "feishu"]
