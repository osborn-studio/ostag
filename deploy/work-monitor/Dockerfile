FROM node:22-slim AS node

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=node /usr/local /usr/local
COPY . /app
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r deploy/work-monitor/requirements.lock \
    && pip install --no-cache-dir --no-deps . \
    && python -c "import lark_oapi; import Crypto" \
    && npm install --global @larksuite/cli@1.0.69 dingtalk-workspace-cli@1.0.51 \
    && node --version && lark-cli --version
RUN useradd --system --uid 10001 --create-home --home-dir /home/ostag ostag \
    && mkdir -p /var/lib/ostag && chown ostag /var/lib/ostag
USER ostag
EXPOSE 8000
CMD ["uvicorn", "ostag.work_monitor.server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
