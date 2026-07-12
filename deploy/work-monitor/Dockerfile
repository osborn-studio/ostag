FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir -r deploy/work-monitor/requirements.lock \
    && pip install --no-cache-dir --no-deps . \
    && python -c "import lark_oapi; import Crypto"
RUN useradd --system --uid 10001 ostag && mkdir -p /var/lib/ostag && chown ostag /var/lib/ostag
USER ostag
EXPOSE 8000
CMD ["uvicorn", "ostag.work_monitor.server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
